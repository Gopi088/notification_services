#!/usr/bin/env bash
# Build, validate, publish, and deploy the API container to ECS Fargate.
# Credentials stay in AWS Secrets Manager; this script never reads a .env file.
set -euo pipefail

AWS_REGION="${AWS_REGION:-us-east-1}"
AWS_ACCOUNT_ID="${AWS_ACCOUNT_ID:-}"
ECR_REPOSITORY="${ECR_REPOSITORY:-notification-service}"
ECS_CLUSTER="${ECS_CLUSTER:-notification-service}"
ECS_SERVICE="${ECS_SERVICE:-notification-service}"
ECS_TASK_FAMILY="${ECS_TASK_FAMILY:-notification-service}"
CLOUDWATCH_LOG_GROUP="${CLOUDWATCH_LOG_GROUP:-/ecs/notification-service}"
ECS_EXECUTION_ROLE_ARN="${ECS_EXECUTION_ROLE_ARN:-}"
ECS_TASK_ROLE_ARN="${ECS_TASK_ROLE_ARN:-}"
ECS_SECRETS_ARN="${ECS_SECRETS_ARN:-}"
ECS_SUBNET_IDS="${ECS_SUBNET_IDS:-}"
ECS_SECURITY_GROUP_IDS="${ECS_SECURITY_GROUP_IDS:-}"
ECS_ASSIGN_PUBLIC_IP="${ECS_ASSIGN_PUBLIC_IP:-DISABLED}"
DESIRED_COUNT="${DESIRED_COUNT:-1}"
LOCAL_IMAGE="${LOCAL_IMAGE:-notification-service}"
# Safe production defaults. Override only when the corresponding infrastructure
# (for example Redis workers) is deployed as well.
MOCK_MODE="${MOCK_MODE:-false}"
QUEUE_ENABLED="${QUEUE_ENABLED:-true}"
AUTH_ENABLED="${AUTH_ENABLED:-true}"
SMS_PROVIDER="${SMS_PROVIDER:-azure}"
WHATSAPP_PROVIDER="${WHATSAPP_PROVIDER:-azure}"
ECS_DEPLOY_WORKERS="${ECS_DEPLOY_WORKERS:-true}"
ECS_WORKER_DESIRED_COUNT="${ECS_WORKER_DESIRED_COUNT:-1}"

require_command() {
  command -v "$1" >/dev/null 2>&1 || { echo "Required command not found: $1" >&2; exit 1; }
}

for command in aws docker git python3; do require_command "$command"; done

# Verify the active AWS principal before building or changing any AWS resource.
caller_account_id="$(aws sts get-caller-identity --query Account --output text)"
if [[ -n "$AWS_ACCOUNT_ID" && "$AWS_ACCOUNT_ID" != "$caller_account_id" ]]; then
  echo "AWS_ACCOUNT_ID does not match the active AWS credentials." >&2
  exit 1
fi
AWS_ACCOUNT_ID="$caller_account_id"

if [[ -z "$ECS_SECRETS_ARN" ]]; then
  echo "ECS_SECRETS_ARN is required. It must reference a Secrets Manager JSON secret." >&2
  exit 1
fi
aws secretsmanager describe-secret --region "$AWS_REGION" --secret-id "$ECS_SECRETS_ARN" >/dev/null

