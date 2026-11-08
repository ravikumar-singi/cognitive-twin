"""Consent and policy profile for a learner.

Every downstream component asks this object what it may do. Defaults are
conservative: neural sensing is off unless explicitly enabled, it is never
enabled for learners under 16, and affect-style inference (attention and
load estimates) is disabled for EU learners to stay clear of the EU AI Act's
ban on emotion recognition in education settings.
"""

from __future__ import annotations

from dataclasses import dataclass

MIN_NEURAL_AGE = 16
AFFECT_RESTRICTED_REGIONS = frozenset({"EU"})


@dataclass(frozen=True)
class ConsentProfile:
    age: int
    region: str = "IN"
    neural_opt_in: bool = False
    guardian_consent: bool = False

    @property
    def is_minor(self) -> bool:
        return self.age < 18

    @property
    def may_enrol(self) -> bool:
        return not self.is_minor or self.guardian_consent

    @property
    def neural_sensing_allowed(self) -> bool:
        if not self.neural_opt_in or self.age < MIN_NEURAL_AGE:
            return False
        if self.is_minor and not self.guardian_consent:
            return False
        return self.region.upper() not in AFFECT_RESTRICTED_REGIONS

    def explain(self) -> dict[str, object]:
        reasons = []
        if not self.neural_opt_in:
            reasons.append("learner has not opted in to neural sensing")
        if self.age < MIN_NEURAL_AGE:
            reasons.append(f"neural sensing is off by default under {MIN_NEURAL_AGE}")
        if self.region.upper() in AFFECT_RESTRICTED_REGIONS:
            reasons.append("affect inference disabled in this region")
        return {
            "may_enrol": self.may_enrol,
            "neural_sensing_allowed": self.neural_sensing_allowed,
            "reasons": reasons,
        }
