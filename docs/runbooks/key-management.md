# Validator keys: rotation, revocation and KMS outages (R2)

Validator private keys live in AWS KMS (ECC_NIST_P256, SIGN_VERIFY) and never leave it. The service may only call `kms:Sign` and `kms:GetPublicKey`. Public keys for every version are kept in `signing_keys` and published at `/api/public/keys`.

## Rotate a validator key (planned; at least yearly)

1. In `deploy/terraform/envs/<env>.tfvars`, append a generation: `generations = [1, 2]`. Never remove an old generation.
2. Deploy (staging first). Terraform creates the new KMS key and prints `validator_kms_keys`.
3. Register it: run `cognitive-twin keys rotate <validator-id> --kms-key-arn <new-arn>` as a one-off task, or use Admin › Signing keys › Rotate.
4. Check: Admin › Signing keys shows v2 active and v1 retired. Issue a test credential in staging and verify an old one; both must be valid.
5. Keep the old KMS key **enabled** for at least the credential retention period. Verification uses the stored public key, but keeping the key lets you re-derive it if needed.

## Suspected key compromise

1. Rotate immediately as above.
2. Find credentials signed by the compromised key: `SELECT id FROM credentials WHERE proofs::text LIKE '%<kid>%'`.
3. Decide with the partner institution whether to revoke them. Revoke via Educator › learner › Revoke, or `POST /api/educator/credentials/<id>/revoke` as an administrator, with a reason. The verify endpoint reports revoked credentials as invalid with that reason.
4. Disable the compromised KMS key **only after** deciding about its credentials.

## KMS outage

Symptoms: `CTKeyServiceFailures` alert; credential requests return 503 with a friendly message. Teaching and verification keep working, because verification uses stored public keys.

1. Check the AWS Health Dashboard for KMS in the region.
2. No data is at risk: learners can claim credentials again later. Post a status note if it lasts more than 30 minutes.
