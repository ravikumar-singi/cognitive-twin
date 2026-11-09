# Roadmap

## v0.1 (this release)
- Simulated student population and training environment
- Cognitive twin with BKT belief state
- Curriculum, tutor and pacing agents with a Guardian shield
- Benchmarks against untrained, fixed-syllabus and random teachers
- Validator DAO, Ed25519-signed verifiable credentials, hash-chained ledger
- FastAPI service, dashboard, CLI, 24 tests, CI

## v0.2: phase one, production pilot (this release)
- PostgreSQL persistence with append-only ledger, consent and audit tables; stateless multi-instance service
- KMS-held validator keys with versioning, rotation and revocation
- Institution SSO (OIDC), learner/educator/administrator roles, cohort scoping, CSRF, rate limits
- Versioned consent, adults-only eligibility from IdP claims, data export and deletion
- Versioned teaching policies with rollback; JSON logs, Prometheus metrics, tracing, alerts
- Terraform for staging and production, CI security scans, staged deploys, load test, runbooks

## v0.3: realism
- Fit simulator parameters to public interaction logs (ASSISTments, EdNet)
- Off-policy evaluation (inverse propensity scoring, doubly robust) on logged data
- Deep knowledge tracing as an alternative belief model
- Second content track; item-response-theory difficulty calibration

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
