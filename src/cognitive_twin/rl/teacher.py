"""Multi-agent teacher: coordinates the agents and runs teaching episodes."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import numpy as np

from ..edge.sensor import EdgeProcessor
from ..env.simulator import MODALITIES, SimulatedLearner
from ..skills import Item, Track
from ..twin import CognitiveTwin
from .agents import CurriculumAgent, PacingAgent, TutorAgent
from .bandit import LinUCB
from .shield import Guardian


@dataclass
class Decision:
    action: str                    # "teach" or "break"
    skill: str | None = None
    difficulty: int | None = None
    modality: str | None = None
    item: Item | None = None
    pacing_state: str = ""
    tutor_ctx: np.ndarray | None = None
    proposed_action: str = ""
    explanation: dict | None = None

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {"action": self.action, "proposed_action": self.proposed_action}
        if self.action == "teach":
            out.update(skill=self.skill, difficulty=self.difficulty, modality=self.modality,
                       item_id=self.item.id if self.item else None)
        return out


class Teacher(Protocol):
    name: str
    def act(self, twin: CognitiveTwin, sensing: bool, explore: bool) -> Decision: ...
    def learn(self, decision: Decision, gain: float) -> None: ...
    def end_episode(self) -> None: ...


def pick_item(track: Track, skill: str, difficulty: int, twin: CognitiveTwin,
              rng: np.random.Generator) -> Item:
    pool = [i for i in track.items_for(skill) if i.difficulty == difficulty]
    recent = {a.item_id for a in twin.attempts[-4:]}
    fresh = [i for i in pool if i.id not in recent] or pool
    return fresh[int(rng.integers(len(fresh)))]


@dataclass
class MultiAgentTeacher:
    track: Track
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(0))
    curriculum: CurriculumAgent | None = None
    tutor: TutorAgent = field(default_factory=TutorAgent)
    pacing: PacingAgent = field(default_factory=PacingAgent)
    guardian: Guardian = field(default_factory=Guardian)
    name: str = "multi-agent RL"

    def __post_init__(self) -> None:
        if self.curriculum is None:
            self.curriculum = CurriculumAgent(self.track.topological_order())

    def act(self, twin: CognitiveTwin, sensing: bool, explore: bool = False,
            force_continue: bool = False) -> Decision:
        """``force_continue`` lets a learner decline a suggested break."""
        p_state = self.pacing.state(twin)
        if force_continue:
            proposed = action = "continue"
        else:
            proposed = self.pacing.act(twin, explore, self.rng)
            action = self.guardian.pacing_override(twin, proposed, sensing)
        if action == "break":
            return Decision("break", pacing_state=p_state, proposed_action=proposed)
        allowed = self.guardian.allowed_skills(twin)
        if not allowed:  # everything mastered
            return Decision("break", pacing_state=p_state, proposed_action="done")
        skill = self.curriculum.act(twin, allowed, explore)
        diffs = self.guardian.allowed_difficulties(twin, skill)
        d, m, ctx = self.tutor.act(twin, skill, diffs, explore)
        item = pick_item(self.track, skill, d, twin, self.rng)
        return Decision("teach", skill, d, m, item, p_state, ctx, proposed)

    def learn(self, decision: Decision, gain: float) -> None:
        if decision.action == "teach":
            self.tutor.learn(decision.difficulty, decision.modality, decision.tutor_ctx, gain)
            self.curriculum.record(gain)
            self.pacing.record(decision.pacing_state, "continue", gain)
        elif decision.proposed_action != "done":
            self.pacing.record(decision.pacing_state, "break", 0.0)

    def end_episode(self) -> None:
        self.curriculum.end_episode()
        self.pacing.end_episode()

    # --- persistence -----------------------------------------------------------

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps({
            "track": self.track.id,
            "curriculum": self.curriculum.model.to_dict(),
            "curriculum_skills": self.curriculum.skills,
            "tutor": self.tutor.model.to_dict(),
            "pacing_q": self.pacing.q,
        }))

    @classmethod
    def load(cls, path: str | Path, track: Track, seed: int = 0) -> MultiAgentTeacher:
        d = json.loads(Path(path).read_text())
        cur = CurriculumAgent(d["curriculum_skills"])
        cur.model = LinUCB.from_dict(d["curriculum"])
        tutor = TutorAgent()
        tutor.model = LinUCB.from_dict(d["tutor"])
        pacing = PacingAgent(q={k: list(v) for k, v in d["pacing_q"].items()})
        return cls(track, np.random.default_rng(seed), cur, tutor, pacing)


# --- baselines -----------------------------------------------------------------

@dataclass
class RandomTeacher:
    track: Track
    rng: np.random.Generator
    name: str = "random"

    def act(self, twin: CognitiveTwin, sensing: bool, explore: bool = False) -> Decision:
        skill = list(self.track.skills)[int(self.rng.integers(len(self.track.skills)))]
        d = int(self.rng.integers(1, 4))
        m = MODALITIES[int(self.rng.integers(len(MODALITIES)))]
        return Decision("teach", skill, d, m, pick_item(self.track, skill, d, twin, self.rng))

    def learn(self, decision: Decision, gain: float) -> None: ...
    def end_episode(self) -> None: ...


@dataclass
class SyllabusTeacher:
    """A conventional fixed syllabus: topological order, fixed time per skill,
    medium difficulty, worked examples, a break every 15 steps."""

    track: Track
    rng: np.random.Generator
    steps_per_skill: int = 8
    name: str = "fixed syllabus"

    def act(self, twin: CognitiveTwin, sensing: bool, explore: bool = False) -> Decision:
        if twin.steps_since_break >= 15:
            return Decision("break")
        order = self.track.topological_order()
        teach_steps = len(twin.attempts)
        skill = order[min(teach_steps // self.steps_per_skill, len(order) - 1)]
        return Decision("teach", skill, 2, "worked_example",
                        pick_item(self.track, skill, 2, twin, self.rng))

    def learn(self, decision: Decision, gain: float) -> None: ...
    def end_episode(self) -> None: ...


# --- episode runner ------------------------------------------------------------

@dataclass
class EpisodeResult:
    teacher: str
    steps: int
    breaks: int
    final_knowledge: float
    mastered_skills: int
    accuracy: float


def run_episode(teacher: Teacher, learner: SimulatedLearner, twin: CognitiveTwin,
                edge: EdgeProcessor, steps: int, learn: bool, reward: str = "oracle"
                ) -> EpisodeResult:
    """Teach one simulated learner for ``steps`` steps.

    ``reward="oracle"`` uses the simulator's true knowledge gain (training in
    simulation); ``reward="belief"`` uses the twin's change in belief, the only
    signal available with real learners.
    """
    breaks, correct = 0, 0
    probe_skill = twin.track.topological_order()[0]
    for _ in range(steps):
        features = edge.sense(learner, probe_skill, 2)
        twin.observe_state(features)
        decision = teacher.act(twin, edge.active, explore=learn)
        if decision.action == "break":
            learner.rest()
            twin.observe_break()
            breaks += 1
            gain = 0.0
        else:
            probe_skill = decision.skill
            out = learner.attempt(decision.skill, decision.difficulty, decision.modality)
            belief_gain = twin.observe_answer(decision.item.id, decision.skill,
                                              decision.difficulty, decision.modality, out.correct)
            gain = out.true_gain if reward == "oracle" else max(belief_gain, 0.0)
            correct += out.correct
        if learn:
            teacher.learn(decision, gain)
    teacher.end_episode()
    taught = steps - breaks
    return EpisodeResult(teacher.name, steps, breaks, learner.total_knowledge(),
                         learner.mastery_count(), correct / taught if taught else 0.0)
