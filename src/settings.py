"""Typed runtime configuration shared by API and workers."""

from dataclasses import dataclass
import hashlib
import json
import os
from ipaddress import ip_network
from urllib.parse import urlsplit, urlunsplit
import warnings


class SettingsError(ValueError):
    pass


def _text(source, name, default=''):
    value = source.get(name, default)
    return str(value).strip()


def _secret_text(source, name, default=''):
    direct = _text(source, name)
    file_name = _text(source, f'{name}_FILE')
    if direct and file_name:
        raise SettingsError(f'configure only one of {name} or {name}_FILE')
    if not file_name:
        return direct or str(default).strip()
    try:
        with open(os.path.abspath(file_name), encoding='utf-8') as handle:
            value = handle.read(65537)
    except OSError as exc:
        raise SettingsError(f'unable to read {name}_FILE') from exc
    if len(value) > 65536:
        raise SettingsError(f'{name}_FILE exceeds 65536 bytes')
    return value.strip() or str(default).strip()


def _integer(source, name, default, minimum=None):
    raw = _text(source, name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f'{name} must be an integer') from exc
    return max(value, minimum) if minimum is not None else value


def _number(source, name, default, minimum=None, maximum=None):
    raw = _text(source, name, default)
    try:
        value = float(raw)
    except (TypeError, ValueError) as exc:
        raise SettingsError(f'{name} must be a number') from exc
    if minimum is not None:
        value = max(value, minimum)
    if maximum is not None:
        value = min(value, maximum)
    return value


def _boolean(source, name, default=False):
    raw = source.get(name)
    if raw is None:
        return bool(default)
    normalized = str(raw).strip().lower()
    if normalized in {'1', 'true', 'yes', 'on'}:
        return True
    if normalized in {'0', 'false', 'no', 'off', ''}:
        return False
    raise SettingsError(f'{name} must be a boolean')


def _safe_url(value):
    if not value:
        return ''
    try:
        parsed = urlsplit(value)
    except ValueError:
        return '<configured>'
    if not parsed.scheme:
        return '<configured>'
    hostname = parsed.hostname or ''
    port = f':{parsed.port}' if parsed.port else ''
    return urlunsplit((parsed.scheme, hostname + port, parsed.path, '', ''))


