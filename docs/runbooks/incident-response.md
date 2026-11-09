# Incident response (R5.6)

**Owner:** the on-call engineer named in the pilot rota. **Escalation:** product owner, then the partner institution's data protection officer (DPO).

## 1. Triage (first 15 minutes)

1. Acknowledge the alert. Open the Grafana "Cognitive Twin" dashboard and the CloudWatch alarms.
2. Classify severity:

| Severity | Examples | Response |
|---|---|---|
| **SEV1** | Suspected data breach; ledger chain broken; credentials issued wrongly; service down | Page product owner now. Start an incident channel and a timeline. |
| **SEV2** | Error rate above 2%; p95 above 300 ms for 30 minutes; key service failing (no issuance) | Fix within the working day; update the channel hourly. |
| **SEV3** | A single learner affected; cosmetic | Ticket. |

3. Record every action with a UTC timestamp in the incident timeline.

## 2. Contain

| Situation | First action |
|---|---|
| Bad release | Roll back: re-run the previous successful deploy, or set the ECS service to the previous task definition. |
| Teaching behaving badly (guardian spike, complaints) | [Policy rollback](policy-rollback.md): activate the previous policy version. |
| Ledger alarm | [Ledger chain broken](ledger-chain-broken.md). Stop issuance first. |
| KMS failing | [Key management](key-management.md), section "KMS outage". |
| Suspected account compromise | Disable the user at the institution IdP; their sessions die within one request because roles are re-read every call. Rotate `ct-<env>/session-secret` to end **all** sessions. |
| Suspected database exposure | Rotate the `ct_app` password (re-run the migrate task after changing `ct-<env>/db-app-password`) and the RDS master secret. Preserve logs. |

## 3. Personal data breach: notification duties

A breach is any accidental or unlawful destruction, loss, alteration, unauthorised disclosure of, or access to personal data. For this pilot that includes learner profiles, consent records, attempts, decisions and audit logs.

1. **Within 1 hour of suspicion:** tell the partner institution's DPO. They are the controller; we act on their instructions.
2. **Assess together:** what data, how many learners, whether it is still happening, and the likely harm.
3. **Regulator notification:** the controller decides and notifies. Deadlines depend on jurisdiction: for example, **72 hours** after becoming aware under GDPR or UK GDPR, and **without delay** under India's DPDP Act and Rules. Confirm current deadlines with counsel at pilot start and write them here: `[jurisdiction: deadline, authority, contact]`.
4. **Learners:** notify affected learners without undue delay when the risk to them is high, using wording agreed with the DPO.
5. **Evidence:** preserve CloudWatch logs, the audit log (append-only), VPC flow logs and database snapshots. Don't delete anything during an investigation.

## 4. Recover and learn

- Confirm `/readyz` is green on every instance and alarms are clear.
- Within five working days, write a blameless post-incident review: timeline, root cause, what went well, what to change, owners and dates.
