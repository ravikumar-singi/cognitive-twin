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
from pathlib import Path

from prometheus_client import Gauge
from sqlalchemy import select
from sqlalchemy.orm import Session

from .db.models import PolicyActivation, PolicyVersion
from .rl.agents import CurriculumAgent, PacingAgent, TutorAgent
from .rl.bandit import LinUCB
from .skills import Track

POLICY_ACTIVE = Gauge("ct_policy_active_info", "Active teaching policy version", ["version"])


class PolicyError(RuntimeError):
    pass


def _version_for(blob: dict) -> tuple[str, str]:
    digest = hashlib.sha256(json.dumps(blob, sort_keys=True).encode()).hexdigest()
    return f"{datetime.now(UTC):%Y%m%d}-{digest[:12]}", digest


def import_policy(db: Session, blob: dict, actor_id: str, notes: str = "",
                  activate: bool = True) -> str:
    for k in ("curriculum", "curriculum_skills", "tutor", "pacing_q"):
        if k not in blob:
            raise PolicyError(f"policy file is missing {k!r}")
    version, digest = _version_for(blob)
    existing = db.scalar(select(PolicyVersion).where(PolicyVersion.sha256 == digest))
    if existing is not None:
        version = existing.version
    else:
        db.add(PolicyVersion(version=version, sha256=digest, blob=blob, notes=notes))
        db.flush()
    if activate:
        activate_policy(db, version, actor_id, reason=f"import: {notes}".strip())
    return version


def import_policy_file(db: Session, path: str | Path, actor_id: str, notes: str = "") -> str:
    return import_policy(db, json.loads(Path(path).read_text()), actor_id, notes)


def activate_policy(db: Session, version: str, actor_id: str, reason: str = "") -> None:
    if db.get(PolicyVersion, version) is None:
        raise PolicyError(f"unknown policy version {version}")
    db.add(PolicyActivation(version=version, activated_by=actor_id, reason=reason))
    db.flush()


def active_version(db: Session) -> str | None:
    return db.scalar(select(PolicyActivation.version)
                     .order_by(PolicyActivation.id.desc()).limit(1))


def list_policies(db: Session) -> list[dict]:
    active = active_version(db)
    rows = db.scalars(select(PolicyVersion).order_by(PolicyVersion.created_at.desc())).all()
    return [{"version": p.version, "sha256": p.sha256, "notes": p.notes,
             "created_at": p.created_at.isoformat() if p.created_at else None,
             "active": p.version == active} for p in rows]


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
        self._active: tuple[float, str] | None = None

    def current(self, db: Session) -> LoadedPolicy:
        now = time.monotonic()
        with self._lock:
            cached = self._active
        if cached is None or now - cached[0] > self.ttl:
            version = active_version(db)
            if version is None:
                raise PolicyError("no teaching policy is active; run `cognitive-twin policy "
                                  "import`")
            with self._lock:
                self._active = (now, version)
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
        POLICY_ACTIVE.clear()
        POLICY_ACTIVE.labels(version).set(1)
        return loaded

    def invalidate(self) -> None:
        with self._lock:
            self._active = None
