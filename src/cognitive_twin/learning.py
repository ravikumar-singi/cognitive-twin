"""Learning service: the teaching loop, backed by the database.

Nothing about a learner is held in process memory between requests (R1.3,
R1.4). Each request loads the learner's twin row under a row lock, rebuilds
the agents from the active policy version, acts, and writes the result back
in the same transaction. Any instance can serve any request.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

import numpy as np
from prometheus_client import Counter
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from .config import Settings
from .consent import ConsentProfile
from .db.models import Attempt as AttemptRow
from .db.models import AuditEvent, ConsentRecord, GuardianIntervention, School, Twin, User, utcnow
from .db.models import Decision as DecisionRow
from .edge.sensor import NEUTRAL
from .explain import explain, skill_statuses
from .keys import KeyRegistry
from .ledger.service import IssueOutcome, LedgerService
from .policy_store import PolicyCache
from .rl.shield import Guardian
from .rl.teacher import Decision, MultiAgentTeacher
from .skills import Track
from .twin import Attempt, CognitiveTwin

TEACHING_STEPS = Counter("ct_teaching_steps_total", "Teaching decisions served", ["action"])
GUARDIAN_INTERVENTIONS = Counter("ct_guardian_interventions_total",
                                 "Guardian safety-shield interventions", ["agent", "rule"])
ANSWERS = Counter("ct_answers_total", "Answers submitted", ["correct"])

# Live human sessions space break prompts out and respect "keep going".
HUMAN_MIN_QUESTIONS_BETWEEN_BREAKS = 8
DECLINED_BREAK_COOLDOWN = 6

# Age bands come from school records. Younger learners get shorter stretches between
# breaks: (questions before a break may be suggested, questions before one is required).
BANDS = ("under13", "13to17", "adult")
BREAK_RULES = {"under13": (5, 12), "13to17": (6, 18), "adult": (8, 25)}
MINOR_POLICY_TEXT = (
    "How Cognitive Twin uses your information.\n\n"
    "Your school has set this up for you, and a parent or guardian has agreed with the "
    "school that you can use it.\n\n"
    "We keep your first name, your answers and which skills you are learning, so the tutors "
    "can pick the right questions for you and your teacher can see how you are doing. We "
    "never show you adverts and we never sell or share your information.\n\n"
    "You, your parent or guardian, or your teacher can ask to see or delete your "
    "information at any time. Just ask your teacher."
)


def band_rank(band: str) -> int:
    return BANDS.index(band) if band in BANDS else -1

LEARNING_POLICY_TEXT = (
    "Cognitive Twin pilot: learning data consent.\n\n"
    "We store your answers, the skills you are working on and the tutors' decisions, so the "
    "tutors can adapt to you and so you can earn credentials. Your institution provides your "
    "name, e-mail address, region and cohort when you sign in.\n\n"
    "Credentials are recorded under a random pseudonymous identifier. You can download or "
    "delete your data at any time. Deleting removes your profile and learning history; any "
    "credentials you earned stay on the ledger and keep verifying, but nothing links them to "
    "you any more.\n\n"
    "The focus headband is not used in this pilot."
)
NEURAL_POLICY_TEXT = (
    "Not active in this pilot. We record your choice now so it can be honoured if a later "
    "phase enables the headband after a separate review."
)


def policy_sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class ConsentRequired(PermissionError):
    pass


class NotEligible(PermissionError):
    pass


class Conflict(ValueError):
    pass


class NotFound(LookupError):
    pass


@dataclass
class Ctx:
    """Who is acting, for audit purposes."""
    actor: User | None
    ip: str | None = None
    request_id: str | None = None


def audit(db: Session, ctx: Ctx, action: str, target: str | None, outcome: str = "ok",
          **details) -> None:
    db.add(AuditEvent(actor_id=ctx.actor.id if ctx.actor else None,
                      actor_role=ctx.actor.role if ctx.actor else None, action=action,
                      target=target, outcome=outcome, ip=ctx.ip, request_id=ctx.request_id,
                      details=details))


class LearningService:
    def __init__(self, db: Session, settings: Settings, policies: PolicyCache, track: Track,
                 keys: KeyRegistry):
        self.db = db
        self.settings = settings
        self.policies = policies
        self.track = track
        self.keys = keys
        self.ledger = LedgerService(db, keys)

    # --- consent ----------------------------------------

    def latest_consent(self, user_id: str, scope: str) -> ConsentRecord | None:
        return self.db.scalar(select(ConsentRecord)
                              .where(ConsentRecord.user_id == user_id,
                                     ConsentRecord.scope == scope)
                              .order_by(ConsentRecord.id.desc()).limit(1))

    def policy_text(self, user: User) -> str:
        return LEARNING_POLICY_TEXT if user.age_band == "adult" else MINOR_POLICY_TEXT

    def consent_status(self, user: User) -> dict:
        learning = self.latest_consent(user.id, "learning")
        neural = self.latest_consent(user.id, "neural_sensing")
        current = self.settings.consent_policy_version
        return {
            "policy_version": current,
            "age_band": user.age_band,
            "not_eligible_reason": self.not_eligible_reason(user),
            "policy_text": self.policy_text(user),
            "neural_policy_text": NEURAL_POLICY_TEXT,
            "learning_granted": bool(learning and learning.granted
                                     and learning.policy_version == current),
            "learning_needs_renewal": bool(learning and learning.granted
                                           and learning.policy_version != current),
            "neural_sensing_granted": bool(neural and neural.granted),
            "neural_sensing_available": self.settings.neural_enabled,
            "eligible": self.eligible(user),
        }

    def not_eligible_reason(self, user: User) -> str | None:
        """Why this learner can't start yet, in words a learner can act on. None = OK.

        Everything here comes from school records: the age band, the guardian's consent
        for under-18s, and the youngest age group the school has chosen to enrol.
        """
        if user.age_band not in BANDS:
            return "Your teacher still needs to approve your account."
        school = self.db.get(School, user.school_id) if user.school_id else None
        youngest = school.youngest_band if school else "adult"
        if band_rank(user.age_band) < band_rank(youngest):
            return "Your school hasn't opened Cognitive Twin to your age group."
        if user.age_band != "adult" and not user.guardian_ok:
            return ("Your school needs to confirm that a parent or guardian has agreed. "
                    "Please ask your teacher.")
        return None

    def eligible(self, user: User) -> bool:
        return self.not_eligible_reason(user) is None

    def record_consent(self, ctx: Ctx, user: User, learning: bool, neural_sensing: bool) -> dict:
        reason = self.not_eligible_reason(user)
        if reason:
            raise NotEligible(reason)
        if user.age_band != "adult":
            neural_sensing = False  # never recorded for children
        v = self.settings.consent_policy_version
        self.db.add(ConsentRecord(user_id=user.id, scope="learning", granted=learning,
                                  policy_version=v,
                                  policy_text_sha256=policy_sha(self.policy_text(user)),
                                  source="learner"))
        self.db.add(ConsentRecord(user_id=user.id, scope="neural_sensing",
                                  granted=neural_sensing, policy_version=v,
                                  policy_text_sha256=policy_sha(NEURAL_POLICY_TEXT),
                                  source="learner"))
        audit(self.db, ctx, "consent.record", user.id, learning=learning,
              neural_sensing=neural_sensing, policy_version=v)
        self.db.flush()
        return self.consent_status(user)

    def consent_history(self, user_id: str) -> list[dict]:
        rows = self.db.scalars(select(ConsentRecord).where(ConsentRecord.user_id == user_id)
                               .order_by(ConsentRecord.id)).all()
        return [{"scope": r.scope, "granted": r.granted, "policy_version": r.policy_version,
                 "policy_text_sha256": r.policy_text_sha256, "source": r.source,
                 "at": r.created_at.isoformat()} for r in rows]

    def profile(self, user: User) -> ConsentProfile:
        neural = self.latest_consent(user.id, "neural_sensing")
        age = {"adult": 18, "13to17": 15, "under13": 10}.get(user.age_band, 0)
        return ConsentProfile(age=age, guardian_consent=user.guardian_ok,
                              region=user.region,
                              neural_opt_in=bool(neural and neural.granted)
                              and self.settings.neural_enabled)

    def require_learning(self, user: User) -> None:
        reason = self.not_eligible_reason(user)
        if reason:
            raise NotEligible(reason)
        c = self.latest_consent(user.id, "learning")  # one query on the hot path
        if not (c and c.granted and c.policy_version == self.settings.consent_policy_version):
            raise ConsentRequired("learning consent for the current policy is required")

    # --- twin load/save ----------------------------------------------------------------------

    def _twin_row(self, user: User, lock: bool) -> Twin:
        q = select(Twin).where(Twin.user_id == user.id, Twin.track_id == self.track.id)
        if lock:
            q = q.with_for_update()
        row = self.db.scalar(q)
        if row is None:
            seed = CognitiveTwin(user.id, self.track)
            row = Twin(user_id=user.id, track_id=self.track.id, p_known=dict(seed.p_known),
                       attention=0.5, load=0.5,
                       steps_since_break=0, step=0, break_declined_at=-100, agent_state={},
                       pending=None)
            self.db.add(row)
            self.db.flush()
        return row

    def _domain(self, user: User, row: Twin) -> CognitiveTwin:
        twin = CognitiveTwin(user.id, self.track)
        twin.p_known.update({k: float(v) for k, v in row.p_known.items()
                             if k in self.track.skills})
        twin.attention, twin.load = row.attention, row.load
        twin.steps_since_break, twin.step = row.steps_since_break, row.step
        attempts = self.db.scalars(select(AttemptRow)
                                   .where(AttemptRow.user_id == user.id,
                                          AttemptRow.track_id == self.track.id)
                                   .order_by(AttemptRow.id)).all()
        twin.attempts = [Attempt(a.item_id, a.skill, a.difficulty, a.modality, a.correct, a.step)
                         for a in attempts]
        return twin

    @staticmethod
    def _store(row: Twin, twin: CognitiveTwin) -> None:
        row.p_known = {k: round(v, 6) for k, v in twin.p_known.items()}
        row.attention, row.load = twin.attention, twin.load
        row.steps_since_break, row.step = twin.steps_since_break, twin.step

    # --- teaching loop ----------------------------------------

    def next(self, ctx: Ctx, user: User, skip_break: bool = False) -> dict:
        self.require_learning(user)
        row = self._twin_row(user, lock=True)
        twin = self._domain(user, row)
        if row.pending and not skip_break:
            return self._present(row.pending)  # idempotent: same question on reload
        policy = self.policies.current(self.db, self.track.id)
        cur, tutor, pacing = policy.agents(self.track, row.agent_state or {})
        quiet_for, max_steps = BREAK_RULES.get(user.age_band, BREAK_RULES["adult"])
        guardian = Guardian(max_steps=max_steps)
        teacher = MultiAgentTeacher(self.track, np.random.default_rng(), cur, tutor, pacing,
                                    guardian)
        profile = self.profile(user)
        twin.observe_state(NEUTRAL)  # headband disabled in phase one (R4.3)
        if skip_break:
            row.break_declined_at = twin.step
        required = twin.steps_since_break >= max_steps  # the safety limit always wins
        quiet = (twin.steps_since_break < quiet_for
                 or twin.step - row.break_declined_at < DECLINED_BREAK_COOLDOWN)
        d = teacher.act(twin, profile.neural_sensing_allowed, explore=False,
                        force_continue=(skip_break or quiet) and not required)
        d.explanation = explain(d, twin)
        row.agent_state = {"curriculum_current": cur.current, "curriculum_steps": cur._steps}
        dec = DecisionRow(user_id=user.id, track_id=self.track.id, step=twin.step, action=d.action,
                          proposed_action=d.proposed_action or d.action, skill=d.skill,
                          difficulty=d.difficulty, modality=d.modality,
                          item_id=d.item.id if d.item else None, policy_version=policy.version,
                          explanation=d.explanation)
        self.db.add(dec)
        for i in guardian.log:
            self.db.add(GuardianIntervention(user_id=user.id, track_id=self.track.id,
                                             step=i.step, agent=i.agent,
                                             rule=i.rule, detail=i.detail,
                                             policy_version=policy.version))
            GUARDIAN_INTERVENTIONS.labels(i.agent, i.rule).inc()
        self.db.flush()
        row.pending = self._pending(d, dec.id, policy.version)
        self._store(row, twin)
        TEACHING_STEPS.labels(d.action).inc()
        return self._present(row.pending)

    @staticmethod
    def _pending(d: Decision, decision_id: int, version: str) -> dict:
        p = {"decision_id": decision_id, "action": d.action,
             "proposed_action": d.proposed_action, "policy_version": version,
             "explanation": d.explanation}
        if d.action == "teach":
            p.update(skill=d.skill, difficulty=d.difficulty, modality=d.modality,
                     item_id=d.item.id)
        return p

    def _present(self, p: dict) -> dict:
        out = {k: p[k] for k in ("action", "proposed_action", "explanation", "policy_version")}
        if p["action"] == "teach":
            item = self.track.items[p["item_id"]]
            out.update(skill=p["skill"], difficulty=p["difficulty"], modality=p["modality"],
                       item={"id": item.id, "prompt": item.prompt, "options": list(item.options),
                             "skill_name": self.track.skills[p["skill"]].name,
                             "tip": item.hint if p["modality"] == "worked_example" else None})
        return out

    def answer(self, ctx: Ctx, user: User, item_id: str, choice: int) -> dict:
        self.require_learning(user)
        row = self._twin_row(user, lock=True)
        p = row.pending
        if not p or p["action"] != "teach" or p["item_id"] != item_id:
            raise Conflict("there is no open question with that id; ask for the next step")
        twin = self._domain(user, row)
        item = self.track.items[item_id]
        correct = choice == item.answer
        delta = twin.observe_answer(item.id, p["skill"], p["difficulty"], p["modality"], correct)
        self.db.add(AttemptRow(user_id=user.id, track_id=self.track.id, item_id=item.id,
                               skill=p["skill"],
                               difficulty=p["difficulty"], modality=p["modality"],
                               correct=correct, step=twin.step,
                               policy_version=p["policy_version"]))
        row.pending = None
        self._store(row, twin)
        ANSWERS.labels(str(correct).lower()).inc()
        return {"correct": correct, "answer": item.answer, "hint": item.hint,
                "belief": round(twin.p_known[p["skill"]], 3), "belief_delta": round(delta, 4),
                "skill": p["skill"]}

    def take_break(self, ctx: Ctx, user: User) -> dict:
        self.require_learning(user)
        row = self._twin_row(user, lock=True)
        if not row.pending or row.pending["action"] != "break":
            raise Conflict("no break is pending")
        twin = self._domain(user, row)
        twin.observe_break()
        row.pending = None
        self._store(row, twin)
        return {"ok": True}

    # --- credentials ----------------------------------------

    def request_credential(self, ctx: Ctx, learner: User, skill: str) -> IssueOutcome:
        if skill not in self.track.skills:
            raise NotFound(f"unknown skill {skill}")
        self.require_learning(learner)
        row = self._twin_row(learner, lock=True)
        twin = self._domain(learner, row)
        held = {c["credentialSubject"]["skill"] for c in
                self.ledger.credentials_for(learner.subject_did) if c["status"] == "active"}
        if skill in held:
            raise Conflict("this skill already has an active credential")
        out = self.ledger.issue(learner, twin, skill)
        audit(self.db, ctx, "credential.request", learner.id,
              outcome="issued" if out.issued else "refused", skill=skill, reason=out.reason,
              credential=out.credential["id"] if out.credential else None)
        return out

    # --- views ----------------------------------------

    def view(self, user: User) -> dict:
        row = self._twin_row(user, lock=False)
        twin = self._domain(user, row)
        tid = self.track.id
        creds = [c for c in self.ledger.credentials_for(user.subject_did)
                 if c["credentialSubject"]["skill"] in self.track.skills]
        active = {c["credentialSubject"]["skill"] for c in creds if c["status"] == "active"}
        skills = skill_statuses(twin, active)
        decisions = self.db.scalars(select(DecisionRow)
                                    .where(DecisionRow.user_id == user.id,
                                           DecisionRow.track_id == tid)
                                    .order_by(DecisionRow.id.desc()).limit(30)).all()
        results = {a.step: a for a in self.db.scalars(
            select(AttemptRow).where(AttemptRow.user_id == user.id, AttemptRow.track_id == tid)
            .order_by(AttemptRow.id.desc()).limit(30))}
        log = []
        for dcs in decisions:
            entry = {"step": dcs.step, "action": dcs.action, "skill": dcs.skill,
                     "difficulty": dcs.difficulty, "modality": dcs.modality,
                     "policy_version": dcs.policy_version}
            a = results.get(dcs.step + 1)
            if dcs.action == "teach" and a is not None and a.item_id == dcs.item_id:
                entry["correct"] = a.correct
            log.append(entry)
        interventions = self.db.scalars(
            select(GuardianIntervention).where(GuardianIntervention.user_id == user.id,
                                               GuardianIntervention.track_id == tid)
            .order_by(GuardianIntervention.id.desc()).limit(20)).all()
        return {
            "user": {"id": user.id, "name": user.display_name, "role": user.role,
                     "region": user.region, "institution": user.institution,
                     "age_band": user.age_band},
            "track_id": tid,
            "consent": self.consent_status(user),
            "sensing_active": False,
            "twin": twin.snapshot(),
            "skills": skills,
            "track": self.track.name,
            "progress": {"mastered": sum(s["status"] in ("mastered", "credentialed")
                                         for s in skills),
                         "total": len(skills),
                         "credentials": sum(c["status"] == "active" for c in creds)},
            "credentials": creds,
            "log": log,
            "interventions": [{"step": i.step, "agent": i.agent, "rule": i.rule,
                               "detail": i.detail} for i in interventions],
            "pending": self._present(row.pending) if row.pending else None,
        }

    # --- data rights (R4.5) ---------------------------------------------------------------------

    def export(self, ctx: Ctx, user: User) -> dict:
        rows = self.db.scalars(select(Twin).where(Twin.user_id == user.id)).all()
        attempts = self.db.scalars(select(AttemptRow).where(AttemptRow.user_id == user.id)
                                   .order_by(AttemptRow.id)).all()
        decisions = self.db.scalars(select(DecisionRow).where(DecisionRow.user_id == user.id)
                                    .order_by(DecisionRow.id)).all()
        interventions = self.db.scalars(select(GuardianIntervention)
                                        .where(GuardianIntervention.user_id == user.id)).all()
        audit(self.db, ctx, "data.export", user.id)
        return {
            "exported_at": utcnow().isoformat(),
            "profile": {"id": user.id, "display_name": user.display_name, "email": user.email,
                        "role": user.role, "institution": user.institution,
                        "region": user.region, "age_band": user.age_band,
                        "guardian_consent_confirmed_by_school": user.guardian_ok,
                        "pseudonymous_id": user.subject_did,
                        "created_at": user.created_at.isoformat()},
            "consent_history": self.consent_history(user.id),
            "subjects": [{"subject": row.track_id, "skill_estimates": row.p_known,
                          "questions_answered": row.step} for row in rows],
            "attempts": [{"subject": a.track_id, "item_id": a.item_id, "skill": a.skill,
                          "difficulty": a.difficulty,
                          "modality": a.modality, "correct": a.correct, "step": a.step,
                          "policy_version": a.policy_version,
                          "at": a.created_at.isoformat()} for a in attempts],
            "decisions": [{"step": d.step, "action": d.action, "skill": d.skill,
                           "difficulty": d.difficulty, "modality": d.modality,
                           "policy_version": d.policy_version,
                           "at": d.created_at.isoformat()} for d in decisions],
            "guardian_interventions": [{"step": i.step, "agent": i.agent, "rule": i.rule,
                                        "detail": i.detail} for i in interventions],
            "credentials": self.ledger.credentials_for(user.subject_did),
        }

    def delete(self, ctx: Ctx, user: User) -> None:
        """Remove personal data and twin state; ledger entries stay but are unlinkable."""
        uid = user.id
        v = self.settings.consent_policy_version
        for scope, text in (("learning", LEARNING_POLICY_TEXT),
                            ("neural_sensing", NEURAL_POLICY_TEXT)):
            self.db.add(ConsentRecord(user_id=uid, scope=scope, granted=False, policy_version=v,
                                      policy_text_sha256=policy_sha(text), source="deletion"))
        audit(self.db, ctx, "data.delete", uid)
        self.db.execute(delete(User).where(User.id == uid))
        self.db.flush()
