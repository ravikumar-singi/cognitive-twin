# Children's data: design and what still needs legal review

Cognitive Twin can be used by learners of every age, including under 13. This page
explains how the product handles children's data and what a school, the operator and
their lawyers must still confirm. **It is not legal advice.**

## How the product behaves

| Rule | Where it is enforced |
|---|---|
| Age comes from school records only (the teacher's approval, or the school's identity provider: `ct_age_band`, `ct_adult_verified` or `birthdate`). A learner never declares their own age. The birthdate itself is not stored, only the age group. | `schools.set_age_band`, `auth/oidc.age_band_from_claims` |
| A learner under 18 cannot start until the school confirms a parent or guardian has agreed (the teacher's checkbox, or the IdP's `ct_guardian_consent` claim). Each confirmation or withdrawal is an append-only consent record (`scope = guardian`, `source = school`). | `learning.not_eligible_reason`, `schools.set_guardian_consent` |
| Each school chooses the youngest age group it enrols; younger learners are refused whatever their teacher sets. | `School.youngest_band` |
| Under-13s: first name only, no e-mail address kept (even if the IdP sends one), child-friendly consent wording. | `schools.create_local_user`, `set_age_band`, `auth/oidc.identity_from_claims`, `learning.MINOR_POLICY_TEXT` |
| Shorter stretches between breaks: required after 12 questions under 13, 18 for 13 to 17, 25 for adults. | `learning.BREAK_RULES`, `rl/shield.Guardian.max_steps` |
| The neural headband is never offered or recorded for under-18s (and stays disabled for everyone in this phase). | `learning.record_consent`, web app |
| Children cannot delete their own account; their teacher or school administrator can export or delete it at once, for example on a parent's request. Adults can do either themselves. | `api/learner.delete_me`, `api/staff.learner_export`, `learner_delete` |
| No advertising, no sale or sharing, no use beyond the school's educational purpose. Learning data is used only to choose the next question and to show progress to the learner and their teachers. | Product scope; state it in the school agreement |
| Teachers see only their own classes, school administrators only their own school. | `deps.educator_can_see`, `api/staff._cohort_or_404` |
| Class-code accounts need no e-mail; passwords are hashed with scrypt; five wrong tries pause the account for 15 minutes; resets sign the person out everywhere. | `schools.py`, `deps.current_user` |

## The legal picture (as of November 2026)

**United States (COPPA).** The FTC finalised amendments to the COPPA Rule on 16 January 2025, published them on 22 April 2025, with compliance required by 22 April 2026. The FTC decided not to codify the proposed "school authorization" exception, to avoid conflicting with possible changes to FERPA, but its existing guidance still lets schools consent on parents' behalf for educational technology used only for educational purposes and not commercially. The amendments also require separate parental consent before disclosing children's data to third parties such as advertisers, and limit how long it may be kept. This product's design matches that pattern (school-directed, no commercial use, no third-party disclosure), but the school agreement must say so, and a written information-security programme is required.
Sources: [Federal Register, 22 April 2025](https://www.govinfo.gov/content/pkg/FR-2025-04-22/html/2025-05904.htm); [K-12 Dive](https://www.k12dive.com/news/new-coppa-rule-effective-date-2025-ftc/746406/).

**India (DPDP Act 2023 and DPDP Rules 2025).** The Rules were notified on 14 November 2025, with most obligations phased in over 18 months. A "child" is anyone under 18. Section 9 requires verifiable parental consent and forbids tracking, behavioural monitoring and targeted advertising directed at children. The Fourth Schedule (Part A) exempts educational institutions when processing is restricted to their own educational activities or the safety of their enrolled children. An edtech provider acting purely as the school's processor generally leaves the consent obligation with the school as the data fiduciary. Whether adaptive teaching counts as "behavioural monitoring" inside the educational-activities exemption needs a lawyer's view.
Sources: [LiveLaw on DPDP child-data rules for edtech](https://www.livelaw.in/articles/dpdp-child-data-rules-demand-edtech-platforms-555317); [dpdpa.com, Rule 12](https://dpdpa.com/dpdparules/rule12.html).

**UK and EU.** The UK GDPR (with the ICO's Age Appropriate Design Code) and the EU GDPR (Article 8 and national ages of digital consent between 13 and 16) apply. Schools are usually the controller and the operator a processor. The EU AI Act's ban on emotion recognition in education is why the headband is disabled in the EU.

## Before children use it: checklist

- [ ] A data processing agreement with each school: the school is the controller (data fiduciary); the operator processes only on its instructions and for its educational purpose; no advertising, sale or secondary use; sub-processors listed (cloud host).
- [ ] The school's process for obtaining and recording guardian consent (for example, the admission form or a separate letter), which the teacher's checkbox attests to. Under DPDP Rule 10 the school must be able to show the consent is verifiable.
- [ ] A lawyer's confirmation, per country, that adaptive question selection is within the educational-activities exemption (India) and the school-authorised educational purpose (US).
- [ ] The DPIA ([`dpia-template.md`](dpia-template.md)) completed with the children's-data sections, and signed off by the school's data protection lead.
- [ ] Data retention agreed with each school (for example, delete a class's learner accounts at the end of the school year), then scheduled.
- [ ] Teachers have reviewed the subject questions for their curriculum and age group.
- [ ] Penetration test covers the class-code and password flows (see [`pentest-scope.md`](pentest-scope.md)).
