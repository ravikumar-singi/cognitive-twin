"""Simulated student population: the RL training environment.

RL teaching policies cannot be trained by experimenting on real students, so
they are trained here first (sim-to-real). Each simulated learner has hidden
state the teacher never sees directly:

* ``knowledge[skill]`` in [0, 1]
* ``aptitude``: global learning-speed multiplier
* ``modality_affinity``: how well each teaching style works for this learner
* ``fatigue`` in [0, 1], rising with work and falling with breaks

Learning follows a *desirable difficulty* curve: gains peak when an item is
slightly above the learner's current level, and are throttled when
prerequisites are weak or the learner is tired.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from ..skills import Track

MODALITIES = ("worked_example", "socratic_dialogue", "spatial_simulation")
DIFFICULTIES = (1, 2, 3)


def _sigmoid(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


@dataclass
class AttemptOutcome:
    correct: bool
    true_gain: float          # privileged signal, available only in simulation
    knowledge_before: float
    knowledge_after: float


@dataclass
class SimulatedLearner:
    track: Track
    rng: np.random.Generator
    knowledge: dict[str, float] = field(default_factory=dict)
    aptitude: float = 1.0
    modality_affinity: dict[str, float] = field(default_factory=dict)
    fatigue: float = 0.0
    fatigue_rate: float = 0.035

    @classmethod
    def sample(cls, track: Track, rng: np.random.Generator) -> SimulatedLearner:
        knowledge = {s: float(rng.uniform(0.0, 0.25)) for s in track.skills}
        # some learners arrive with prior knowledge of the foundations
        for s in track.skills:
            if not track.skills[s].prereqs and rng.random() < 0.3:
                knowledge[s] = float(rng.uniform(0.4, 0.7))
        favourite = MODALITIES[int(rng.integers(len(MODALITIES)))]
        affinity = {m: float(rng.uniform(0.6, 0.9)) for m in MODALITIES}
        affinity[favourite] = float(rng.uniform(1.3, 1.7))
        return cls(
            track=track,
            rng=rng,
            knowledge=knowledge,
            aptitude=float(rng.uniform(0.6, 1.4)),
            modality_affinity=affinity,
            fatigue_rate=float(rng.uniform(0.02, 0.05)),
        )

    # --- observable dynamics -------------------------------------------------

    def level(self, skill: str) -> float:
        """Learner's level on the item-difficulty scale (1..3)."""
        return 1.0 + 2.0 * self.knowledge[skill]

    def prereq_readiness(self, skill: str) -> float:
        prereqs = self.track.skills[skill].prereqs
        if not prereqs:
            return 1.0
        return min(self.knowledge[p] for p in prereqs)

    def p_correct(self, skill: str, difficulty: int) -> float:
        k = self.knowledge[skill]
        base = _sigmoid(8.0 * (k - (difficulty - 1) / 3.0 - 0.1))
        guess = 0.25  # four-option items
        return guess + (1 - guess - 0.08) * base * (1 - 0.35 * self.fatigue)

    def zone(self, skill: str, difficulty: int) -> float:
        """Desirable-difficulty multiplier, peaking ~0.5 above current level."""
        gap = difficulty - self.level(skill) - 0.5
        return math.exp(-(gap**2) / 0.9)

    def attempt(self, skill: str, difficulty: int, modality: str) -> AttemptOutcome:
        before = self.knowledge[skill]
        correct = bool(self.rng.random() < self.p_correct(skill, difficulty))
        readiness = self.prereq_readiness(skill)
        readiness_mult = 1.0 if readiness >= 0.6 else 0.15 + readiness
        gain = (
            0.20
            * self.aptitude
            * self.modality_affinity[modality]
            * self.zone(skill, difficulty)
            * readiness_mult
            * (1.0 - self.fatigue) ** 1.5
            * (1.0 - before)
        )
        if not correct:
            gain *= 0.6  # productive failure still teaches, but less
        after = min(1.0, before + gain)
        self.knowledge[skill] = after
        strain = 0.02 * max(0.0, difficulty - self.level(skill))
        self.fatigue = min(1.0, self.fatigue + self.fatigue_rate + strain)
        return AttemptOutcome(correct, after - before, before, after)

    def rest(self) -> None:
        self.fatigue *= 0.3

    # --- latent signals the edge sensor turns into features ------------------

    def latent_attention(self, skill: str, difficulty: int) -> float:
        return max(0.0, min(1.0, (1 - self.fatigue) * (0.45 + 0.55 * self.zone(skill, difficulty))))

    def latent_load(self, skill: str, difficulty: int) -> float:
        return max(0.0, min(1.0, 0.5 + (difficulty - self.level(skill)) / 3.0 + 0.3 * self.fatigue))

    # --- evaluation ------------------------------------------------------------

    def mastery_count(self, threshold: float = 0.8) -> int:
        return sum(1 for v in self.knowledge.values() if v >= threshold)

    def total_knowledge(self) -> float:
        return float(sum(self.knowledge.values()))
