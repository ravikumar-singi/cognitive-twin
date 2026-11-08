"""Session layer shared by the API and the CLI.

One trained policy is shared by every learner (the agents' models), while each
learner gets their own agent instances for per-session state such as the
current curriculum option, the pending pacing transition and the Guardian's
audit log. Live sessions do not update the shared policy: policies are
retrained offline and redeployed, which keeps behaviour reviewable.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import numpy as np

from .consent import ConsentProfile
from .edge.sensor import EdgeProcessor, NeuralBand
from .env.simulator import SimulatedLearner
from .ledger.ledger import Ledger
from .rl.agents import CurriculumAgent, PacingAgent, TutorAgent
from .rl.shield import Guardian
from .rl.teacher import Decision, MultiAgentTeacher
from .skills import Track, load_track
from .twin import CognitiveTwin


def bundled_policy_path() -> Path:
    return Path(str(resources.files("cognitive_twin").joinpath("content/policy.json")))


def load_policy(track: Track) -> MultiAgentTeacher:
    path = Path(os.environ.get("CT_POLICY_PATH", bundled_policy_path()))
    if path.exists():
        return MultiAgentTeacher.load(path, track)
    from .rl.train import train
    teacher, _ = train(episodes=300, track=track)
    return teacher


@dataclass
class LearnerSession:
    id: str
    name: str
    consent: ConsentProfile
    twin: CognitiveTwin
    teacher: MultiAgentTeacher
    edge: EdgeProcessor
    simulated: SimulatedLearner | None
    pending: Decision | None = None
    credentials: list[str] = field(default_factory=list)
    log: list[dict] = field(default_factory=list)


class Platform:
    def __init__(self, seed: int = 11):
        self.track = load_track()
        self.policy = load_policy(self.track)
        self.ledger = Ledger.with_default_dao()
        self.rng = np.random.default_rng(seed)
        self.sessions: dict[str, LearnerSession] = {}

    def _teacher_for_session(self) -> MultiAgentTeacher:
        cur = CurriculumAgent(self.policy.curriculum.skills)
        cur.model = self.policy.curriculum.model
        tutor = TutorAgent()
        tutor.model = self.policy.tutor.model
        pacing = PacingAgent(q=self.policy.pacing.q)
        return MultiAgentTeacher(self.track, np.random.default_rng(int(self.rng.integers(1 << 31))),
                                 cur, tutor, pacing, Guardian())

    def enrol(self, name: str, consent: ConsentProfile, simulated: bool = True) -> LearnerSession:
        if not consent.may_enrol:
            raise PermissionError("guardian consent is required for learners under 18")
        sid = "lrn_" + secrets.token_hex(4)
        sim = SimulatedLearner.sample(self.track, self.rng) if simulated else None
        # A real deployment pairs a physical band; the demo pairs the simulated one.
        band = NeuralBand(self.rng) if simulated else None
        session = LearnerSession(sid, name, consent, CognitiveTwin(sid, self.track),
                                 self._teacher_for_session(), EdgeProcessor(consent, band), sim)
        self.sessions[sid] = session
        return session

    def get(self, sid: str) -> LearnerSession:
        if sid not in self.sessions:
            raise KeyError(sid)
        return self.sessions[sid]

    # --- teaching loop -------------------------------------------------------------

    def next_decision(self, s: LearnerSession) -> Decision:
        if s.simulated is not None:
            attempts = s.twin.attempts
            probe = attempts[-1].skill if attempts else self.track.topological_order()[0]
            s.twin.observe_state(s.edge.sense(s.simulated, probe, 2))
        s.pending = s.teacher.act(s.twin, s.edge.active, explore=False)
        return s.pending

    def answer(self, s: LearnerSession, item_id: str, choice: int) -> dict:
        d = s.pending
        if d is None or d.action != "teach" or d.item is None or d.item.id != item_id:
            raise ValueError("no pending item with that id; call next first")
        correct = choice == d.item.answer
        delta = s.twin.observe_answer(d.item.id, d.skill, d.difficulty, d.modality, correct)
        entry = {"step": s.twin.step, **d.as_dict(), "correct": correct,
                 "belief_delta": round(delta, 4)}
        s.log.append(entry)
        s.pending = None
        return {"correct": correct, "answer": d.item.answer, "hint": d.item.hint,
                "belief": round(s.twin.p_known[d.skill], 3), **entry}

    def take_break(self, s: LearnerSession) -> None:
        if s.simulated is not None:
            s.simulated.rest()
        s.twin.observe_break()
        s.log.append({"step": s.twin.step, "action": "break",
                      "proposed_action": s.pending.proposed_action if s.pending else ""})
        s.pending = None

    def autoplay(self, s: LearnerSession, steps: int) -> list[dict]:
        if s.simulated is None:
            raise ValueError("autoplay needs a simulated learner")
        out = []
        for _ in range(steps):
            d = self.next_decision(s)
            if d.action == "break":
                self.take_break(s)
                out.append(s.log[-1])
                continue
            outcome = s.simulated.attempt(d.skill, d.difficulty, d.modality)
            choice = d.item.answer if outcome.correct else (d.item.answer + 1) % 4
            r = self.answer(s, d.item.id, choice)
            out.append({k: r[k] for k in ("step", "skill", "difficulty", "modality",
                                          "correct", "belief")})
        return out

    def request_credential(self, s: LearnerSession, skill: str):
        if skill not in self.track.skills:
            raise KeyError(skill)
        res = self.ledger.request_credential(s.twin, skill)
        if res.issued:
            s.credentials.append(res.credential["id"])
        return res
