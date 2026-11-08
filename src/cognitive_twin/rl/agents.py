"""The three cooperating RL agents.

They form a hierarchy over one shared objective, the learner's knowledge gain:

* ``CurriculumAgent`` (high level, temporally extended): which skill to work
  on for the next few steps. LinUCB over skills.
* ``TutorAgent`` (low level, every step): which difficulty and teaching
  modality to use. LinUCB over the 9 (difficulty x modality) arms.
* ``PacingAgent`` (every step): continue or take a break. Tabular Q-learning,
  because the value of a break only shows up in later steps.

Each agent sees the twin (belief state) and is restricted by the Guardian.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np

from ..env.simulator import DIFFICULTIES, MODALITIES
from ..twin import CognitiveTwin
from .bandit import LinUCB

REWARD_SCALE = 10.0
TUTOR_ARMS = list(itertools.product(DIFFICULTIES, MODALITIES))


def _recent_accuracy(twin: CognitiveTwin, skill: str) -> float:
    recent = twin.recent_for(skill)
    return sum(a.correct for a in recent) / len(recent) if recent else 0.5


class CurriculumAgent:
    DIM = 6
    OPTION_LENGTH = 4

    def __init__(self, skills: list[str], alpha: float = 0.5):
        self.skills = skills
        self.model = LinUCB(len(skills), self.DIM, alpha)
        self.current: str | None = None
        self._ctx: np.ndarray | None = None
        self._gains: list[float] = []

    def context(self, twin: CognitiveTwin) -> np.ndarray:
        rows = []
        for s in self.skills:
            p = twin.p_known[s]
            rows.append([1.0, p, p * (1 - p), twin.prereq_min(s),
                         1.0 if s == self.current else 0.0, _recent_accuracy(twin, s)])
        return np.array(rows)

    def needs_decision(self, twin: CognitiveTwin) -> bool:
        return (self.current is None or len(self._gains) >= self.OPTION_LENGTH
                or twin.p_known[self.current] >= 0.95)

    def act(self, twin: CognitiveTwin, allowed: list[str], explore: bool) -> str:
        if not self.needs_decision(twin) and self.current in allowed:
            return self.current
        self._close_option()
        ctx = self.context(twin)
        idx = [self.skills.index(s) for s in allowed]
        arm = self.model.select(ctx, idx, explore)
        self.current, self._ctx = self.skills[arm], ctx[arm]
        return self.current

    def record(self, gain: float) -> None:
        self._gains.append(gain)

    def _close_option(self) -> None:
        if self.current is not None and self._ctx is not None and self._gains:
            reward = REWARD_SCALE * float(np.mean(self._gains))
            self.model.update(self.skills.index(self.current), self._ctx, reward)
        self._gains = []

    def end_episode(self) -> None:
        self._close_option()
        self.current, self._ctx = None, None


class TutorAgent:
    DIM = 7

    def __init__(self, alpha: float = 0.5):
        self.model = LinUCB(len(TUTOR_ARMS), self.DIM, alpha)

    def context(self, twin: CognitiveTwin, skill: str) -> np.ndarray:
        p = twin.p_known[skill]
        x = np.array([1.0, p, p * p, twin.attention, twin.load,
                      twin.fatigue_estimate(), _recent_accuracy(twin, skill)])
        return np.tile(x, (len(TUTOR_ARMS), 1))

    def act(self, twin: CognitiveTwin, skill: str, allowed_difficulties: list[int],
            explore: bool) -> tuple[int, str, np.ndarray]:
        ctx = self.context(twin, skill)
        allowed = [i for i, (d, _) in enumerate(TUTOR_ARMS) if d in allowed_difficulties]
        arm = self.model.select(ctx, allowed, explore)
        d, m = TUTOR_ARMS[arm]
        return d, m, ctx[arm]

    def learn(self, difficulty: int, modality: str, ctx: np.ndarray, gain: float) -> None:
        self.model.update(TUTOR_ARMS.index((difficulty, modality)), ctx, REWARD_SCALE * gain)


@dataclass
class PacingAgent:
    lr: float = 0.15
    gamma: float = 0.9
    epsilon: float = 0.2
    q: dict[str, list[float]] = field(default_factory=dict)
    ACTIONS = ("continue", "break")

    def __post_init__(self) -> None:
        self._pending: tuple[str, int, float] | None = None

    @staticmethod
    def state(twin: CognitiveTwin) -> str:
        fatigue_bucket = min(4, twin.steps_since_break // 5)
        attention_bucket = 0 if twin.attention < 0.35 else (1 if twin.attention < 0.6 else 2)
        return f"f{fatigue_bucket}-a{attention_bucket}"

    def _q(self, s: str) -> list[float]:
        return self.q.setdefault(s, [0.0, 0.0])

    def act(self, twin: CognitiveTwin, explore: bool, rng: np.random.Generator) -> str:
        s = self.state(twin)
        if self._pending is not None:  # complete the previous transition
            ps, pa, pr = self._pending
            target = pr + self.gamma * max(self._q(s))
            self._q(ps)[pa] += self.lr * (target - self._q(ps)[pa])
            self._pending = None
        if explore and rng.random() < self.epsilon:
            return self.ACTIONS[int(rng.integers(2))]
        values = self._q(s)
        return self.ACTIONS[int(np.argmax(values))]

    def record(self, twin_state: str, action: str, gain: float) -> None:
        self._pending = (twin_state, self.ACTIONS.index(action), REWARD_SCALE * gain)

    def end_episode(self) -> None:
        if self._pending is not None:
            ps, pa, pr = self._pending
            self._q(ps)[pa] += self.lr * (pr - self._q(ps)[pa])
            self._pending = None
