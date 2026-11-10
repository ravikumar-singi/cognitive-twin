"""Guardian: a safety shield between the RL agents and the learner.

Shielded RL (Alshiekh et al., 2018) lets agents explore freely inside a set of
actions that are known to be safe. The Guardian computes that set before each
agent acts and records every time it had to override or narrow a choice, so
educators can audit what the agents were stopped from doing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..twin import MASTERY, CognitiveTwin

MAX_STEPS_WITHOUT_BREAK = 25
MIN_STEPS_BEFORE_BREAK = 3
LOW_ATTENTION = 0.25
NOVICE = 0.3


@dataclass
class Intervention:
    step: int
    agent: str
    rule: str
    detail: str

    def as_dict(self) -> dict[str, object]:
        return self.__dict__.copy()


@dataclass
class Guardian:
    log: list[Intervention] = field(default_factory=list)
    # Longest run of questions before a break is required; lower for younger learners.
    max_steps: int = MAX_STEPS_WITHOUT_BREAK

    def _note(self, twin: CognitiveTwin, agent: str, rule: str, detail: str) -> None:
        self.log.append(Intervention(twin.step, agent, rule, detail))

    # --- Curriculum agent ---------------------------------------------------

    def allowed_skills(self, twin: CognitiveTwin) -> list[str]:
        frontier = twin.frontier()
        if frontier:
            return frontier
        open_skills = [s for s in twin.track.skills if twin.p_known[s] < MASTERY]
        if not open_skills:
            return []
        best = max(open_skills, key=twin.prereq_min)
        self._note(twin, "curriculum", "frontier-empty",
                   f"no skill had ready prerequisites; allowing {best} only")
        return [best]

    # --- Tutor agent ----------------------------------------------------------

    def allowed_difficulties(self, twin: CognitiveTwin, skill: str) -> list[int]:
        if twin.p_known[skill] < NOVICE:
            return [1, 2]
        return [1, 2, 3]

    # --- Pacing agent ---------------------------------------------------------

    def pacing_override(self, twin: CognitiveTwin, proposed: str, sensing: bool) -> str:
        if proposed == "break" and twin.steps_since_break < MIN_STEPS_BEFORE_BREAK:
            self._note(twin, "pacing", "min-work", "break too soon; continuing")
            return "continue"
        if proposed == "continue" and twin.steps_since_break >= self.max_steps:
            self._note(twin, "pacing", "max-session",
                       f"{twin.steps_since_break} steps without a break; forcing a break")
            return "break"
        if (proposed == "continue" and sensing and twin.attention < LOW_ATTENTION
                and twin.steps_since_break >= MIN_STEPS_BEFORE_BREAK):
            self._note(twin, "pacing", "low-attention",
                       f"attention {twin.attention:.2f}; forcing a break")
            return "break"
        return proposed
