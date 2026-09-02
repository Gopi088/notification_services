#!/usr/bin/env bash
# Idempotently create the AWS primitives needed by deploy/deploy.sh.
# Networking and secret values stay manual because they are environment-specific.
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-}"
ECR_REPOSITORY="${ECR_REPOSITORY:-notification-service}"
ECS_CLUSTER="${ECS_CLUSTER:-notification-service}"
CLOUDWATCH_LOG_GROUP="${CLOUDWATCH_LOG_GROUP:-/ecs/notification-service}"
CLOUDWATCH_RETENTION_DAYS="${CLOUDWATCH_RETENTION_DAYS:-30}"
ECS_EXECUTION_ROLE_NAME="${ECS_EXECUTION_ROLE_NAME:-ecsTaskExecutionRole}"
ECS_TASK_ROLE_NAME="${ECS_TASK_ROLE_NAME:-notification-service-task-role}"
ECS_SECRETS_ARN="${ECS_SECRETS_ARN:-}"

command -v aws >/dev/null 2>&1 || { echo "Required command not found: aws" >&2; exit 1; }

# Verify credentials and prevent accidental setup in a different account.
caller_account_id="$(aws sts get-caller-identity --query Account --output text)"
if [[ -n "$AWS_ACCOUNT_ID" && "$AWS_ACCOUNT_ID" != "$caller_account_id" ]]; then
  echo "AWS_ACCOUNT_ID does not match the active AWS credentials." >&2
  exit 1
fi
AWS_ACCOUNT_ID="$caller_account_id"

trust_policy='{"Version":"2012-10-17","Statement":[{"Effect":"Allow","Principal":{"Service":"ecs-tasks.amazonaws.com"},"Action":"sts:AssumeRole"}]}'

if ! aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$ECR_REPOSITORY" >/dev/null 2>&1; then
  aws ecr create-repository --region "$AWS_REGION" --repository-name "$ECR_REPOSITORY" >/dev/null
  echo "Created ECR repository: $ECR_REPOSITORY"
fi

if ! aws logs describe-log-groups --region "$AWS_REGION" --log-group-name-prefix "$CLOUDWATCH_LOG_GROUP" \
  --query "logGroups[?logGroupName=='$CLOUDWATCH_LOG_GROUP'].logGroupName" --output text | grep -qx "$CLOUDWATCH_LOG_GROUP"; then
  aws logs create-log-group --region "$AWS_REGION" --log-group-name "$CLOUDWATCH_LOG_GROUP"
  echo "Created CloudWatch log group: $CLOUDWATCH_LOG_GROUP"
fi
aws logs put-retention-policy --region "$AWS_REGION" --log-group-name "$CLOUDWATCH_LOG_GROUP" \
  --retention-in-days "$CLOUDWATCH_RETENTION_DAYS"

if ! aws ecs describe-clusters --region "$AWS_REGION" --clusters "$ECS_CLUSTER" \
  --query 'clusters[0].status' --output text | grep -qx ACTIVE; then
  aws ecs create-cluster --region "$AWS_REGION" --cluster-name "$ECS_CLUSTER" >/dev/null
  echo "Created ECS cluster: $ECS_CLUSTER"
fi

if ! aws iam get-role --role-name "$ECS_EXECUTION_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ECS_EXECUTION_ROLE_NAME" --assume-role-policy-document "$trust_policy" >/dev/null
  echo "Created ECS execution role: $ECS_EXECUTION_ROLE_NAME"
fi
aws iam attach-role-policy --role-name "$ECS_EXECUTION_ROLE_NAME" \
  --policy-arn arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy

if ! aws iam get-role --role-name "$ECS_TASK_ROLE_NAME" >/dev/null 2>&1; then
  aws iam create-role --role-name "$ECS_TASK_ROLE_NAME" --assume-role-policy-document "$trust_policy" >/dev/null
  echo "Created ECS task role: $ECS_TASK_ROLE_NAME"
fi

# The execution role injects task-definition secrets before the container
# starts. Scope access to this one secret rather than granting account-wide
# Secrets Manager access. A customer-managed KMS key needs a matching manual
# kms:Decrypt grant on that key.
if [[ -n "$ECS_SECRETS_ARN" ]]; then
  secrets_policy="{\"Version\":\"2012-10-17\",\"Statement\":[{\"Effect\":\"Allow\",\"Action\":[\"secretsmanager:GetSecretValue\"],\"Resource\":\"${ECS_SECRETS_ARN}\"}]}"
  aws iam put-role-policy --role-name "$ECS_EXECUTION_ROLE_NAME" \
    --policy-name notification-service-read-secrets --policy-document "$secrets_policy"
fi

echo
echo "AWS primitives are ready for account $AWS_ACCOUNT_ID in $AWS_REGION."
echo "Execution role ARN: arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ECS_EXECUTION_ROLE_NAME}"
echo "Task role ARN:      arn:aws:iam::${AWS_ACCOUNT_ID}:role/${ECS_TASK_ROLE_NAME}"
echo
echo "Manual prerequisites (intentionally not created by this script):"
echo "  1. RDS PostgreSQL and a Secrets Manager JSON secret containing DATABASE_URL."
echo "  2. Optional ElastiCache Redis and REDIS_URL/REDIS_PASSWORD in that same secret."
echo "  3. VPC subnets and an ECS security group (inbound 8000 only from an ALB security group)."
echo "  4. Optional ALB, target group, HTTPS listener, DNS, and provider webhook URLs."
echo "  5. If the secret uses a customer-managed KMS key, grant the execution role kms:Decrypt on that key."
if [[ -z "$ECS_SECRETS_ARN" ]]; then
  echo "  6. Create the JSON secret, keep its source file outside Git, then export ECS_SECRETS_ARN."
else
  aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id "$ECS_SECRETS_ARN" >/dev/null
  echo "  Secrets Manager secret verified: $ECS_SECRETS_ARN"
fi
