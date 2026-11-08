"""Plain-language explanations of what the agents decided and why.

Every decision the learner sees comes with a short, jargon-free reason, so the
system never feels like a black box. Wording is deliberately simple:
"how sure we are you know this" rather than "BKT posterior".
"""

from __future__ import annotations

from .ledger.ledger import Evidence, meets_rubric
from .rl.shield import MAX_STEPS_WITHOUT_BREAK
from .rl.teacher import Decision
from .twin import MASTERY, CognitiveTwin

MODALITY_LABEL = {
    "worked_example": "Worked example",
    "socratic_dialogue": "Think it through",
    "spatial_simulation": "Picture it",
}
MODALITY_WHY = {
    "worked_example": "a worked example, with a tip shown up front",
    "socratic_dialogue": "a think-it-through question, with a nudge if you need one",
    "spatial_simulation": "a picture-it question that asks you to visualise the idea",
}
DIFFICULTY_LABEL = {1: "Warm-up", 2: "Core", 3: "Stretch"}


def pct(x: float) -> int:
    return round(100 * x)


def explain(decision: Decision, twin: CognitiveTwin) -> dict[str, object]:
    if decision.action == "break":
        if decision.proposed_action == "done":
            return {"headline": "Every skill is mastered. Brilliant work.", "reasons": []}
        mins = 1 if twin.steps_since_break < MAX_STEPS_WITHOUT_BREAK else 2
        reasons = [f"You've answered {twin.steps_since_break} questions since your last pause."]
        if twin.attention < 0.35:
            reasons.append("Your focus looks like it's dipping.")
        reasons.append("A short pause helps new ideas stick, so the next questions go better.")
        return {"headline": f"Time for a {mins}-minute break", "reasons": reasons,
                "break_seconds": 60 * mins}

    skill = twin.track.skills[decision.skill]
    known = twin.p_known[decision.skill]
    reasons = []
    if not twin.recent_for(decision.skill, 1):
        prereqs = [twin.track.skills[p].name for p in skill.prereqs]
        reasons.append(f"You're ready for {skill.name}"
                       + (f": your {', '.join(prereqs)} looks solid." if prereqs else "."))
    else:
        reasons.append(f"We're {pct(known)}% sure you know {skill.name}; "
                       f"the goal is {pct(MASTERY)}%.")
    level = DIFFICULTY_LABEL[decision.difficulty]
    if decision.difficulty == 3:
        reasons.append("This is a stretch question. Slightly-too-hard questions teach the most.")
    elif decision.difficulty == 1:
        reasons.append("A warm-up question to build confidence first.")
    else:
        reasons.append("A core question pitched at about your level.")
    reasons.append(f"It's shown as {MODALITY_WHY[decision.modality]}, "
                   "a style that has worked well for learners like you.")
    return {
        "headline": f"{level} question on {skill.name}",
        "reasons": reasons,
        "difficulty_label": level,
        "modality_label": MODALITY_LABEL[decision.modality],
    }


def skill_statuses(twin: CognitiveTwin, credentialed: set[str]) -> list[dict[str, object]]:
    out = []
    frontier = set(twin.frontier())
    for sid in twin.track.topological_order():
        s = twin.track.skills[sid]
        known = twin.p_known[sid]
        attempts = twin.recent_for(sid, 1000)
        if sid in credentialed:
            status, note = "credentialed", "Signed and recorded on the ledger"
        elif known >= MASTERY:
            ok, why = meets_rubric(Evidence.from_twin(twin, sid))
            status = "mastered"
            note = "Ready to claim your credential" if ok else f"Almost there: {_friendly(why)}"
        elif attempts:
            n = len(attempts)
            status, note = "learning", f"{n} question{'s' if n != 1 else ''} so far"
        elif sid in frontier:
            status, note = "ready", "Ready to start"
        else:
            missing = [twin.track.skills[p].name for p in s.prereqs if twin.p_known[p] < 0.7]
            status, note = "locked", "Unlocks after " + ", ".join(missing)
        out.append({
            "id": sid, "name": s.name, "status": status, "note": note,
            "known_pct": pct(known), "goal_pct": pct(MASTERY), "started": bool(attempts),
            "can_claim": status == "mastered" and note.startswith("Ready"),
            "prereqs": [twin.track.skills[p].name for p in s.prereqs],
        })
    return out


def _friendly(reason: str) -> str:
    if "difficulty 2+" in reason:
        return "answer a few more core or stretch questions correctly"
    if "accuracy" in reason:
        return "get a few more recent answers right"
    return reason
