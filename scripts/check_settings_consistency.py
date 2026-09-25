from pathlib import Path
import re
import sys

import yaml


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.settings import PlatformSettings


COMMON = {
    'APP_ENV', 'APP_RELEASE_TAG', 'APP_GIT_SHA', 'APP_IMAGE_REFERENCE',
    'DATABASE_URL', 'DATABASE_ROLE', 'REDIS_URL', 'REDIS_NAMESPACE', 'REDIS_SOCKET_TIMEOUT',
    'STORAGE_BACKEND', 'S3_BUCKET', 'S3_PREFIX', 'S3_ENDPOINT_URL', 'S3_REGION',
    'S3_EXPECTED_BUCKET_OWNER', 'AWS_ACCESS_KEY_ID', 'AWS_SECRET_ACCESS_KEY',
    'AWS_SESSION_TOKEN', 'EVIDENCE_CACHE_MODE', 'EVIDENCE_CACHE_TTL_SECONDS',
    'EVIDENCE_CACHE_STALE_IF_ERROR_SECONDS', 'EXTERNAL_HTTP_MAX_ATTEMPTS',
    'EXTERNAL_HTTP_BASE_DELAY_SECONDS', 'EXTERNAL_HTTP_MAX_DELAY_SECONDS',
    'EXTERNAL_HTTP_CIRCUIT_FAILURES', 'EXTERNAL_HTTP_CIRCUIT_RESET_SECONDS',
    'EXTERNAL_HTTP_MAX_CONCURRENCY', 'EXTERNAL_HTTP_REQUESTS_PER_MINUTE',
    'EXTERNAL_HTTP_ACQUIRE_TIMEOUT_SECONDS', 'RESEARCH_PLANNER_API_KEY',
    'RESEARCH_PLANNER_BASE_URL', 'RESEARCH_PLANNER_MODEL', 'NCBI_EMAIL',
    'NCBI_API_KEY',
}
API = COMMON | {
    'PUBLIC_BASE_URL', 'CORS_ORIGINS', 'ALLOW_LEGACY_ARTIFACT_PATHS',
    'METRICS_SCRAPE_TOKEN',
}
WORKER = COMMON | {
    'JOB_LEASE_SECONDS', 'JOB_RESULT_TTL_SECONDS', 'JOB_MAX_ATTEMPTS',
    'WORKER_MAX_CONCURRENCY', 'WORKER_DRAIN_TIMEOUT_SECONDS',
    'WORKER_LIGHT_RESERVED_SLOTS',
    'WORKER_REGISTRY_TTL_SECONDS', 'WORKER_METRICS_HOST', 'WORKER_METRICS_PORT',
    'WORKER_MIN_FREE_DISK_BYTES', 'WORKER_CAPABILITY_ROUTING',
    'WORKER_REQUIRE_EXECUTION_FINGERPRINT', 'STATE_WRITER_BATCH_SIZE',
    'STATE_WRITER_BATCH_WAIT_MS', 'STATE_WRITER_QUEUE_MAXSIZE',
    'STATE_WRITER_ENQUEUE_TIMEOUT_SECONDS', 'STATE_WRITER_MAX_RETRIES',
    'STATE_WRITER_RETRY_BASE_SECONDS', 'STATE_WRITER_PAUSE_THRESHOLD',
    'STATE_WRITER_RESUME_THRESHOLD',
}
DISPATCHER = {
    'APP_ENV', 'APP_RELEASE_TAG', 'APP_GIT_SHA', 'APP_IMAGE_REFERENCE',
    'DATABASE_URL', 'DATABASE_ROLE', 'REDIS_URL', 'REDIS_NAMESPACE',
    'REDIS_SOCKET_TIMEOUT', 'JOB_BACKEND', 'STORAGE_BACKEND', 'S3_BUCKET',
    'WORKER_CAPABILITY_ROUTING',
}
MAINTENANCE = {
    'APP_ENV', 'DATABASE_URL', 'DATABASE_ROLE', 'STORAGE_BACKEND',
    'S3_BUCKET', 'S3_PREFIX', 'S3_ENDPOINT_URL', 'S3_REGION',
    'S3_EXPECTED_BUCKET_OWNER',
}


def _environment_keys(service):
    environment = service.get('environment') or {}
    if isinstance(environment, dict):
        return set(environment)
    return {str(item).split('=', 1)[0] for item in environment}


def _env_example_keys():
    keys = set()
    for line in (ROOT / '.env.example').read_text(encoding='utf-8').splitlines():
        match = re.match(r'^([A-Z][A-Z0-9_]*)=', line.strip())
        if match:
            keys.add(match.group(1))
    return keys


def _helm_files():
    files = []
    for folder in ('helm', 'charts', 'deploy/helm'):
        root = ROOT / folder
        if root.exists():
            files.extend(root.rglob('*.yaml'))
            files.extend(root.rglob('*.yml'))
    return files


def validate():
    compose = yaml.safe_load((ROOT / 'docker-compose.yml').read_text(encoding='utf-8'))
    services = compose.get('services') or {}
    errors = []
    known = PlatformSettings.environment_variables()
    for service_name, required in {
        'api': API,
        'dispatcher': DISPATCHER,
        'worker': WORKER,
        'artifact-maintenance': MAINTENANCE,
    }.items():
        configured = _environment_keys(services.get(service_name) or {})
        missing = sorted(required - configured)
        unknown = sorted((configured & COMMON) - known)
        if missing:
            errors.append(f'{service_name} missing settings: {", ".join(missing)}')
        if unknown:
            errors.append(f'{service_name} has unknown typed settings: {", ".join(unknown)}')
    documented = _env_example_keys()
    missing_documentation = sorted((API | WORKER | MAINTENANCE) - documented)
    if missing_documentation:
        errors.append(
            '.env.example missing settings: ' + ', '.join(missing_documentation)
        )
    helm_files = _helm_files()
    if helm_files:
        helm_text = '\n'.join(path.read_text(encoding='utf-8') for path in helm_files)
        missing_helm = sorted(name for name in COMMON if name not in helm_text)
        if missing_helm:
            errors.append('Helm missing settings: ' + ', '.join(missing_helm))
    return errors, len(helm_files)


def main():
    errors, helm_count = validate()
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print(
        'typed settings consistent with docker-compose.yml; '
        + (f'validated {helm_count} Helm files' if helm_count else 'no Helm chart present')
    )
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
