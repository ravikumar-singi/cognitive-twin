# Backup and restore (R1.5)

| What | How | Retention |
|---|---|---|
| Point-in-time recovery | RDS automated backups (continuous WAL) | 14 days staging, 35 days production |
| Daily snapshot | AWS Backup plan `ct-<env>`, 02:30 IST | 35 days |
| Encryption | KMS key `alias/ct-<env>-data` for RDS, snapshots and the backup vault | n/a |

## Restore test (required once before the pilot, then quarterly)

Run `deploy/scripts/restore-test.sh <env>` from an operator machine with AWS credentials. It:

1. restores the production database to a **new** instance at "now minus 15 minutes" (the original is untouched);
2. runs `cognitive-twin ledger verify` and row counts against the restored copy;
3. prints the results and the elapsed time, which is your measured recovery time;
4. deletes the temporary instance.

Record the date, the result and the measured recovery time here:

| Date | Environment | Restore point | Ledger intact | Recovery time | By |
|---|---|---|---|---|---|
| `[pending: before pilot launch]` | | | | | |

## Real restore

1. Decide the restore point (just before the incident).
2. `aws rds restore-db-instance-to-point-in-time --source-db-instance-identifier ct-production --target-db-instance-identifier ct-production-restored --restore-time <UTC> ...`
3. Verify the ledger on the restored instance, then swap: rename the old instance, rename the restored one to `ct-production`, or point `DB_HOST` at it and redeploy.
4. Re-run the migrate task, which also re-applies the `ct_app` role grants.
