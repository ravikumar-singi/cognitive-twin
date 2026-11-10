# Roadmap

## v0.1 (this release)
- Simulated student population and training environment
- Cognitive twin with BKT belief state
- Curriculum, tutor and pacing agents with a Guardian shield
- Benchmarks against untrained, fixed-syllabus and random teachers
- Validator DAO, Ed25519-signed verifiable credentials, hash-chained ledger
- FastAPI service, dashboard, CLI, 24 tests, CI

## v0.2: phase one, production pilot
- PostgreSQL persistence with append-only ledger, consent and audit tables; stateless multi-instance service
- KMS-held validator keys with versioning, rotation and revocation
- Institution SSO (OIDC), learner/educator/administrator roles, cohort scoping, CSRF, rate limits
- Versioned consent, adults-only eligibility from IdP claims, data export and deletion
- Versioned teaching policies with rollback; JSON logs, Prometheus metrics, tracing, alerts
- Terraform for staging and production, CI security scans, staged deploys, load test, runbooks

## v0.3: schools, all ages, five subjects (this release)
- Middle-school maths, high-school algebra, general science and Python basics alongside ML foundations, each with a trained policy
- Schools as tenants; school administrators; class codes, usernames and staff invitations for schools without SSO
- Age groups and guardian consent from school records; under-13 data minimisation; age-based break limits
- Teacher tools: approvals, adding students, password resets, data export and deletion on request

## v0.3.x: content and realism
- Teacher review of every question; larger question banks (at least 20 per skill) with item-response-theory difficulty calibration
- Per-subject credential thresholds to bring credential precision back above 80%
- Fit simulator parameters to public interaction logs (ASSISTments, EdNet); off-policy evaluation on logged data
- Deep knowledge tracing as an alternative belief model
- Printable class sign-in cards; retention schedules per school

## v0.4: learning agents at scale
- Centralised training with decentralised execution for the agent team
- Constrained RL so fairness and time budgets are optimised, not just enforced
- LLM content layer: Socratic dialogue and item generation, with the Guardian checking outputs

## v0.5: hardware and spatial
- Integrate a consumer EEG headband through a device SDK
- AR session runtime with edge rendering

## v1.0: independent validators and evidence
- Validators on their own infrastructure with their own keys (phase two)
- Zero-knowledge proofs in place of salted commitments
- LTI 1.3 and OneRoster integration; randomised controlled pilot
