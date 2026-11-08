"""The Cognitive Twin: the learner model every agent acts on.

The teacher never sees a learner's true knowledge. It holds a *belief*,
updated with Bayesian Knowledge Tracing (BKT) from answers, plus smoothed
state features from the edge device. Teaching is therefore a POMDP and the
twin is the belief state the RL agents observe.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .edge.sensor import StateFeatures
from .skills import Track

# BKT parameters; guess and slip vary with item difficulty. Calibrated against
# the simulator so that belief-mastery is not over-confident (see docs/rl-design.md).
P_INIT = 0.15
P_LEARN = 0.05
GUESS = {1: 0.50, 2: 0.30, 3: 0.25}
SLIP = {1: 0.08, 2: 0.12, 3: 0.20}
MASTERY = 0.95


@dataclass
class Attempt:
    item_id: str
    skill: str
    difficulty: int
    modality: str
    correct: bool
    step: int


@dataclass
class CognitiveTwin:
    learner_id: str
    track: Track
    p_known: dict[str, float] = field(default_factory=dict)
    attention: float = 0.5
    load: float = 0.5
    steps_since_break: int = 0
    step: int = 0
    attempts: list[Attempt] = field(default_factory=list)

    def __post_init__(self) -> None:
        for s in self.track.skills:
            self.p_known.setdefault(s, P_INIT)

    # --- updates -----------------------------------------------------------

    def observe_state(self, features: StateFeatures, alpha: float = 0.4) -> None:
        w = alpha * max(features.signal_quality, 0.0)
        self.attention = (1 - w) * self.attention + w * features.attention
        self.load = (1 - w) * self.load + w * features.cognitive_load

    def observe_answer(self, item_id: str, skill: str, difficulty: int, modality: str,
                       correct: bool) -> float:
        """BKT posterior + learning transition. Returns the change in belief."""
        p = self.p_known[skill]
        g, s = GUESS[difficulty], SLIP[difficulty]
        if correct:
            post = p * (1 - s) / (p * (1 - s) + (1 - p) * g)
        else:
            post = p * s / (p * s + (1 - p) * (1 - g))
        new = post + (1 - post) * P_LEARN
        self.p_known[skill] = new
        self.step += 1
        self.steps_since_break += 1
        self.attempts.append(Attempt(item_id, skill, difficulty, modality, correct, self.step))
        return new - p

    def observe_break(self) -> None:
        self.steps_since_break = 0
        self.step += 1

    # --- queries -----------------------------------------------------------

    def prereq_min(self, skill: str) -> float:
        prereqs = self.track.skills[skill].prereqs
        return min((self.p_known[p] for p in prereqs), default=1.0)

    def frontier(self, readiness: float = 0.7) -> list[str]:
        """Skills not yet mastered whose prerequisites look learned."""
        return [
            s for s in self.track.topological_order()
            if self.p_known[s] < MASTERY and self.prereq_min(s) >= readiness
        ]

    def mastered(self) -> list[str]:
        return [s for s, p in self.p_known.items() if p >= MASTERY]

    def fatigue_estimate(self) -> float:
        return min(1.0, self.steps_since_break / 20.0)

    def recent_for(self, skill: str, n: int = 6) -> list[Attempt]:
        return [a for a in self.attempts if a.skill == skill][-n:]

    def snapshot(self) -> dict[str, object]:
        return {
            "learner_id": self.learner_id,
            "step": self.step,
            "p_known": {k: round(v, 3) for k, v in self.p_known.items()},
            "mastered": self.mastered(),
            "frontier": self.frontier(),
            "attention": round(self.attention, 3),
            "load": round(self.load, 3),
            "fatigue_estimate": round(self.fatigue_estimate(), 3),
        }
