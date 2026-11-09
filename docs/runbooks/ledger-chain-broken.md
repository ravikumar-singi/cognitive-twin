# Ledger chain broken

**Alerts:** `CTLedgerChainBroken` (Prometheus), `ct-<env>-ledger-chain-broken` (CloudWatch). Instances also report not ready (`/readyz` returns 503), so the load balancer stops sending them traffic.

The hash chain only breaks if ledger rows changed outside the application. Database triggers and the `ct_app` role's permissions both forbid updates and deletes, so treat this as a **SEV1 possible-tampering incident**.

1. **Stop issuance.** Scale the service to zero or put the ALB behind a maintenance response. Learners can't use the system meanwhile, but no credential will be issued onto a broken chain.
2. **Find the first bad block:** `cognitive-twin ledger verify` from a one-off task. It logs `first_bad_block`.
3. **Preserve evidence:** take a manual RDS snapshot now, and export the RDS PostgreSQL logs and CloudTrail for the period.
4. **Find who changed what:** check CloudTrail for RDS access, the database logs for UPDATE/DELETE/ALTER on ledger tables, and whether triggers were disabled (`SELECT tgname, tgenabled FROM pg_trigger WHERE tgname LIKE '%no_mutation%'`).
5. **Restore:** recover to just before the first tampered write with point-in-time recovery ([backup and restore](backup-restore.md)), then re-run `cognitive-twin ledger verify` on the restored database.
6. **Assess credentials issued after the restore point:** they will be missing from the restored ledger. List them from the snapshot and either re-issue them (learners re-claim) or revoke them with a reason.
7. Follow [incident response](incident-response.md) for notification.
