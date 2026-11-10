"""Versioned teaching policies (requirement R5.3).

A policy is the JSON produced by ``MultiAgentTeacher.save``. Each version is
stored once, content-addressed, and activated through an append-only
``policy_activations`` log. The active version is the latest activation, so
reverting a bad release is one admin call that re-activates an older version.

Instances cache the active version for ``CT_POLICY_CACHE_SECONDS`` (default
15 s), so a revert reaches every instance well within a minute.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

from prometheus_client import Gauge
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import PolicyActivation, PolicyVersion
from .rl.agents import CurriculumAgent, PacingAgent, TutorAgent
from .rl.bandit import LinUCB
from .skills import DEFAULT_TRACK, Track, load_tracks

POLICY_ACTIVE = Gauge("ct_policy_active_info", "Active teaching policy version", ["version"])


class PolicyError(RuntimeError):
    pass


def _version_for(blob: dict) -> tuple[str, str]:
    digest = hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()
    return f"{datetime.now(UTC):%Y%m%d}-{digest[:12]}", digest


def policy_track(blob: dict) -> str:
    """Which subject a policy teaches: its skills must match exactly one subject."""
    skills = set(blob.get("curriculum_skills", []))
    for t in load_tracks().values():
        if skills == set(t.skills):
            return t.id
    raise PolicyError("the policy's skills do not match any subject")


def import_policy(db: Session, blob: dict, actor_id: str, notes: str = "",
                  activate: bool = True) -> str:
    for k in ("curriculum", "curriculum_skills", "tutor", "pacing_q"):
        if k not in blob:
            raise PolicyError(f"policy file is missing {k!r}")
    track_id = policy_track(blob)
    version, digest = _version_for(blob)
    existing = db.scalar(select(PolicyVersion).where(PolicyVersion.sha256 == digest))
    if existing is not None:
        version = existing.version
    else:
        db.add(PolicyVersion(version=version, track_id=track_id, sha256=digest, blob=blob,
                             notes=notes))
        db.flush()
    if activate:
        activate_policy(db, version, actor_id, reason=f"import: {notes}".strip())
    return version


def import_policy_file(db: Session, path: str | Path, actor_id: str, notes: str = "") -> str:
    return import_policy(db, json.loads(Path(path).read_text()), actor_id, notes)


def activate_policy(db: Session, version: str, actor_id: str, reason: str = "") -> None:
    row = db.get(PolicyVersion, version)
    if row is None:
        raise PolicyError(f"unknown policy version {version}")
    db.add(PolicyActivation(version=version, track_id=row.track_id, activated_by=actor_id,
                            reason=reason))
    db.flush()


def active_version(db: Session, track_id: str = DEFAULT_TRACK) -> str | None:
    return db.scalar(select(PolicyActivation.version)
                     .where(PolicyActivation.track_id == track_id)
                     .order_by(PolicyActivation.id.desc()).limit(1))


def list_policies(db: Session) -> list[dict]:
    tracks = load_tracks()
    active = {t: active_version(db, t) for t in tracks}
    rows = db.scalars(select(PolicyVersion).order_by(PolicyVersion.track_id,
                                                     PolicyVersion.created_at.desc())).all()
    return [{"version": p.version, "track": p.track_id,
             "track_name": tracks[p.track_id].name if p.track_id in tracks else p.track_id,
             "sha256": p.sha256, "notes": p.notes,
             "created_at": p.created_at.isoformat() if p.created_at else None,
             "active": p.version == active.get(p.track_id)} for p in rows]


def import_bundled_policies(db: Session, actor_id: str) -> list[str]:
    """Import the benchmarked policy shipped for every subject that has none active."""
    done = []
    folder = resources.files("cognitive_twin").joinpath("content/policies")
    for track_id in load_tracks():
        if active_version(db, track_id) is None:
            f = folder.joinpath(f"{track_id}.json")
            import_policy(db, json.loads(f.read_text()), actor_id,
                          notes="bundled policy")
            done.append(track_id)
    return done


class LoadedPolicy:
    """The shared, read-only models of one policy version."""

    def __init__(self, version: str, blob: dict):
        self.version = version
        self.skills: list[str] = blob["curriculum_skills"]
        self.curriculum_model = LinUCB.from_dict(blob["curriculum"])
        self.tutor_model = LinUCB.from_dict(blob["tutor"])
        self.pacing_q: dict[str, list[float]] = {k: list(v) for k, v in blob["pacing_q"].items()}

    def agents(self, track: Track, agent_state: dict) -> tuple[CurriculumAgent, TutorAgent,
                                                               PacingAgent]:
        missing = set(self.skills) ^ set(track.skills)
        if missing:
            raise PolicyError(f"policy {self.version} does not match the content track")
        cur = CurriculumAgent(self.skills)
        cur.model = self.curriculum_model
        cur.current = agent_state.get("curriculum_current")
        cur._steps = int(agent_state.get("curriculum_steps", 0))
        tutor = TutorAgent()
        tutor.model = self.tutor_model
        # live sessions never update the shared Q-table, so a copy is not needed
        pacing = PacingAgent(q=self.pacing_q)
        return cur, tutor, pacing


class PolicyCache:
    def __init__(self, ttl_seconds: int):
        self.ttl = ttl_seconds
        self._lock = threading.Lock()
        self._loaded: dict[str, LoadedPolicy] = {}
        self._active: dict[str, tuple[float, str]] = {}

    def current(self, db: Session, track_id: str = DEFAULT_TRACK) -> LoadedPolicy:
        now = time.monotonic()
        with self._lock:
            cached = self._active.get(track_id)
        if cached is None or now - cached[0] > self.ttl:
            version = active_version(db, track_id)
            if version is None:
                raise PolicyError(f"no teaching policy is active for {track_id}; run "
                                  "`cognitive-twin policy import-bundled`")
            with self._lock:
                self._active[track_id] = (now, version)
        else:
            version = cached[1]
        return self.get(db, version)

    def get(self, db: Session, version: str) -> LoadedPolicy:
        with self._lock:
            if version in self._loaded:
                return self._loaded[version]
        row = db.get(PolicyVersion, version)
        if row is None:
            raise PolicyError(f"unknown policy version {version}")
        loaded = LoadedPolicy(version, row.blob)
        with self._lock:
            self._loaded[version] = loaded
        POLICY_ACTIVE.labels(version).set(1)
        return loaded

    def invalidate(self) -> None:
        with self._lock:
            self._active = {}