@dataclass(frozen=True)
class PlatformSettings:
    app_env: str
    public_base_url: str
    cors_origins: tuple[str, ...]
    trusted_proxy_cidrs: tuple[str, ...]
    release_tag: str
    git_sha: str
    image_reference: str
    job_backend: str
    redis_url: str
    redis_namespace: str
    redis_socket_timeout: float
    database_url: str
    database_role: str
    storage_backend: str
    upload_root: str
    upload_max_bytes: int
    upload_total_quota_bytes: int
    upload_max_decompressed_bytes: int
    upload_max_compression_ratio: float
    api_request_body_max_bytes: int
    api_upload_body_max_bytes: int
    auth_request_body_max_bytes: int
    allow_legacy_artifact_paths: bool
    s3_bucket: str
    s3_prefix: str
    s3_endpoint_url: str
    s3_region: str
    s3_expected_bucket_owner: str
    aws_access_key_id: str
    aws_secret_access_key: str
    aws_session_token: str
    readiness_timeout_seconds: float
    job_lease_seconds: int
    job_result_ttl_seconds: int
    job_max_attempts: int
    worker_max_concurrency: int
    worker_light_reserved_slots: int
    worker_drain_timeout_seconds: float
    worker_registry_ttl_seconds: int
    worker_metrics_host: str
    worker_metrics_port: int
    worker_min_free_disk_bytes: int
    worker_capability_routing: bool
    worker_require_execution_fingerprint: bool
    state_writer_batch_size: int
    state_writer_batch_wait_ms: float
    state_writer_queue_maxsize: int
    state_writer_enqueue_timeout_seconds: float
    state_writer_max_retries: int
    state_writer_retry_base_seconds: float
    state_writer_pause_threshold: float
    state_writer_resume_threshold: float
    evidence_cache_mode: str
    evidence_cache_ttl_seconds: int
    evidence_cache_stale_if_error_seconds: int
    external_http_max_attempts: int
    external_http_base_delay_seconds: float
    external_http_max_delay_seconds: float
    external_http_circuit_failures: int
    external_http_circuit_reset_seconds: float
    external_http_max_concurrency: int
    external_http_requests_per_minute: int
    external_http_acquire_timeout_seconds: float
    research_planner_base_url: str
    research_planner_model: str
    research_planner_api_key: str
    ncbi_email: str
    ncbi_api_key: str
    metrics_scrape_token: str
    legacy_api_token: str

    SCHEMA_VERSION = 1

    @classmethod
    def from_env(cls, source=None):
        source = os.environ if source is None else source
        app_env = _text(source, 'APP_ENV', 'development').lower()
        if app_env in {'production', 'prod'}:
            direct_secrets = (
                'DATABASE_URL', 'REDIS_URL', 'AWS_ACCESS_KEY_ID',
                'AWS_SECRET_ACCESS_KEY', 'AWS_SESSION_TOKEN',
                'RESEARCH_PLANNER_API_KEY', 'OPENAI_API_KEY', 'CADD_API_KEY',
                'NCBI_API_KEY', 'METRICS_SCRAPE_TOKEN',
            )
            configured = [name for name in direct_secrets if _text(source, name)]
            if configured:
                raise SettingsError(
                    'production secrets must use *_FILE: ' + ', '.join(configured)
                )
        cache_mode = _text(source, 'EVIDENCE_CACHE_MODE', 'ttl').lower()
        if cache_mode not in {'ttl', 'refresh', 'frozen'}:
            raise SettingsError('EVIDENCE_CACHE_MODE must be ttl, refresh or frozen')
        values = cls(
            app_env=app_env,
            public_base_url=_text(source, 'PUBLIC_BASE_URL'),
            cors_origins=tuple(
                item.strip()
                for item in _text(
                    source,
                    'CORS_ORIGINS',
                    'http://localhost:5173,http://127.0.0.1:5173',
                ).split(',')
                if item.strip()
            ),
            trusted_proxy_cidrs=tuple(
                item.strip()
                for item in _text(source, 'TRUSTED_PROXY_CIDRS').split(',')
                if item.strip()
            ),
            release_tag=_text(source, 'APP_RELEASE_TAG', 'development'),
            git_sha=_text(source, 'APP_GIT_SHA', 'unknown'),
            image_reference=_text(source, 'APP_IMAGE_REFERENCE', 'unknown'),
            job_backend=_text(source, 'JOB_BACKEND', 'local').lower(),
            redis_url=_secret_text(source, 'REDIS_URL', 'redis://127.0.0.1:6379/0'),
            redis_namespace=_text(source, 'REDIS_NAMESPACE', 'bioagent'),
            redis_socket_timeout=_number(source, 'REDIS_SOCKET_TIMEOUT', 15, 6),
            database_url=_secret_text(source, 'DATABASE_URL', 'sqlite+aiosqlite:///./output/bio-agent.db'),
            database_role=_text(source, 'DATABASE_ROLE', 'internal').lower(),
            storage_backend=_text(source, 'STORAGE_BACKEND', 'local').lower(),
            upload_root=_text(source, 'UPLOAD_ROOT', 'output/uploads'),
            upload_max_bytes=_integer(source, 'UPLOAD_MAX_BYTES', 50 * 1024 * 1024, 1),
            upload_total_quota_bytes=_integer(source, 'UPLOAD_TOTAL_QUOTA_BYTES', 10 * 1024 ** 3, 1),
            upload_max_decompressed_bytes=_integer(source, 'UPLOAD_MAX_DECOMPRESSED_BYTES', 200 * 1024 * 1024, 1),
            upload_max_compression_ratio=_number(source, 'UPLOAD_MAX_COMPRESSION_RATIO', 100, 1),
            api_request_body_max_bytes=_integer(
                source, 'API_REQUEST_BODY_MAX_BYTES', 2 * 1024 * 1024, 1024
            ),
            api_upload_body_max_bytes=_integer(
                source, 'API_UPLOAD_BODY_MAX_BYTES', 55 * 1024 * 1024, 1024
            ),
            auth_request_body_max_bytes=_integer(
                source, 'AUTH_REQUEST_BODY_MAX_BYTES', 16 * 1024, 1024
            ),
            allow_legacy_artifact_paths=_boolean(
                source,
                'ALLOW_LEGACY_ARTIFACT_PATHS',
                app_env not in {'production', 'prod'},
            ),
            s3_bucket=_text(source, 'S3_BUCKET'),
            s3_prefix=_text(source, 'S3_PREFIX', 'bio-agent'),
            s3_endpoint_url=_text(source, 'S3_ENDPOINT_URL'),
            s3_region=_text(source, 'S3_REGION'),
            s3_expected_bucket_owner=_text(source, 'S3_EXPECTED_BUCKET_OWNER'),
            aws_access_key_id=_secret_text(source, 'AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=_secret_text(source, 'AWS_SECRET_ACCESS_KEY'),
            aws_session_token=_secret_text(source, 'AWS_SESSION_TOKEN'),
            readiness_timeout_seconds=_number(source, 'READINESS_TIMEOUT_SECONDS', 2, .1, 30),
            job_lease_seconds=_integer(source, 'JOB_LEASE_SECONDS', 300, 1),
            job_result_ttl_seconds=_integer(source, 'JOB_RESULT_TTL_SECONDS', 86400, 60),
            job_max_attempts=_integer(source, 'JOB_MAX_ATTEMPTS', 3, 1),
            worker_max_concurrency=_integer(source, 'WORKER_MAX_CONCURRENCY', 2, 1),
            worker_light_reserved_slots=_integer(source, 'WORKER_LIGHT_RESERVED_SLOTS', 0, 0),
            worker_drain_timeout_seconds=_number(source, 'WORKER_DRAIN_TIMEOUT_SECONDS', 120, 0),
            worker_registry_ttl_seconds=_integer(source, 'WORKER_REGISTRY_TTL_SECONDS', 30, 5),
            worker_metrics_host=_text(source, 'WORKER_METRICS_HOST', '0.0.0.0'),
            worker_metrics_port=_integer(source, 'WORKER_METRICS_PORT', 9000, 0),
            worker_min_free_disk_bytes=_integer(source, 'WORKER_MIN_FREE_DISK_BYTES', 1024 ** 3, 0),
            worker_capability_routing=_boolean(source, 'WORKER_CAPABILITY_ROUTING', False),
            worker_require_execution_fingerprint=_boolean(source, 'WORKER_REQUIRE_EXECUTION_FINGERPRINT', False),
            state_writer_batch_size=_integer(source, 'STATE_WRITER_BATCH_SIZE', 50, 1),
            state_writer_batch_wait_ms=_number(source, 'STATE_WRITER_BATCH_WAIT_MS', 20, 0),
            state_writer_queue_maxsize=_integer(source, 'STATE_WRITER_QUEUE_MAXSIZE', 1000, 1),
            state_writer_enqueue_timeout_seconds=_number(source, 'STATE_WRITER_ENQUEUE_TIMEOUT_SECONDS', 5, .1),
            state_writer_max_retries=_integer(source, 'STATE_WRITER_MAX_RETRIES', 8, 1),
            state_writer_retry_base_seconds=_number(source, 'STATE_WRITER_RETRY_BASE_SECONDS', .25, .01),
            state_writer_pause_threshold=_number(source, 'STATE_WRITER_PAUSE_THRESHOLD', .8, .1, 1),
            state_writer_resume_threshold=_number(source, 'STATE_WRITER_RESUME_THRESHOLD', .5, 0, 1),
            evidence_cache_mode=cache_mode,
            evidence_cache_ttl_seconds=_integer(source, 'EVIDENCE_CACHE_TTL_SECONDS', 86400, 60),
            evidence_cache_stale_if_error_seconds=_integer(source, 'EVIDENCE_CACHE_STALE_IF_ERROR_SECONDS', 604800, 0),
            external_http_max_attempts=_integer(source, 'EXTERNAL_HTTP_MAX_ATTEMPTS', 3, 1),
            external_http_base_delay_seconds=_number(source, 'EXTERNAL_HTTP_BASE_DELAY_SECONDS', .25, 0),
            external_http_max_delay_seconds=_number(source, 'EXTERNAL_HTTP_MAX_DELAY_SECONDS', 10, 0),
            external_http_circuit_failures=_integer(source, 'EXTERNAL_HTTP_CIRCUIT_FAILURES', 5, 1),
            external_http_circuit_reset_seconds=_number(source, 'EXTERNAL_HTTP_CIRCUIT_RESET_SECONDS', 60, .1),
            external_http_max_concurrency=_integer(source, 'EXTERNAL_HTTP_MAX_CONCURRENCY', 4, 1),
            external_http_requests_per_minute=_integer(source, 'EXTERNAL_HTTP_REQUESTS_PER_MINUTE', 60, 1),
            external_http_acquire_timeout_seconds=_number(source, 'EXTERNAL_HTTP_ACQUIRE_TIMEOUT_SECONDS', 30, .1),
            research_planner_base_url=_text(source, 'RESEARCH_PLANNER_BASE_URL') or _text(source, 'OPENAI_BASE_URL'),
            research_planner_model=_text(source, 'RESEARCH_PLANNER_MODEL'),
            research_planner_api_key=(
                _secret_text(source, 'RESEARCH_PLANNER_API_KEY')
                or _secret_text(source, 'CADD_API_KEY')
                or _secret_text(source, 'OPENAI_API_KEY')
            ),
            ncbi_email=_text(source, 'NCBI_EMAIL'),
            ncbi_api_key=_secret_text(source, 'NCBI_API_KEY'),
            metrics_scrape_token=_secret_text(source, 'METRICS_SCRAPE_TOKEN'),
            legacy_api_token=_text(source, 'CADD_API_TOKEN'),
        )
        values._warn_deprecated()
        return values

    def _warn_deprecated(self):
        if self.legacy_api_token:
            warnings.warn(
                'CADD_API_TOKEN is deprecated; use JWT credentials or an external secret',
                FutureWarning,
                stacklevel=3,
            )

    def validate(self, component='runtime'):
        errors = []
        if self.job_backend not in {'local', 'redis'}:
            errors.append('JOB_BACKEND must be local or redis')
        if self.storage_backend not in {'local', 's3'}:
            errors.append('STORAGE_BACKEND must be local or s3')
        if self.worker_light_reserved_slots and not self.worker_capability_routing:
            errors.append('WORKER_LIGHT_RESERVED_SLOTS requires WORKER_CAPABILITY_ROUTING')
        if self.state_writer_resume_threshold >= self.state_writer_pause_threshold:
            errors.append('STATE_WRITER_RESUME_THRESHOLD must be lower than STATE_WRITER_PAUSE_THRESHOLD')
        if self.database_role not in {
            'api', 'dispatcher', 'worker', 'maintenance', 'migration', 'internal',
        }:
            errors.append(
                'DATABASE_ROLE must be api, dispatcher, worker, maintenance, '
                'migration or internal'
            )
        if self.app_env in {'production', 'prod'}:
            try:
                public_url = urlsplit(self.public_base_url)
                public_port = public_url.port
            except ValueError:
                public_url = None
                public_port = None
            if component in {'api', 'runtime'} and (
                public_url is None
                or public_url.scheme != 'https'
                or not public_url.hostname
                or public_url.username is not None
                or public_url.password is not None
                or public_url.query
                or public_url.fragment
                or public_url.path not in {'', '/'}
                or public_port is not None and not 1 <= public_port <= 65535
            ):
                errors.append(
                    'production requires PUBLIC_BASE_URL as an HTTPS origin'
                )
            if component in {'api', 'runtime'}:
                if not self.trusted_proxy_cidrs:
                    errors.append('production requires TRUSTED_PROXY_CIDRS')
                else:
                    try:
                        for value in self.trusted_proxy_cidrs:
                            ip_network(value, strict=False)
                    except ValueError:
                        errors.append('TRUSTED_PROXY_CIDRS contains an invalid network')
                for origin in self.cors_origins:
                    try:
                        cors_url = urlsplit(origin)
                        cors_port = cors_url.port
                    except ValueError:
                        cors_url = None
                        cors_port = None
                    if (
                        cors_url is None
                        or cors_url.scheme != 'https'
                        or not cors_url.hostname
                        or cors_url.username is not None
                        or cors_url.password is not None
                        or cors_url.path not in {'', '/'}
                        or cors_url.query
                        or cors_url.fragment
                        or cors_port is not None and not 1 <= cors_port <= 65535
                    ):
                        errors.append(
                            'production CORS_ORIGINS must contain HTTPS origins only'
                        )
                        break
            if self.legacy_api_token:
                errors.append('production forbids CADD_API_TOKEN')
            if self.allow_legacy_artifact_paths:
                errors.append('production forbids ALLOW_LEGACY_ARTIFACT_PATHS')
            if (
                component in {'api', 'runtime', 'worker', 'dispatcher'}
                and self.job_backend != 'redis'
            ):
                errors.append('production requires JOB_BACKEND=redis')
            if self.storage_backend != 's3':
                errors.append('production requires STORAGE_BACKEND=s3')
            if not self.s3_bucket:
                errors.append('production requires S3_BUCKET')
            if component in {'api', 'runtime', 'worker', 'dispatcher'}:
                if not self.redis_url:
                    errors.append('production Redis execution requires REDIS_URL')
                if not self.database_url:
                    errors.append('production Redis execution requires DATABASE_URL')
            elif component in {'maintenance', 'migration'} and not self.database_url:
                errors.append(f'production {component} requires DATABASE_URL')
            expected_database_role = (
                component
                if component in {
                    'api', 'worker', 'dispatcher', 'maintenance', 'migration',
                }
                else 'api'
            )
            if self.database_role != expected_database_role:
                errors.append(
                    f'production {component} requires DATABASE_ROLE={expected_database_role}'
                )
        if errors:
            raise SettingsError('; '.join(errors))
        return self

    def sanitized_configuration(self):
        return {
            'schema_version': self.SCHEMA_VERSION,
            'environment': self.app_env,
            'public_base_url': self.public_base_url,
            'cors_origins': list(self.cors_origins),
            'trusted_proxy_count': len(self.trusted_proxy_cidrs),
            'release_tag': self.release_tag,
            'git_sha': self.git_sha,
            'image_reference': self.image_reference,
            'job_backend': self.job_backend,
            'worker_light_reserved_slots': self.worker_light_reserved_slots,
            'redis_url': _safe_url(self.redis_url),
            'database_url': _safe_url(self.database_url),
            'database_role': self.database_role,
            'storage_backend': self.storage_backend,
            'legacy_artifact_paths': self.allow_legacy_artifact_paths,
            's3_bucket': self.s3_bucket,
            's3_region': self.s3_region,
            'evidence_cache_mode': self.evidence_cache_mode,
            'external_http_policy': {
                'max_attempts': self.external_http_max_attempts,
                'circuit_failures': self.external_http_circuit_failures,
                'max_concurrency': self.external_http_max_concurrency,
                'requests_per_minute': self.external_http_requests_per_minute,
            },
            'state_writer_backpressure': {
                'pause_threshold': self.state_writer_pause_threshold,
                'resume_threshold': self.state_writer_resume_threshold,
            },
            'secrets_configured': {
                'aws': bool(self.aws_access_key_id and self.aws_secret_access_key),
                'planner': bool(self.research_planner_api_key),
                'ncbi': bool(self.ncbi_api_key),
                'metrics_scrape': bool(self.metrics_scrape_token),
            },
        }

    @property
    def fingerprint(self):
        encoded = json.dumps(
            self.sanitized_configuration(),
            sort_keys=True,
            separators=(',', ':'),
        ).encode('utf-8')
        return hashlib.sha256(encoded).hexdigest()

    def public_snapshot(self):
        return {
            'schema_version': self.SCHEMA_VERSION,
            'fingerprint': self.fingerprint,
            'environment': self.app_env,
        }

    @classmethod
    def environment_variables(cls):
        return {
            'APP_ENV', 'APP_RELEASE_TAG', 'APP_GIT_SHA', 'APP_IMAGE_REFERENCE',
            'PUBLIC_BASE_URL', 'CORS_ORIGINS', 'TRUSTED_PROXY_CIDRS',
            'JOB_BACKEND', 'REDIS_URL', 'REDIS_URL_FILE', 'REDIS_NAMESPACE', 'REDIS_SOCKET_TIMEOUT',
            'DATABASE_URL', 'DATABASE_URL_FILE', 'DATABASE_ROLE', 'STORAGE_BACKEND', 'UPLOAD_ROOT', 'UPLOAD_MAX_BYTES',
            'UPLOAD_TOTAL_QUOTA_BYTES', 'UPLOAD_MAX_DECOMPRESSED_BYTES',
            'UPLOAD_MAX_COMPRESSION_RATIO', 'API_REQUEST_BODY_MAX_BYTES',
            'API_UPLOAD_BODY_MAX_BYTES', 'AUTH_REQUEST_BODY_MAX_BYTES',
            'ALLOW_LEGACY_ARTIFACT_PATHS',
            'S3_BUCKET', 'S3_PREFIX',
            'S3_ENDPOINT_URL', 'S3_REGION', 'S3_EXPECTED_BUCKET_OWNER',
            'AWS_ACCESS_KEY_ID', 'AWS_ACCESS_KEY_ID_FILE',
            'AWS_SECRET_ACCESS_KEY', 'AWS_SECRET_ACCESS_KEY_FILE',
            'AWS_SESSION_TOKEN', 'AWS_SESSION_TOKEN_FILE',
            'READINESS_TIMEOUT_SECONDS', 'JOB_LEASE_SECONDS', 'JOB_RESULT_TTL_SECONDS',
            'JOB_MAX_ATTEMPTS', 'WORKER_MAX_CONCURRENCY', 'WORKER_DRAIN_TIMEOUT_SECONDS',
            'WORKER_LIGHT_RESERVED_SLOTS',
            'WORKER_REGISTRY_TTL_SECONDS', 'WORKER_METRICS_HOST', 'WORKER_METRICS_PORT',
            'WORKER_MIN_FREE_DISK_BYTES', 'WORKER_CAPABILITY_ROUTING',
            'WORKER_REQUIRE_EXECUTION_FINGERPRINT', 'STATE_WRITER_BATCH_SIZE',
            'STATE_WRITER_BATCH_WAIT_MS', 'STATE_WRITER_QUEUE_MAXSIZE',
            'STATE_WRITER_ENQUEUE_TIMEOUT_SECONDS', 'STATE_WRITER_MAX_RETRIES',
            'STATE_WRITER_RETRY_BASE_SECONDS', 'EVIDENCE_CACHE_MODE',
            'STATE_WRITER_PAUSE_THRESHOLD', 'STATE_WRITER_RESUME_THRESHOLD',
            'EVIDENCE_CACHE_TTL_SECONDS', 'EVIDENCE_CACHE_STALE_IF_ERROR_SECONDS',
            'EXTERNAL_HTTP_MAX_ATTEMPTS', 'EXTERNAL_HTTP_BASE_DELAY_SECONDS',
            'EXTERNAL_HTTP_MAX_DELAY_SECONDS', 'EXTERNAL_HTTP_CIRCUIT_FAILURES',
            'EXTERNAL_HTTP_CIRCUIT_RESET_SECONDS', 'EXTERNAL_HTTP_MAX_CONCURRENCY',
            'EXTERNAL_HTTP_REQUESTS_PER_MINUTE', 'EXTERNAL_HTTP_ACQUIRE_TIMEOUT_SECONDS',
            'RESEARCH_PLANNER_BASE_URL', 'RESEARCH_PLANNER_MODEL',
            'RESEARCH_PLANNER_API_KEY', 'OPENAI_BASE_URL', 'OPENAI_API_KEY',
            'CADD_API_KEY', 'NCBI_EMAIL', 'NCBI_API_KEY', 'CADD_API_TOKEN',
            'METRICS_SCRAPE_TOKEN', 'METRICS_SCRAPE_TOKEN_FILE',
            'RLS_CONTEXT_SIGNING_KEY', 'RLS_CONTEXT_SIGNING_KEY_FILE',
            'RLS_CONTEXT_KEY_ID', 'RLS_CONTEXT_ROTATION_GRACE_SECONDS',
            'RLS_CONTEXT_SIGNING_KEY_SHA256', 'CADD_JWT_SECRET_SHA256',
        }

    @property
    def trusted_hosts(self):
        hosts = {'localhost', '127.0.0.1', 'api'}
        if self.public_base_url:
            try:
                hostname = urlsplit(self.public_base_url).hostname
            except ValueError:
                hostname = None
            if hostname:
                hosts.add(hostname)
        return tuple(sorted(hosts))
