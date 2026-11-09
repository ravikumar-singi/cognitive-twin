# Teaching policy rollback (R5.3)

Every teaching decision records the policy version that produced it.

**Roll back (under 2 minutes):** Admin › Teaching policies › **Activate** on the previous version, with a reason. Or run `cognitive-twin policy activate <version> --notes "rollback: ..."` from a one-off task. Every instance picks up the change within `CT_POLICY_CACHE_SECONDS` (default 15 s). Nothing is deleted, and the activation is recorded in the append-only `policy_activations` table and the audit log.

**Release a new policy:**
1. `cognitive-twin train --out policy.json --json benchmark.json` and review the benchmark against the current version.
2. Admin › Teaching policies: upload it (inactive), or `cognitive-twin policy import policy.json` (activates).
3. Activate in staging first and watch the guardian-intervention and error panels for a day.
4. Activate in production. Watch `CTGuardianInterventionSpike` for the next 24 hours.

**Investigating a bad version:** `SELECT policy_version, action, count(*) FROM decisions WHERE created_at > now() - interval '1 day' GROUP BY 1, 2;` and the guardian interventions grouped by `policy_version`.
