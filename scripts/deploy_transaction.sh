#!/usr/bin/env bash
set -euo pipefail

deploy_path=${1:?deploy path is required}
health_url=${2:?health URL is required}
health_attempts=${DEPLOY_HEALTH_ATTEMPTS:-30}
health_interval_seconds=${DEPLOY_HEALTH_INTERVAL_SECONDS:-5}

[[ "$health_attempts" =~ ^[1-9][0-9]*$ ]]
[[ "$health_interval_seconds" =~ ^[0-9]+$ ]]

environment_value() {
  local path=$1
  local name=$2
  local value
  value=$(sed -n "s/^${name}=//p" "$path")
  test -n "$value"
  test "$(grep -c "^${name}=" "$path")" -eq 1
  printf '%s' "$value"
}

secret_file_value() {
  local environment_path=$1
  local name=$2
  local secret_path
  secret_path=$(environment_value "$environment_path" "${name}_FILE")
  [[ "$secret_path" = /* ]]
  [[ "$secret_path" != *"/../"* ]]
  [[ "$secret_path" != *"/.." ]]
  test -f "$secret_path"
  test -r "$secret_path"
  test "$(wc -c < "$secret_path")" -le 65536
  local mode
  mode=$(stat -c '%a' "$secret_path")
  (( (8#$mode & 077) == 0 ))
  tr -d '\r\n' < "$secret_path"
}

wait_for_health() {
  local image_environment=$1
  local attempt
  local release_tag
  local git_sha
  release_tag=$(environment_value "$image_environment" RELEASE_TAG)
  git_sha=$(environment_value "$image_environment" GIT_SHA 2> /dev/null || printf 'unknown')
  for ((attempt = 1; attempt <= health_attempts; attempt++)); do
    if curl --fail --silent --show-error \
      --header "X-Expected-Release: ${release_tag}" \
      --header "X-Expected-Commit: ${git_sha}" \
      "$health_url" > /dev/null; then
      return 0
    fi
    if ((attempt < health_attempts)); then
      sleep "$health_interval_seconds"
    fi
  done
  return 1
}

verify_public_deployment() {
  python3 verify_public_deployment.py \
    --base-url "$public_base_url" \
    --username "$smoke_username" \
    --password-file "$smoke_password_file" \
    --job-tool "$smoke_job_tool" \
    --job-timeout-seconds "$smoke_job_timeout_seconds"
}

cd "$deploy_path"
test -f .env.production
grep -Eq '^APP_ENV=production$' .env.production
if grep -Eq '^(POSTGRES_PASSWORD|CADD_JWT_SECRET|RLS_CONTEXT_SIGNING_KEY|PLUGIN_SANDBOX_TOKEN|METRICS_SCRAPE_TOKEN|ALERTMANAGER_WEBHOOK_URL|API_DATABASE_URL|DISPATCHER_DATABASE_URL|WORKER_DATABASE_URL|MIGRATION_DATABASE_URL|PITR_DATABASE_URL|API_REDIS_URL|DISPATCHER_REDIS_URL|WORKER_REDIS_URL|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|RESEARCH_PLANNER_API_KEY|OPENAI_API_KEY|CADD_API_KEY|NCBI_API_KEY)=' .env.production; then
  echo "production secrets must be supplied through *_FILE" >&2
  exit 1
fi
test "$(grep -c '^PUBLIC_BASE_URL=' .env.production)" -eq 1
test "$(grep -c '^CORS_ORIGINS=' .env.production)" -eq 1
test "$(grep -c '^WEB_PUBLISHED_PORT=' .env.production)" -eq 1
test "$(grep -c '^DEPLOY_SMOKE_USERNAME=' .env.production)" -eq 1
test "$(grep -c '^DEPLOY_SMOKE_PASSWORD_FILE=' .env.production)" -eq 1
test "$(grep -c '^DEPLOY_SMOKE_JOB_TOOL=' .env.production)" -eq 1
test "$(grep -c '^DEPLOY_SMOKE_JOB_TIMEOUT_SECONDS=' .env.production)" -eq 1
test "$(grep -c '^TRUSTED_PROXY_CIDRS=' .env.production)" -eq 1
grep -Eq '^PUBLIC_BASE_URL=https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$' .env.production
grep -Eq '^WEB_PUBLISHED_PORT=[0-9]{1,5}$' .env.production
grep -Eq '^DEPLOY_SMOKE_USERNAME=[A-Za-z0-9._@+-]+$' .env.production
grep -Eq '^DEPLOY_SMOKE_PASSWORD_FILE=/[A-Za-z0-9._/-]+$' .env.production
grep -Eq '^DEPLOY_SMOKE_JOB_TOOL=[A-Za-z0-9_.:-]+$' .env.production
grep -Eq '^DEPLOY_SMOKE_JOB_TIMEOUT_SECONDS=[1-9][0-9]*$' .env.production
grep -Eq '^TRUSTED_PROXY_CIDRS=[0-9A-Fa-f:.,/]+$' .env.production
postgres_password=$(secret_file_value .env.production POSTGRES_PASSWORD)
if test "$postgres_password" = "bioagent-dev-password"; then
  echo "production PostgreSQL password must not use the development default" >&2
  exit 1
fi
for secret_name in \
  POSTGRES_PASSWORD CADD_JWT_SECRET RLS_CONTEXT_SIGNING_KEY PLUGIN_SANDBOX_TOKEN CADD_AUTH_USERS METRICS_SCRAPE_TOKEN ALERTMANAGER_WEBHOOK_URL \
  API_DATABASE_URL DISPATCHER_DATABASE_URL WORKER_DATABASE_URL MAINTENANCE_DATABASE_URL MIGRATION_DATABASE_URL PITR_DATABASE_URL \
  API_REDIS_URL DISPATCHER_REDIS_URL WORKER_REDIS_URL; do
  test "$(grep -c "^${secret_name}_FILE=" .env.production)" -eq 1
  secret_file_value .env.production "$secret_name" > /dev/null
done
for strong_secret in CADD_JWT_SECRET RLS_CONTEXT_SIGNING_KEY PLUGIN_SANDBOX_TOKEN METRICS_SCRAPE_TOKEN; do
  strong_secret_value=$(secret_file_value .env.production "$strong_secret")
  test "${#strong_secret_value}" -ge 32
done
alertmanager_webhook_url=$(secret_file_value .env.production ALERTMANAGER_WEBHOOK_URL)
[[ "$alertmanager_webhook_url" =~ ^https://[^[:space:]]+$ ]]
test "$(grep -c '^STORAGE_BACKEND=' .env.production)" -eq 1
test "$(grep -c '^S3_BUCKET=' .env.production)" -eq 1
test "$(grep -c '^S3_REGION=' .env.production)" -eq 1
test "$(grep -c '^S3_ENDPOINT_URL=' .env.production)" -eq 1
test "$(grep -c '^S3_EXPECTED_BUCKET_OWNER=' .env.production)" -eq 1
test "$(grep -c '^S3_BACKUP_ROLE_ARN=' .env.production)" -eq 1
test "$(grep -c '^PITR_CHECKPOINT_TIMEOUT_SECONDS=' .env.production)" -eq 1
api_database_url=$(secret_file_value .env.production API_DATABASE_URL)
dispatcher_database_url=$(secret_file_value .env.production DISPATCHER_DATABASE_URL)
worker_database_url=$(secret_file_value .env.production WORKER_DATABASE_URL)
maintenance_database_url=$(secret_file_value .env.production MAINTENANCE_DATABASE_URL)
migration_database_url=$(secret_file_value .env.production MIGRATION_DATABASE_URL)
pitr_database_url=$(secret_file_value .env.production PITR_DATABASE_URL)
api_redis_url=$(secret_file_value .env.production API_REDIS_URL)
dispatcher_redis_url=$(secret_file_value .env.production DISPATCHER_REDIS_URL)
worker_redis_url=$(secret_file_value .env.production WORKER_REDIS_URL)
[[ "$api_database_url" =~ ^postgres(ql)?(\+asyncpg)?://.+$ ]]
[[ "$dispatcher_database_url" =~ ^postgres(ql)?(\+asyncpg)?://.+$ ]]
[[ "$worker_database_url" =~ ^postgres(ql)?(\+asyncpg)?://.+$ ]]
[[ "$maintenance_database_url" =~ ^postgres(ql)?(\+asyncpg)?://.+$ ]]
[[ "$migration_database_url" =~ ^postgres(ql)?(\+asyncpg)?://.+$ ]]
[[ "$pitr_database_url" =~ ^postgres(ql)?(\+asyncpg)?://.+$ ]]
[[ "$api_redis_url" =~ ^rediss?://.+$ ]]
[[ "$dispatcher_redis_url" =~ ^rediss?://.+$ ]]
[[ "$worker_redis_url" =~ ^rediss?://.+$ ]]
if printf '%s\n' "$api_database_url" "$dispatcher_database_url" "$worker_database_url" "$maintenance_database_url" "$migration_database_url" "$pitr_database_url" | grep -Eqi '@(db|localhost|127\.|\[::1\])[:/]'; then
  echo "production database URLs must not target a local Compose host" >&2
  exit 1
fi
if test "$api_database_url" = "$dispatcher_database_url" \
  || test "$api_database_url" = "$worker_database_url" \
  || test "$api_database_url" = "$migration_database_url" \
  || test "$api_database_url" = "$maintenance_database_url" \
  || test "$dispatcher_database_url" = "$worker_database_url" \
  || test "$dispatcher_database_url" = "$migration_database_url" \
  || test "$dispatcher_database_url" = "$maintenance_database_url" \
  || test "$worker_database_url" = "$migration_database_url" \
  || test "$worker_database_url" = "$maintenance_database_url" \
  || test "$maintenance_database_url" = "$migration_database_url"; then
  echo "production API, Dispatcher, Worker, Maintenance and Migration database URLs must use distinct identities" >&2
  exit 1
fi
if test "$api_redis_url" = "$dispatcher_redis_url" \
  || test "$api_redis_url" = "$worker_redis_url" \
  || test "$dispatcher_redis_url" = "$worker_redis_url"; then
  echo "production API, Dispatcher and Worker Redis URLs must use distinct ACL identities" >&2
  exit 1
fi
grep -Eq '^STORAGE_BACKEND=s3$' .env.production
grep -Eq '^S3_BUCKET=[A-Za-z0-9][A-Za-z0-9.-]{1,61}[A-Za-z0-9]$' .env.production
grep -Eq '^S3_REGION=[a-z0-9-]+$' .env.production
grep -Eq '^S3_ENDPOINT_URL=https://[^[:space:]]+$' .env.production
grep -Eq '^S3_EXPECTED_BUCKET_OWNER=[0-9]{12}$' .env.production
grep -Eq '^S3_BACKUP_ROLE_ARN=arn:(aws|aws-cn|aws-us-gov):iam::[0-9]{12}:role/.+$' .env.production
grep -Eq '^PITR_CHECKPOINT_TIMEOUT_SECONDS=[1-9][0-9]*$' .env.production
test "$(grep -c '^RECOVERY_EVIDENCE_PATH=' .env.production)" -eq 1
test "$(grep -c '^RECOVERY_EVIDENCE_DIRECTORY=' .env.production)" -eq 1
test "$(grep -c '^RECOVERY_BACKUP_MANIFEST_PATH=' .env.production)" -eq 1
test "$(grep -c '^RECOVERY_RESTORE_REPORT_PATH=' .env.production)" -eq 1
test "$(grep -c '^RECOVERY_EVIDENCE_HMAC_KEY_PATH=' .env.production)" -eq 1
test "$(grep -c '^RECOVERY_POINT_MAX_AGE_SECONDS=' .env.production)" -eq 1
test "$(grep -c '^RECOVERY_VERIFICATION_MAX_AGE_SECONDS=' .env.production)" -eq 1
grep -Eq '^RECOVERY_EVIDENCE_PATH=/[A-Za-z0-9._/-]+$' .env.production
grep -Eq '^RECOVERY_EVIDENCE_DIRECTORY=/[A-Za-z0-9._/-]+$' .env.production
grep -Eq '^RECOVERY_BACKUP_MANIFEST_PATH=/[A-Za-z0-9._/-]+$' .env.production
grep -Eq '^RECOVERY_RESTORE_REPORT_PATH=/[A-Za-z0-9._/-]+$' .env.production
grep -Eq '^RECOVERY_EVIDENCE_HMAC_KEY_PATH=/[A-Za-z0-9._/-]+$' .env.production
for path_name in \
  RECOVERY_EVIDENCE_PATH \
  RECOVERY_EVIDENCE_DIRECTORY \
  RECOVERY_BACKUP_MANIFEST_PATH \
  RECOVERY_RESTORE_REPORT_PATH \
  RECOVERY_EVIDENCE_HMAC_KEY_PATH; do
  recovery_path=$(environment_value .env.production "$path_name")
  [[ "$recovery_path" != *"/../"* ]]
  [[ "$recovery_path" != *"/.." ]]
done
grep -Eq '^RECOVERY_POINT_MAX_AGE_SECONDS=[1-9][0-9]*$' .env.production
grep -Eq '^RECOVERY_VERIFICATION_MAX_AGE_SECONDS=[1-9][0-9]*$' .env.production
evidence_path=$(environment_value .env.production RECOVERY_EVIDENCE_PATH)
evidence_directory=$(environment_value .env.production RECOVERY_EVIDENCE_DIRECTORY)
test "$evidence_path" = "${evidence_directory%/}/latest-production.json"
public_base_url=$(environment_value .env.production PUBLIC_BASE_URL)
cors_origins=$(environment_value .env.production CORS_ORIGINS)
web_published_port=$(environment_value .env.production WEB_PUBLISHED_PORT)
smoke_username=$(environment_value .env.production DEPLOY_SMOKE_USERNAME)
smoke_password_file=$(environment_value .env.production DEPLOY_SMOKE_PASSWORD_FILE)
smoke_job_tool=$(environment_value .env.production DEPLOY_SMOKE_JOB_TOOL)
smoke_job_timeout_seconds=$(environment_value .env.production DEPLOY_SMOKE_JOB_TIMEOUT_SECONDS)
((web_published_port >= 1 && web_published_port <= 65535))
[[ "$smoke_password_file" != *"/../"* ]]
[[ "$smoke_password_file" != *"/.." ]]
test -r "$smoke_password_file"
smoke_password_mode=$(stat -c '%a' "$smoke_password_file")
(( (8#$smoke_password_mode & 077) == 0 ))
test "$cors_origins" = "$public_base_url"
test "$health_url" = "${public_base_url%/}/health"
test -f verify_public_deployment.py
test -f release-images.next.env

base_compose=(
  docker compose
  --profile monitoring
  --env-file .env.production
  -f docker-compose.yml
  -f docker-compose.secure.yml
  -f docker-compose.deploy.yml
)
next_compose=("${base_compose[@]}" --env-file release-images.next.env)
had_current=false
if test -f release-images.env; then
  had_current=true
  current_compose=("${base_compose[@]}" --env-file release-images.env)
  cp release-images.env release-images.previous.env
fi

"${next_compose[@]}" config --quiet
"${next_compose[@]}" pull \
  api dispatcher worker artifact-maintenance web plugin-sandbox migration recovery-check recovery-evidence-publisher \
  storage-check pitr-checkpoint prometheus alertmanager
"${next_compose[@]}" run --rm --no-deps storage-check
"${next_compose[@]}" run --rm --no-deps recovery-evidence-publisher
"${next_compose[@]}" run --rm --no-deps recovery-check
"${next_compose[@]}" run --rm --no-deps pitr-checkpoint
"${next_compose[@]}" run --rm migration python scripts/verify_database_roles.py
"${next_compose[@]}" run --rm migration
"${next_compose[@]}" run --rm artifact-maintenance \
  python -m src.artifact_backfill --apply --artifact-root /app/output

if "${next_compose[@]}" up -d --no-build --remove-orphans \
  && "${next_compose[@]}" run --rm migration \
    python scripts/configure_tenant_context.py --enable \
  && wait_for_health release-images.next.env \
  && verify_public_deployment; then
    mv release-images.next.env release-images.env
    "${next_compose[@]}" ps
    exit 0
fi

"${next_compose[@]}" ps || true
"${next_compose[@]}" run --rm migration \
  python scripts/configure_tenant_context.py --disable || true
if "$had_current"; then
  echo "candidate deployment unhealthy; rolling back to previous image digests" >&2
  rollback_healthy=false
  if "${current_compose[@]}" up -d --no-build --remove-orphans \
    && wait_for_health release-images.env \
    && verify_public_deployment; then
    rollback_healthy=true
  fi
  "${current_compose[@]}" ps || true
  if "$rollback_healthy"; then
    echo "rollback completed; deployment remains failed" >&2
  else
    echo "rollback failed to restore a healthy service" >&2
  fi
else
  echo "candidate deployment unhealthy and no previous image set exists" >&2
fi
exit 1
