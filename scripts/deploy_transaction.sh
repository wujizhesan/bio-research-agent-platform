#!/usr/bin/env bash
set -euo pipefail

deploy_path=${1:?deploy path is required}
health_url=${2:?health URL is required}
bootstrap_config=${3:-}
health_attempts=${DEPLOY_HEALTH_ATTEMPTS:-30}
health_interval_seconds=${DEPLOY_HEALTH_INTERVAL_SECONDS:-5}
umask 077

[[ "$health_attempts" =~ ^[1-9][0-9]*$ ]]
[[ "$health_interval_seconds" =~ ^[0-9]+$ ]]
[[ -z "$bootstrap_config" || "$bootstrap_config" == "--bootstrap-config" ]]

release_secret_names=(
  DEPLOY_SMOKE_PASSWORD POSTGRES_PASSWORD CADD_JWT_SECRET RLS_CONTEXT_SIGNING_KEY
  PLUGIN_SANDBOX_TOKEN CADD_AUTH_USERS METRICS_SCRAPE_TOKEN ALERTMANAGER_WEBHOOK_URL
  API_DATABASE_URL DISPATCHER_DATABASE_URL WORKER_DATABASE_URL MAINTENANCE_DATABASE_URL
  MIGRATION_DATABASE_URL PITR_DATABASE_URL API_REDIS_URL DISPATCHER_REDIS_URL WORKER_REDIS_URL
)
release_bundle_files=(
  deploy_transaction.sh docker-compose.yml docker-compose.secure.yml docker-compose.deploy.yml
  verify_public_deployment.py monitoring/prometheus.yml monitoring/alertmanager.yml
  monitoring/storage-deletion-alerts.yml
)

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
  (( (8#$mode & 037) == 0 ))
  tr -d '\r\n' < "$secret_path"
}

secret_file_digest() {
  local environment_path=$1
  local name=$2
  local secret_path
  secret_path=$(environment_value "$environment_path" "${name}_FILE")
  secret_file_value "$environment_path" "$name" > /dev/null
  sha256sum < "$secret_path" | cut -d ' ' -f 1
}

release_bundle_path() {
  local state_path=$1
  local bundle_path
  bundle_path=$(environment_value "$state_path" RELEASE_BUNDLE_DIR)
  [[ "$bundle_path" =~ ^release-bundles/[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]]
  printf '%s' "$bundle_path"
}

release_bundle_digest() {
  local bundle_path=$1
  local version=${2:-3}
  local file_name
  local -a files=("${release_bundle_files[@]}")
  if test "$version" = 2; then
    files=("${release_bundle_files[@]:1}")
  else
    test "$version" = 3
  fi
  test -d "$bundle_path"
  test ! -L release-bundles
  test ! -L "$bundle_path"
  test ! -L "$bundle_path/monitoring"
  for file_name in "${files[@]}"; do
    test -f "$bundle_path/$file_name"
    test ! -L "$bundle_path/$file_name"
  done
  (cd "$bundle_path" && sha256sum "${files[@]}") \
    | sha256sum | cut -d ' ' -f 1
}

snapshot_legacy_bundle() {
  local bundle_path=$1
  local file_name
  test ! -L monitoring
  mkdir -p "$bundle_path/monitoring"
  for file_name in "${release_bundle_files[@]}"; do
    test -f "$file_name"
    test ! -L "$file_name"
    cp --preserve=mode,timestamps -- "$file_name" "$bundle_path/$file_name"
  done
}

restore_legacy_monitoring() {
  local state_path=$1
  local bundle_path file_name
  if test "$(environment_value "$state_path" MONITORING_CONFIG_DIR)" != ./monitoring; then
    return 0
  fi
  bundle_path=$(release_bundle_path "$state_path")
  for file_name in prometheus.yml alertmanager.yml storage-deletion-alerts.yml; do
    test ! -L "monitoring/$file_name"
    cp --preserve=mode,timestamps -- "$bundle_path/monitoring/$file_name" "monitoring/$file_name"
  done
}

write_release_state() {
  local config_path=$1
  local image_path=$2
  local output_path=$3
  local bundle_path=$4
  local monitoring_dir=$5
  local backend_image frontend_image release_tag git_sha bundle_digest secret_name expected_bundle_digest
  backend_image=$(environment_value "$image_path" BACKEND_IMAGE)
  frontend_image=$(environment_value "$image_path" FRONTEND_IMAGE)
  release_tag=$(environment_value "$image_path" RELEASE_TAG)
  git_sha=$(environment_value "$image_path" GIT_SHA)
  bundle_digest=$(release_bundle_digest "$bundle_path")
  if test "$image_path" = release-images.next.env; then
    expected_bundle_digest=$(environment_value "$image_path" RELEASE_BUNDLE_SHA256)
    [[ "$expected_bundle_digest" =~ ^[0-9a-f]{64}$ ]]
    if test "$expected_bundle_digest" != "$bundle_digest"; then
      echo "candidate release bundle differs from verified source" >&2
      return 1
    fi
  fi
  install -m 600 "$config_path" "$output_path"
  printf '\nBACKEND_IMAGE=%s\nFRONTEND_IMAGE=%s\nRELEASE_TAG=%s\nGIT_SHA=%s\nRELEASE_CONFIG_VERSION=3\nRELEASE_BUNDLE_DIR=%s\nRELEASE_BUNDLE_SHA256=%s\nMONITORING_CONFIG_DIR=%s\n' \
    "$backend_image" "$frontend_image" "$release_tag" "$git_sha" \
    "$bundle_path" "$bundle_digest" "$monitoring_dir" >> "$output_path"
  for secret_name in "${release_secret_names[@]}"; do
    printf '%s_SHA256=%s\n' "$secret_name" \
      "$(secret_file_digest "$config_path" "$secret_name")" >> "$output_path"
  done
}

verify_release_state() {
  local state_path=$1
  local name expected actual bundle_path version
  version=$(environment_value "$state_path" RELEASE_CONFIG_VERSION)
  [[ "$version" = 2 || "$version" = 3 ]]
  bundle_path=$(release_bundle_path "$state_path")
  expected=$(environment_value "$state_path" RELEASE_BUNDLE_SHA256)
  [[ "$expected" =~ ^[0-9a-f]{64}$ ]]
  actual=$(release_bundle_digest "$bundle_path" "$version")
  if test "$expected" != "$actual"; then
    echo "release bundle changed after snapshot" >&2
    return 1
  fi
  actual=$(environment_value "$state_path" MONITORING_CONFIG_DIR)
  if test "$actual" != "./$bundle_path/monitoring" && test "$actual" != ./monitoring; then
    echo "release monitoring directory is invalid" >&2
    return 1
  fi
  for name in "${release_secret_names[@]}"; do
    expected=$(environment_value "$state_path" "${name}_SHA256")
    [[ "$expected" =~ ^[0-9a-f]{64}$ ]]
    actual=$(secret_file_digest "$state_path" "$name")
    if test "$expected" != "$actual"; then
      echo "${name} file changed after release snapshot; use a new immutable path" >&2
      return 1
    fi
  done
}

verify_legacy_release_state() {
  local name expected actual
  test "$(environment_value release-images.env RELEASE_CONFIG_VERSION)" = 1
  cmp -n "$(wc -c < .env.production)" .env.production release-images.env
  for name in CADD_JWT_SECRET RLS_CONTEXT_SIGNING_KEY; do
    expected=$(environment_value release-images.env "${name}_SHA256")
    actual=$(secret_file_digest release-images.env "$name")
    test "$expected" = "$actual"
  done
}

verify_live_release() {
  local service expected_image expected_hash hash_line container_ids container_id actual_image actual_hash
  local started_at started_seconds secret_name secret_path secret_modified
  for service in api worker dispatcher artifact-maintenance plugin-sandbox plugin-sandbox-heavy web; do
    expected_image=$(environment_value release-images.env BACKEND_IMAGE)
    if test "$service" = web; then
      expected_image=$(environment_value release-images.env FRONTEND_IMAGE)
    fi
    hash_line=$("${bootstrap_compose[@]}" config --hash "$service")
    expected_hash=${hash_line#"$service "}
    if [[ "$hash_line" != "$service "* || ! "$expected_hash" =~ ^[0-9a-f]{64}$ ]]; then
      echo "cannot verify live $service configuration hash" >&2
      return 1
    fi
    container_ids=$("${bootstrap_compose[@]}" ps -q "$service")
    if test -z "$container_ids"; then
      echo "cannot bootstrap: $service has no running container" >&2
      return 1
    fi
    for container_id in $container_ids; do
      actual_image=$(docker inspect --format '{{.Config.Image}}' "$container_id")
      actual_hash=$(docker inspect --format '{{index .Config.Labels "com.docker.compose.config-hash"}}' "$container_id")
      if test "$actual_image" != "$expected_image" || test "$actual_hash" != "$expected_hash"; then
        echo "cannot bootstrap: live $service differs from current release configuration" >&2
        return 1
      fi
      if test "$service" = api; then
        started_at=$(docker inspect --format '{{.State.StartedAt}}' "$container_id")
        started_seconds=$(date -d "$started_at" +%s)
        for secret_name in CADD_JWT_SECRET RLS_CONTEXT_SIGNING_KEY; do
          secret_path=$(environment_value .env.production "${secret_name}_FILE")
          secret_modified=$(stat -c '%Y' "$secret_path")
          if ((secret_modified > started_seconds)); then
            echo "cannot bootstrap: ${secret_name} file changed after live API started" >&2
            return 1
          fi
        done
      fi
    done
  done
}

wait_for_health() {
  local image_environment=$1
  local attempt
  local release_tag
  local git_sha
  local target_health_url
  release_tag=$(environment_value "$image_environment" RELEASE_TAG)
  git_sha=$(environment_value "$image_environment" GIT_SHA 2> /dev/null || printf 'unknown')
  target_health_url=$(environment_value "$image_environment" PUBLIC_BASE_URL 2> /dev/null || printf '%s' "${health_url%/health}")
  target_health_url="${target_health_url%/}/health"
  for ((attempt = 1; attempt <= health_attempts; attempt++)); do
    if curl --fail --silent --show-error \
      --header "X-Expected-Release: ${release_tag}" \
      --header "X-Expected-Commit: ${git_sha}" \
      "$target_health_url" > /dev/null; then
      return 0
    fi
    if ((attempt < health_attempts)); then
      sleep "$health_interval_seconds"
    fi
  done
  return 1
}

verify_public_deployment() {
  local state_path=$1
  local bundle_path
  bundle_path=$(release_bundle_path "$state_path")
  python3 "$bundle_path/verify_public_deployment.py" \
    --base-url "$(environment_value "$state_path" PUBLIC_BASE_URL)" \
    --username "$(environment_value "$state_path" DEPLOY_SMOKE_USERNAME)" \
    --password-file "$(environment_value "$state_path" DEPLOY_SMOKE_PASSWORD_FILE)" \
    --job-tool "$(environment_value "$state_path" DEPLOY_SMOKE_JOB_TOOL)" \
    --job-timeout-seconds "$(environment_value "$state_path" DEPLOY_SMOKE_JOB_TIMEOUT_SECONDS)"
}

cd "$deploy_path"
test -f .env.production
grep -Eq '^APP_ENV=production$' .env.production
if grep -Eq '^(POSTGRES_PASSWORD|CADD_JWT_SECRET|RLS_CONTEXT_SIGNING_KEY|PLUGIN_SANDBOX_TOKEN|METRICS_SCRAPE_TOKEN|ALERTMANAGER_WEBHOOK_URL|API_DATABASE_URL|DISPATCHER_DATABASE_URL|WORKER_DATABASE_URL|MIGRATION_DATABASE_URL|PITR_DATABASE_URL|API_REDIS_URL|DISPATCHER_REDIS_URL|WORKER_REDIS_URL|AWS_ACCESS_KEY_ID|AWS_SECRET_ACCESS_KEY|AWS_SESSION_TOKEN|RESEARCH_PLANNER_API_KEY|OPENAI_API_KEY|CADD_API_KEY|NCBI_API_KEY)=' .env.production; then
  echo "production secrets must be supplied through *_FILE" >&2
  exit 1
fi
if grep -Eq '^(BACKEND_IMAGE|FRONTEND_IMAGE|RELEASE_TAG|GIT_SHA|RELEASE_CONFIG_VERSION|RELEASE_BUNDLE_DIR|RELEASE_BUNDLE_SHA256|MONITORING_CONFIG_DIR|[A-Z0-9_]+_SHA256)=' .env.production; then
  echo "image and release-state fields must not be set in .env.production" >&2
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
test "$(grep -c '^MONITORING_SECRET_GID=' .env.production)" -eq 1
if grep -q '^RLS_CONTEXT_KEY_ID=' .env.production; then
  grep -Eq '^RLS_CONTEXT_KEY_ID=[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' .env.production
fi
if grep -q '^RLS_CONTEXT_ROTATION_GRACE_SECONDS=' .env.production; then
  grep -Eq '^RLS_CONTEXT_ROTATION_GRACE_SECONDS=[1-9][0-9]*$' .env.production
fi
grep -Eq '^PUBLIC_BASE_URL=https://[A-Za-z0-9.-]+(:[0-9]{1,5})?$' .env.production
grep -Eq '^WEB_PUBLISHED_PORT=[0-9]{1,5}$' .env.production
grep -Eq '^DEPLOY_SMOKE_USERNAME=[A-Za-z0-9._@+-]+$' .env.production
grep -Eq '^DEPLOY_SMOKE_PASSWORD_FILE=/[A-Za-z0-9._/-]+$' .env.production
grep -Eq '^DEPLOY_SMOKE_JOB_TOOL=[A-Za-z0-9_.:-]+$' .env.production
grep -Eq '^DEPLOY_SMOKE_JOB_TIMEOUT_SECONDS=[1-9][0-9]*$' .env.production
grep -Eq '^TRUSTED_PROXY_CIDRS=[0-9A-Fa-f:.,/]+$' .env.production
grep -Eq '^MONITORING_SECRET_GID=[1-9][0-9]*$' .env.production
postgres_password=$(secret_file_value .env.production POSTGRES_PASSWORD)
if test "$postgres_password" = "bioagent-dev-password"; then
  echo "production PostgreSQL password must not use the development default" >&2
  exit 1
fi
for secret_name in "${release_secret_names[@]}"; do
  test "$(grep -c "^${secret_name}_FILE=" .env.production)" -eq 1
  secret_file_value .env.production "$secret_name" > /dev/null
done
for strong_secret in CADD_JWT_SECRET RLS_CONTEXT_SIGNING_KEY PLUGIN_SANDBOX_TOKEN METRICS_SCRAPE_TOKEN; do
  strong_secret_value=$(secret_file_value .env.production "$strong_secret")
  test "${#strong_secret_value}" -ge 32
done
monitoring_secret_gid=$(environment_value .env.production MONITORING_SECRET_GID)
((monitoring_secret_gid >= 1 && monitoring_secret_gid <= 2147483647))
for monitoring_secret in METRICS_SCRAPE_TOKEN ALERTMANAGER_WEBHOOK_URL; do
  monitoring_secret_path=$(environment_value .env.production "${monitoring_secret}_FILE")
  test "$(stat -c '%g' "$monitoring_secret_path")" -eq "$monitoring_secret_gid"
  monitoring_secret_mode=$(stat -c '%a' "$monitoring_secret_path")
  (( (8#$monitoring_secret_mode & 040) != 0 ))
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
base_compose=(
  docker compose
  --profile monitoring
  -f docker-compose.yml
  -f docker-compose.secure.yml
  -f docker-compose.deploy.yml
)
if test "$bootstrap_config" = "--bootstrap-config"; then
  test -f release-images.env
  test -f verify_public_deployment.py
  if grep -Eq '^RELEASE_CONFIG_VERSION=(2|3)$' release-images.env; then
    echo "current release already has a config snapshot" >&2
    exit 1
  fi
  if grep -q '^RELEASE_CONFIG_VERSION=1$' release-images.env; then
    verify_legacy_release_state
    bootstrap_compose=("${base_compose[@]}" --env-file release-images.env)
  else
    bootstrap_compose=(
      "${base_compose[@]}"
      --env-file .env.production
      --env-file release-images.env
    )
  fi
  verify_live_release
  if ! wait_for_health release-images.env; then
    echo "cannot bootstrap: current release health check failed" >&2
    exit 1
  fi
  mkdir -p release-bundles
  bootstrap_bundle=$(mktemp -d "$deploy_path/release-bundles/bootstrap.XXXXXX")
  bootstrap_bundle=${bootstrap_bundle#"$deploy_path/"}
  snapshot_legacy_bundle "$bootstrap_bundle"
  snapshot=$(mktemp "$deploy_path/.release-bootstrap.XXXXXX")
  write_release_state .env.production release-images.env "$snapshot" "$bootstrap_bundle" ./monitoring
  cp release-images.env release-images.prebootstrap.env
  mv -- "$snapshot" release-images.env
  echo "current release bundle snapshot created; rotate secrets only with new file paths"
  exit 0
fi
test -f release-images.next.env
if test -f release-images.env; then
  if ! grep -Eq '^RELEASE_CONFIG_VERSION=(2|3)$' release-images.env; then
    echo "current release lacks a bundle snapshot; run --bootstrap-config before changing keys" >&2
    exit 1
  fi
  verify_release_state release-images.env
fi
candidate_bundle=$(release_bundle_path release-images.next.env)
candidate_snapshot=$(mktemp "$deploy_path/.release-candidate.XXXXXX")
write_release_state .env.production release-images.next.env "$candidate_snapshot" "$candidate_bundle" "./$candidate_bundle/monitoring"
mv -- "$candidate_snapshot" release-images.next.env
verify_release_state release-images.next.env

next_compose=(
  docker compose --project-directory "$deploy_path" --profile monitoring
  -f "$candidate_bundle/docker-compose.yml"
  -f "$candidate_bundle/docker-compose.secure.yml"
  -f "$candidate_bundle/docker-compose.deploy.yml"
  --env-file release-images.next.env
)
had_current=false
if test -f release-images.env; then
  had_current=true
  current_bundle=$(release_bundle_path release-images.env)
  current_compose=(
    docker compose --project-directory "$deploy_path" --profile monitoring
    -f "$current_bundle/docker-compose.yml"
    -f "$current_bundle/docker-compose.secure.yml"
    -f "$current_bundle/docker-compose.deploy.yml"
    --env-file release-images.env
  )
  cp release-images.env release-images.previous.env
fi

"${next_compose[@]}" config --quiet
"${next_compose[@]}" pull \
  api dispatcher worker artifact-maintenance web plugin-sandbox plugin-sandbox-heavy migration recovery-check recovery-evidence-publisher \
  storage-check pitr-checkpoint prometheus alertmanager
"${next_compose[@]}" run --rm --no-deps api \
  python -c 'from src.auth import AuthService; AuthService.from_env()'
"${next_compose[@]}" run --rm --no-deps storage-check
"${next_compose[@]}" run --rm --no-deps recovery-evidence-publisher
"${next_compose[@]}" run --rm --no-deps recovery-check
"${next_compose[@]}" run --rm --no-deps pitr-checkpoint
"${next_compose[@]}" run --rm migration python scripts/verify_database_roles.py
"${next_compose[@]}" run --rm migration
"${next_compose[@]}" run --rm artifact-maintenance \
  python -m src.artifact_backfill --apply --artifact-root /app/output
"${next_compose[@]}" run --rm migration \
  python scripts/configure_tenant_context.py --stage

if verify_release_state release-images.next.env \
  && "${next_compose[@]}" up -d --no-build --remove-orphans \
  && "${next_compose[@]}" run --rm migration \
    python scripts/configure_tenant_context.py --enable \
  && wait_for_health release-images.next.env \
  && verify_public_deployment release-images.next.env \
  && verify_release_state release-images.next.env; then
    mv release-images.next.env release-images.env
    "${next_compose[@]}" ps
    exit 0
fi

"${next_compose[@]}" ps || true
if "$had_current"; then
  echo "candidate deployment unhealthy; rolling back to previous release state" >&2
  rollback_healthy=false
  candidate_backend_image=$(environment_value release-images.next.env BACKEND_IMAGE)
  if verify_release_state release-images.env \
    && restore_legacy_monitoring release-images.env \
    && BACKEND_IMAGE="$candidate_backend_image" "${current_compose[@]}" run --rm migration \
      python scripts/configure_tenant_context.py --enable \
    && "${current_compose[@]}" up -d --no-build --remove-orphans \
    && wait_for_health release-images.env \
    && verify_public_deployment release-images.env; then
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
