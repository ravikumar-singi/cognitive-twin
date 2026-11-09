#!/usr/bin/env bash
# Restore-test the database (R1.5). Restores to a temporary instance, verifies, cleans up.
# usage: deploy/scripts/restore-test.sh <staging|production>
set -euo pipefail
ENV="${1:?usage: restore-test.sh <staging|production>}"
SRC="ct-${ENV}"
TMP="ct-${ENV}-restore-test-$(date +%Y%m%d%H%M)"
RESTORE_TIME="$(date -u -d '15 minutes ago' +%Y-%m-%dT%H:%M:%SZ)"
START=$(date +%s)

SUBNETS=$(aws rds describe-db-instances --db-instance-identifier "$SRC" --query 'DBInstances[0].DBSubnetGroup.DBSubnetGroupName' --output text)
SG=$(aws rds describe-db-instances --db-instance-identifier "$SRC" --query 'DBInstances[0].VpcSecurityGroups[0].VpcSecurityGroupId' --output text)

echo "restoring $SRC at $RESTORE_TIME to $TMP"
aws rds restore-db-instance-to-point-in-time \
  --source-db-instance-identifier "$SRC" --target-db-instance-identifier "$TMP" \
  --restore-time "$RESTORE_TIME" --db-subnet-group-name "$SUBNETS" \
  --vpc-security-group-ids "$SG" --no-publicly-accessible --no-multi-az >/dev/null
trap 'echo "deleting $TMP"; aws rds delete-db-instance --db-instance-identifier "$TMP" --skip-final-snapshot >/dev/null' EXIT
aws rds wait db-instance-available --db-instance-identifier "$TMP"
HOST=$(aws rds describe-db-instances --db-instance-identifier "$TMP" --query 'DBInstances[0].Endpoint.Address' --output text)
echo "restored in $(( $(date +%s) - START ))s; verifying"

# Run the verification as a one-off ECS task pointed at the restored host.
CLUSTER="ct-${ENV}"
TD="ct-${ENV}-ledger-verify"
NET=$(aws ecs describe-services --cluster "$CLUSTER" --services app --query 'services[0].networkConfiguration' --output json)
TASK=$(aws ecs run-task --cluster "$CLUSTER" --launch-type FARGATE --task-definition "$TD" \
  --network-configuration "$NET" \
  --overrides "{\"containerOverrides\":[{\"name\":\"ledger-verify\",\"environment\":[{\"name\":\"DB_HOST\",\"value\":\"$HOST\"}]}]}" \
  --query 'tasks[0].taskArn' --output text)
aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK"
CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[0].exitCode' --output text)
echo "ledger verify exit code: $CODE (0 = chain intact)"
echo "total recovery time: $(( $(date +%s) - START ))s"
test "$CODE" = "0"
