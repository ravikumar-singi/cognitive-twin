# Phase-one requirements traceability

Source: [Cognitive Twin: Phase One Production Requirements](https://claude.ai/artifact/TEDW76LuV3tQVnmSDmJFAV).

**Status key:** ✅ built and tested · 🟡 built, but needs an action in a real environment to sign off · ⬜ organisational action, not code.

## R1. Persistence

| Req | Status | Implementation | Evidence |
|---|---|---|---|
| R1.1 Managed relational DB with PITR | 🟡 | `db/models.py`, Alembic migration `0001`; RDS PostgreSQL 16, multi-AZ, PITR (`deploy/terraform/database.tf`) | Full suite runs on PostgreSQL via real migrations; `test_migrations_match_the_models_exactly`. Needs `terraform apply`. |
| R1.2 Append-only ledger; no updates/deletes; chain verified at startup and nightly | ✅ | Triggers (`db/session.py`), least-privilege `ct_app` role (`db/roles.py`), startup check (`app.py`), nightly ECS schedule (`ecs.tf`), alert | `test_append_only_tables_reject_updates_and_deletes`, `test_app_role_cannot_update_or_delete_ledger_rows`, `test_tampered_chain_fails_readiness`, promtool alert test |
| R1.3 Sessions survive restart; reload resumes | ✅ | All state in PostgreSQL; the open question is stored and re-served | `test_progress_survives_restart_and_any_instance_can_serve`; browser reload check |
| R1.4 Stateless, multi-instance | ✅ | No in-process learner state; Redis rate limits; advisory locks for ledger appends and startup | Two-instance test; multi-worker load tests; concurrent sign-in test |
| R1.5 Daily encrypted backups; restore tested before pilot | 🟡 | AWS Backup plan and vault (KMS), `deploy/scripts/restore-test.sh`, [runbook](runbooks/backup-restore.md) | Run the restore test on staging and record it in the runbook |

## R2. Signing keys and credential durability

| Req | Status | Implementation | Evidence |
|---|---|---|---|
| R2.1 Keys in KMS/HSM, never in app memory or source | ✅ | `keys.py` `AwsKmsKeyProvider`; KMS keys in `kms.tf`; sign-only IAM; local keys refused outside development | `test_kms_keys_sign_and_rotate_without_breaking_old_signatures`, `test_production_config_rejects_unsafe_settings` |
| R2.2 Credential records key ID and version | ✅ | Each proof carries `kid` and `keyVersion`, and the signed attestation lists them | `test_restart_and_rotate_credential_still_verifies` |
| R2.3 Rotation without invalidating; retired keys published | ✅ | `KeyRegistry.rotate`; `/api/public/keys` lists all versions; old KMS generations kept (`prevent_destroy`) | same test |
| R2.4 Documented revocation; verify reports revoked with reason | ✅ | `LedgerService.revoke`, educator/admin endpoint, [runbook](runbooks/key-management.md) | `test_revocation_is_reported_with_reason` |
| R2.5 Issue, restart, rotate, still verifies | ✅ | | `test_restart_and_rotate_credential_still_verifies` (runs on PostgreSQL in CI) |

## R3. Identity, access and API security

| Req | Status | Implementation | Evidence |
|---|---|---|---|
| R3.1 Institution SSO (OIDC); no anonymous sessions | ✅ | `auth/oidc.py`: code flow, PKCE, state, nonce, JWKS validation; dev sign-in impossible outside development | `tests/test_oidc.py` (full flow against a mock IdP) |
| R3.2 Learner / educator / admin roles, cohort scoping | ✅ | `deps.py` `require_role`, `educator_can_see`; cohorts synced from IdP claims | `test_educator_sees_only_their_cohort`, role-matrix tests |
| R3.3 Server-side authorisation on every endpoint | ✅ | Every private route depends on `current_user` or `require_role`; IDs alone never grant access | `test_every_private_route_requires_sign_in`, `test_learners_cannot_reach_staff_routes`, `test_educators_cannot_reach_admin_routes` |
| R3.4 Issuance only by the learner or their educator; logged | ✅ | `/api/me/credentials`, `/api/educator/learners/{id}/credentials`; audit events | `test_learner_and_educator_can_trigger_issuance_and_it_is_audited` |
| R3.5 Minimal public verify | ✅ | `/api/public/credentials/{id}/verify` returns status, reason, checks and six credential fields | `test_public_verify_returns_only_what_is_needed` |
| R3.6 HTTPS, rate limits per user and IP, input validation | ✅ | ALB TLS 1.2+ with HTTP redirect, HSTS, HTTPS enforcement; Redis-backed limits; WAF; Pydantic validation; CSRF | `test_per_user_rate_limit`, `test_per_ip_rate_limit_on_sign_in_only`, `test_security_headers`, `test_invalid_input_is_rejected`, `test_csrf_token_required_for_cookie_sessions` |
| R3.7 Dependency scan and external pen test, highs fixed | 🟡 | CI: pip-audit, bandit, Trivy, ruff security rules | Scans run on every push. External test: [scope](compliance/pentest-scope.md) to be commissioned |

## R4. Consent and data rights

| Req | Status | Implementation | Evidence |
|---|---|---|---|
| R4.1 Age from school records; under-18s need school-confirmed guardian consent; school minimum age (v0.3, replaces "adults only") | ✅ | `age_band_from_claims`, teacher approval, `not_eligible_reason`; append-only `guardian` consent records | `test_full_school_journey_for_a_young_learner`, `test_minor_without_guardian_consent_waits`, `test_school_can_set_a_stricter_age_limit`, `test_age_groups_and_guardian_consent_come_from_school_claims` |
| R4.2 Versioned, timestamped, append-only consent | ✅ | `consent_records` table (append-only), policy version and text hash | `test_consent_is_versioned_and_never_overwritten`, `test_new_policy_version_requires_renewed_consent` |
| R4.3 Headband disabled; sensing consent stored separately | ✅ | Separate `neural_sensing` scope; `CT_NEURAL_ENABLED` refused at boot | `test_neural_consent_is_stored_but_sensing_stays_off` |
| R4.4 Region from institution records drives regional rules | ✅ | `ct_region` claim required; EU disables affect inference | `test_region_must_come_from_the_institution`, `test_region_comes_from_the_institution_and_drives_rules` |
| R4.5 Export and deletion; ledger keeps only unlinkable data | ✅ | `/api/me/export`, `/api/me/delete`; random DIDs | `test_export_contains_my_data`, `test_deletion_removes_personal_data_but_keeps_unlinkable_credentials` |
| R4.6 DPIA signed off by partner | ⬜ | [DPIA draft](compliance/dpia-template.md) with the technical sections filled in | Partner to complete and sign |

## R6. Schools, ages and subjects (v0.3)

| Req | Status | Implementation | Evidence |
|---|---|---|---|
| R6.1 Several subjects, each with its own trained policy and separate progress | ✅ | 5 subject files, `content/policies/*`, per-subject twins and policy activations; bundled policies activate on first start | `test_subjects_are_separate_and_schools_choose_them`, `test_policy_versions_are_recorded_and_revertible`; [benchmarks](benchmarks/) |
| R6.2 Schools without SSO: class codes, usernames, staff invitations | ✅ | `schools.py`; `/auth/join`, `/auth/local-login`, `/auth/invite`, `/auth/password` | `test_full_school_journey_for_a_young_learner`, `test_join_codes_can_be_turned_off_and_invites_are_single_use` |
| R6.3 Password safety: hashing, lockout, reset signs out, forced change of temporary passwords | ✅ | scrypt; 5 tries / 15 min; session epoch; `must_change` gate; sign-in paths rate-limited per IP | `test_lockout_after_repeated_wrong_passwords`, `test_teacher_adds_students_and_resets_passwords` |
| R6.4 School isolation: teachers see their classes, school admins their school | ✅ | `educator_can_see`, `_cohort_or_404`, 404 outside scope | `test_schools_and_classes_are_isolated` |
| R6.5 Child protections: minimal data for under-13s, shorter break limits, no headband, deletion via school | ✅ | See [children's data](compliance/children.md) | `test_full_school_journey_for_a_young_learner`, `test_sso_personas_cover_children` |
| R6.6 Legal review of children's data per country; teacher review of question content | ⬜ | [Checklist](compliance/children.md#before-children-use-it-checklist) | Operator, schools and counsel |
| R6.7 Upgrade keeps existing pilot data | ✅ | Migration `0002` with backfill | Migration run against seeded v0.2 data on PostgreSQL; schema drift test |

## R5. Operations

| Req | Status | Implementation | Evidence |
|---|---|---|---|
| R5.1 Structured logs, metrics, traces, dashboards | ✅ | JSON logs with request IDs; Prometheus metrics; OpenTelemetry; Grafana dashboard (10 panels) | `/metrics` checked in tests; dashboard JSON |
| R5.2 Alerts: errors, ledger chain, key service, replication lag | ✅ | `alerts.yml` (6 rules) plus CloudWatch alarms (5xx rate, p95, replica lag, storage, unhealthy targets, ledger log filter) | `promtool check rules` and `promtool test rules` in CI |
| R5.3 Versioned policies; decisions record version; fast revert | ✅ | `policy_store.py`; `decisions.policy_version`; admin activate; 15 s cache | `test_policy_versions_are_recorded_and_revertible` |
| R5.4 Infrastructure as code; staging and production; staging first | 🟡 | `deploy/terraform` with two env files; deploy workflow gates production on staging plus approval | `terraform fmt` and `terraform validate` in CI. Needs a first apply |
| R5.5 2,000 concurrent learners, p95 < 300 ms | 🟡 | Locust script with an SLO gate; capacity fixes listed in [load-testing.md](load-testing.md) | Passes at 1,000 learners locally (p95 260 ms, 0 failures). 2,000-learner run due on staging |
| R5.6 Incident runbook incl. breach notification | ✅ | [Incident response](runbooks/incident-response.md) plus 4 specific runbooks | Jurisdiction deadlines to confirm with counsel |

## Acceptance criteria

| Criterion | Status |
|---|---|
| Every requirement met in staging | 🟡 Code complete; needs the first staging deploy |
| Restart-and-rotate credential test passes | ✅ Automated (R2.5) |
| Penetration test, no open highs | ⬜ To be commissioned (scope now includes class codes and passwords) |
| Children's data reviewed by counsel for each country; DPAs with schools | ⬜ Operator and school action |
| DPIA signed off | ⬜ Partner action |
| Partner approves the pilot plan | ⬜ Partner action |