ECS_EXECUTION_ROLE_ARN="${ECS_EXECUTION_ROLE_ARN:-arn:aws:iam::${AWS_ACCOUNT_ID}:role/ecsTaskExecutionRole}"
ECS_TASK_ROLE_ARN="${ECS_TASK_ROLE_ARN:-arn:aws:iam::${AWS_ACCOUNT_ID}:role/notification-service-task-role}"
image_tag="$(git rev-parse --short=12 HEAD 2>/dev/null || date -u +%Y%m%d%H%M%S)"
image_uri="${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com/${ECR_REPOSITORY}:${image_tag}"
container_name="notification-service-local-check-${image_tag}"
cleanup() {
  docker rm -f "$container_name" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Building local image ${LOCAL_IMAGE}:${image_tag}"
docker build --tag "${LOCAL_IMAGE}:${image_tag}" .

# Validate the built image locally in safe mock mode before publishing it.
echo "Validating the container health check locally"
docker run -d --name "$container_name" \
  -e MOCK_MODE=true \
  -e STORAGE_BACKEND=sqlite \
  -e DATABASE_PATH=/tmp/notifications.db \
  -e AUTH_ENABLED=false \
  -e LOG_FILE= \
  -e AUDIT_LOG_FILE= \
  "${LOCAL_IMAGE}:${image_tag}" >/dev/null
for _ in $(seq 1 30); do
  health="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}starting{{end}}' "$container_name")"
  [[ "$health" == "healthy" ]] && break
  [[ "$health" == "unhealthy" ]] && { docker logs "$container_name" >&2; exit 1; }
  sleep 2
done
[[ "${health:-starting}" == "healthy" ]] || { docker logs "$container_name" >&2; exit 1; }
docker rm -f "$container_name" >/dev/null

# Create the ECR repository only on its first deployment.
if ! aws ecr describe-repositories --region "$AWS_REGION" --repository-names "$ECR_REPOSITORY" >/dev/null 2>&1; then
  aws ecr create-repository --region "$AWS_REGION" --repository-name "$ECR_REPOSITORY" >/dev/null
fi

# Authenticate without printing the ECR password, then publish the immutable tag.
aws ecr get-login-password --region "$AWS_REGION" | \
  docker login --username AWS --password-stdin "${AWS_ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com" >/dev/null
docker tag "${LOCAL_IMAGE}:${image_tag}" "$image_uri"
docker push "$image_uri"

escape_sed() { printf '%s' "$1" | sed 's/[&|\\]/\\&/g'; }
render_task_definition() {
  local family="$1" command_json="${2:-}" output="$3"
  sed \
    -e "s|__ECS_TASK_FAMILY__|$(escape_sed "$family")|g" \
    -e "s|__ECS_EXECUTION_ROLE_ARN__|$(escape_sed "$ECS_EXECUTION_ROLE_ARN")|g" \
    -e "s|__ECS_TASK_ROLE_ARN__|$(escape_sed "$ECS_TASK_ROLE_ARN")|g" \
    -e "s|__IMAGE_URI__|$(escape_sed "$image_uri")|g" \
    -e "s|__ECS_SECRETS_ARN__|$(escape_sed "$ECS_SECRETS_ARN")|g" \
    -e "s|__CLOUDWATCH_LOG_GROUP__|$(escape_sed "$CLOUDWATCH_LOG_GROUP")|g" \
    -e "s|__AWS_REGION__|$(escape_sed "$AWS_REGION")|g" \
    -e "s|__MOCK_MODE__|$(escape_sed "$MOCK_MODE")|g" \
    -e "s|__QUEUE_ENABLED__|$(escape_sed "$QUEUE_ENABLED")|g" \
    -e "s|__AUTH_ENABLED__|$(escape_sed "$AUTH_ENABLED")|g" \
    -e "s|__SMS_PROVIDER__|$(escape_sed "$SMS_PROVIDER")|g" \
    -e "s|__WHATSAPP_PROVIDER__|$(escape_sed "$WHATSAPP_PROVIDER")|g" \
    deploy/task-definition.json > "$output"
  if [[ -n "$command_json" ]]; then
    python3 - "$output" "$command_json" <<'PY'
import json
import sys
path, command = sys.argv[1:]
with open(path, encoding="utf-8") as source:
    task = json.load(source)
task["containerDefinitions"][0]["command"] = json.loads(command)
# Worker commands do not start the HTTP server.  Keeping the API port mapping
# and /health probe would make every worker task fail its ECS health check.
task["containerDefinitions"][0].pop("portMappings", None)
task["containerDefinitions"][0].pop("healthCheck", None)
with open(path, "w", encoding="utf-8") as target:
    json.dump(task, target)
PY
  fi
}

register_task_definition() {
  local family="$1" command_json="${2:-}" rendered
  rendered="$(mktemp)"
  render_task_definition "$family" "$command_json" "$rendered"
  aws ecs register-task-definition --region "$AWS_REGION" --cli-input-json "file://${rendered}" \
    --query 'taskDefinition.taskDefinitionArn' --output text
  rm -f "$rendered"
}

network_configuration=""
if [[ -n "$ECS_SUBNET_IDS" && -n "$ECS_SECURITY_GROUP_IDS" ]]; then
  network_configuration="awsvpcConfiguration={subnets=[${ECS_SUBNET_IDS}],securityGroups=[${ECS_SECURITY_GROUP_IDS}],assignPublicIp=${ECS_ASSIGN_PUBLIC_IP}}"
fi

deploy_service() {
  local service_name="$1" task_arn="$2" desired_count="$3"
  local existing_service
  existing_service="$(aws ecs describe-services --region "$AWS_REGION" --cluster "$ECS_CLUSTER" \
    --services "$service_name" --query 'services[0].serviceArn' --output text)"
  if [[ "$existing_service" == "None" ]]; then
    [[ -n "$network_configuration" ]] || {
      echo "ECS_SUBNET_IDS and ECS_SECURITY_GROUP_IDS are required to create a new service." >&2
      exit 1
    }
    aws ecs create-service --region "$AWS_REGION" --cluster "$ECS_CLUSTER" --service-name "$service_name" \
      --task-definition "$task_arn" --desired-count "$desired_count" --launch-type FARGATE \
      --network-configuration "$network_configuration" >/dev/null
  else
    aws ecs update-service --region "$AWS_REGION" --cluster "$ECS_CLUSTER" --service "$service_name" \
      --task-definition "$task_arn" --desired-count "$desired_count" --force-new-deployment >/dev/null
  fi
}

task_definition_arn="$(register_task_definition "$ECS_TASK_FAMILY")"
deploy_service "$ECS_SERVICE" "$task_definition_arn" "$DESIRED_COUNT"

# Production queue delivery needs independent workers. Each stream consumer is
# deployed as its own Fargate service, so an API restart cannot lose delivery.
worker_services=()
if [[ "$QUEUE_ENABLED" == "true" ]]; then
  [[ "$ECS_DEPLOY_WORKERS" == "true" ]] || {
    echo "QUEUE_ENABLED=true requires ECS_DEPLOY_WORKERS=true for reliable delivery." >&2
    exit 1
  }
  for channel in sms whatsapp email; do
    worker_service="${ECS_SERVICE}-worker-${channel}"
    worker_task="$(register_task_definition "${ECS_TASK_FAMILY}-worker-${channel}" "[\"python3\",\"-m\",\"app.worker_runner\",\"${channel}\"]")"
    deploy_service "$worker_service" "$worker_task" "$ECS_WORKER_DESIRED_COUNT"
    worker_services+=("$worker_service")
  done
  retry_service="${ECS_SERVICE}-worker-retry"
  retry_task="$(register_task_definition "${ECS_TASK_FAMILY}-worker-retry" "[\"python3\",\"-m\",\"app.worker_runner\",\"--retry\"]")"
  deploy_service "$retry_service" "$retry_task" "$ECS_WORKER_DESIRED_COUNT"
  worker_services+=("$retry_service")
fi

all_services=("$ECS_SERVICE" "${worker_services[@]}")
aws ecs wait services-stable --region "$AWS_REGION" --cluster "$ECS_CLUSTER" --services "${all_services[@]}"
echo "Deployment stable"
echo "  Image: $image_uri"
echo "  Cluster: $ECS_CLUSTER"
echo "  Service: $ECS_SERVICE"
echo "  Task definition: $task_definition_arn"
aws ecs describe-services --region "$AWS_REGION" --cluster "$ECS_CLUSTER" --services "$ECS_SERVICE" \
  --query 'services[0].{status:status,running:runningCount,desired:desiredCount,taskDefinition:taskDefinition}' \
  --output table
