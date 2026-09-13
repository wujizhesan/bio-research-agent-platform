#!/usr/bin/env bash
set -euo pipefail

deploy_path=${1:?deploy path is required}
health_url=${2:?health URL is required}
health_attempts=${DEPLOY_HEALTH_ATTEMPTS:-30}
health_interval_seconds=${DEPLOY_HEALTH_INTERVAL_SECONDS:-5}

[[ "$health_attempts" =~ ^[1-9][0-9]*$ ]]
[[ "$health_interval_seconds" =~ ^[0-9]+$ ]]

wait_for_health() {
  local attempt
  for ((attempt = 1; attempt <= health_attempts; attempt++)); do
    if curl --fail --silent --show-error "$health_url" > /dev/null; then
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
grep -Eq '^CADD_JWT_SECRET=.{32,}$' .env.production
grep -Eq '^PLUGIN_SANDBOX_TOKEN=.{32,}$' .env.production
! grep -Eq '^POSTGRES_PASSWORD=(bioagent-dev-password|"bioagent-dev-password")$' .env.production
test "$(grep -c '^RECOVERY_EVIDENCE_PATH=' .env.production)" -eq 1
test "$(grep -c '^RECOVERY_POINT_MAX_AGE_SECONDS=' .env.production)" -eq 1
test "$(grep -c '^RECOVERY_VERIFICATION_MAX_AGE_SECONDS=' .env.production)" -eq 1
grep -Eq '^RECOVERY_EVIDENCE_PATH=/[A-Za-z0-9._/-]+$' .env.production
! grep -Eq '^RECOVERY_EVIDENCE_PATH=.*\.\.' .env.production
grep -Eq '^RECOVERY_POINT_MAX_AGE_SECONDS=[1-9][0-9]*$' .env.production
grep -Eq '^RECOVERY_VERIFICATION_MAX_AGE_SECONDS=[1-9][0-9]*$' .env.production
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
"${next_compose[@]}" pull api worker web plugin-sandbox migration recovery-check
"${next_compose[@]}" run --rm --no-deps recovery-check
"${next_compose[@]}" run --rm migration

if "${next_compose[@]}" up -d --no-build --remove-orphans && wait_for_health; then
  mv release-images.next.env release-images.env
  "${next_compose[@]}" ps
  exit 0
fi

"${next_compose[@]}" ps || true
if "$had_current"; then
  echo "candidate deployment unhealthy; rolling back to previous image digests" >&2
  rollback_healthy=false
  if "${current_compose[@]}" up -d --no-build --remove-orphans && wait_for_health; then
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
