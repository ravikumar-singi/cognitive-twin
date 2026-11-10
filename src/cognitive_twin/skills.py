"""Skills graph and question bank.

Each subject ("track") is one JSON file in ``content/tracks``: a skills graph
with prerequisites and four-option questions at three difficulty levels. Skill
and item ids are unique across subjects. Files are validated and loaded once.
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
    audience: str = ""
    description: str = ""

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


DEFAULT_TRACK = "ml-foundations"


def _parse(raw: dict) -> Track:
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
        if not (len(item.options) == 4 and len(set(item.options)) == 4
                and 0 <= item.answer < 4 and item.difficulty in (1, 2, 3)):
            raise ValueError(f"item {item.id} is malformed")
    track = Track(raw["id"], raw["name"], skills, items, raw.get("audience", ""),
                  raw.get("description", ""))
    track.topological_order()  # validates the graph
    for sid in skills:
        if {i.difficulty for i in track.items_for(sid)} != {1, 2, 3}:
            raise ValueError(f"skill {sid} needs items at every difficulty")
    return track


# Display order: youngest audience first; subjects not listed follow alphabetically.
ORDER = ("middle-school-maths", "high-school-algebra", "general-science", "python-basics",
         "ml-foundations")


@lru_cache(maxsize=1)
def load_tracks() -> dict[str, Track]:
    """Every bundled subject. Skill and item ids are unique across all subjects, so a
    credential's skill id identifies its subject."""
    tracks: dict[str, Track] = {}
    seen_skills: set[str] = set()
    seen_items: set[str] = set()
    folder = resources.files("cognitive_twin").joinpath("content/tracks")
    def rank(f):
        stem = f.name.removesuffix(".json")
        return (ORDER.index(stem) if stem in ORDER else len(ORDER), f.name)
    for f in sorted(folder.iterdir(), key=rank):
        if not f.name.endswith(".json"):
            continue
        t = _parse(json.loads(f.read_text()))
        if t.id in tracks or seen_skills & set(t.skills) or seen_items & set(t.items):
            raise ValueError(f"track {t.id} reuses an id from another track")
        seen_skills |= set(t.skills)
        seen_items |= set(t.items)
        tracks[t.id] = t
    return tracks


def load_track(track_id: str = DEFAULT_TRACK) -> Track:
    return load_tracks()[track_id]


def track_of_skill(skill_id: str) -> Track | None:
    return next((t for t in load_tracks().values() if skill_id in t.skills), None)
