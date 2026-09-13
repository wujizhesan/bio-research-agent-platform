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

cd "$deploy_path"
test -f .env.production
grep -Eq '^POSTGRES_PASSWORD=.+$' .env.production
grep -Eq '^APP_ENV=production$' .env.production
grep -Eq '^CADD_JWT_SECRET=.{32,}$' .env.production
grep -Eq '^PLUGIN_SANDBOX_TOKEN=.{32,}$' .env.production
if grep -Eq '^POSTGRES_PASSWORD=(bioagent-dev-password|"bioagent-dev-password")$' .env.production; then
  echo "production PostgreSQL password must not use the development default" >&2
  exit 1
fi
test "$(grep -c '^DATABASE_URL=' .env.production)" -eq 1
test "$(grep -c '^PITR_DATABASE_URL=' .env.production)" -eq 1
test "$(grep -c '^STORAGE_BACKEND=' .env.production)" -eq 1
test "$(grep -c '^S3_BUCKET=' .env.production)" -eq 1
test "$(grep -c '^S3_REGION=' .env.production)" -eq 1
test "$(grep -c '^S3_ENDPOINT_URL=' .env.production)" -eq 1
test "$(grep -c '^S3_EXPECTED_BUCKET_OWNER=' .env.production)" -eq 1
test "$(grep -c '^S3_BACKUP_ROLE_ARN=' .env.production)" -eq 1
test "$(grep -c '^PITR_CHECKPOINT_TIMEOUT_SECONDS=' .env.production)" -eq 1
grep -Eq '^DATABASE_URL=postgres(ql)?(\+asyncpg)?://.+$' .env.production
grep -Eq '^PITR_DATABASE_URL=postgres(ql)?(\+asyncpg)?://.+$' .env.production
if grep -Eqi '^(DATABASE_URL|PITR_DATABASE_URL)=.*@(db|localhost|127\.|\[::1\])[:/]' .env.production; then
  echo "production database URLs must not target a local Compose host" >&2
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
test -f release-images.next.env

base_compose=(
  docker compose
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
  api worker web plugin-sandbox migration recovery-check recovery-evidence-publisher \
  storage-check pitr-checkpoint
"${next_compose[@]}" run --rm --no-deps storage-check
"${next_compose[@]}" run --rm --no-deps recovery-evidence-publisher
"${next_compose[@]}" run --rm --no-deps recovery-check
"${next_compose[@]}" run --rm --no-deps pitr-checkpoint
"${next_compose[@]}" run --rm migration

if "${next_compose[@]}" up -d --no-build --remove-orphans \
  && wait_for_health release-images.next.env; then
  mv release-images.next.env release-images.env
  "${next_compose[@]}" ps
  exit 0
fi

"${next_compose[@]}" ps || true
if "$had_current"; then
  echo "candidate deployment unhealthy; rolling back to previous image digests" >&2
  rollback_healthy=false
  if "${current_compose[@]}" up -d --no-build --remove-orphans \
    && wait_for_health release-images.env; then
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
