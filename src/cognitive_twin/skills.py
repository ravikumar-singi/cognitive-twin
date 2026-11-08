"""Skills graph and question bank.

The MVP ships one track, *Machine Learning Foundations*, so the full journey
can run end to end with auto-gradable items. Content lives in
``content/ml_foundations.json`` and is loaded once.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from functools import lru_cache
from importlib import resources


@dataclass(frozen=True)
class Item:
    id: str
    skill: str
    difficulty: int  # 1 (intro) .. 3 (stretch)
    prompt: str
    options: tuple[str, ...]
    answer: int  # index into options
    hint: str


@dataclass(frozen=True)
class Skill:
    id: str
    name: str
    prereqs: tuple[str, ...] = field(default_factory=tuple)
    rubric: str = ""


@dataclass(frozen=True)
class Track:
    id: str
    name: str
    skills: dict[str, Skill]
    items: dict[str, Item]

    def items_for(self, skill_id: str) -> list[Item]:
        return [i for i in self.items.values() if i.skill == skill_id]

    def topological_order(self) -> list[str]:
        """Skills ordered so every prerequisite comes before its dependants."""
        order: list[str] = []
        seen: set[str] = set()

        def visit(sid: str, stack: tuple[str, ...] = ()) -> None:
            if sid in seen:
                return
            if sid in stack:
                raise ValueError(f"cycle in skills graph at {sid}")
            for p in self.skills[sid].prereqs:
                visit(p, stack + (sid,))
            seen.add(sid)
            order.append(sid)

        for sid in self.skills:
            visit(sid)
        return order


@lru_cache(maxsize=1)
def load_track() -> Track:
    raw = json.loads(
        resources.files("cognitive_twin").joinpath("content/ml_foundations.json").read_text()
    )
    skills = {
        s["id"]: Skill(s["id"], s["name"], tuple(s.get("prereqs", [])), s.get("rubric", ""))
        for s in raw["skills"]
    }
    items = {
        i["id"]: Item(
            i["id"], i["skill"], i["difficulty"], i["prompt"], tuple(i["options"]),
            i["answer"], i.get("hint", ""),
        )
        for i in raw["items"]
    }
    for item in items.values():
        if item.skill not in skills:
            raise ValueError(f"item {item.id} references unknown skill {item.skill}")
    track = Track(raw["id"], raw["name"], skills, items)
    track.topological_order()  # validates the graph
    return track
