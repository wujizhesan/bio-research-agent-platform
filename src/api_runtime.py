"""Runtime resource assembly for the FastAPI adapter."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
import os
from pathlib import Path

try:
    from .audit_log import AuditLogger
    from .auth import AuthService, LoginRateLimiter
    from .database import Database
    from .file_storage import LocalFileStorage, S3FileStorage
    from .job_manager import JobManager
    from .job_execution import build_tool_executor_from_env, job_max_workers_from_env
    from .plugin_manager import PluginManager
    from .redis_job_manager import RedisJobManager
except ImportError:
    from audit_log import AuditLogger
    from auth import AuthService, LoginRateLimiter
    from database import Database
    from file_storage import LocalFileStorage, S3FileStorage
    from job_manager import JobManager
    from job_execution import build_tool_executor_from_env, job_max_workers_from_env
    from plugin_manager import PluginManager
    from redis_job_manager import RedisJobManager


@dataclass(frozen=True)
class ApiRuntime:
    jobs: object
    plugins: object
    database: object
    storage: object
    audit: object
    auth: AuthService
    login_rate_limiter: LoginRateLimiter
    job_backend: str
    storage_backend: str
    owns_jobs: bool
    owns_database: bool

    def bind(self, app):
        app.state.job_manager = self.jobs
        app.state.job_backend = self.job_backend
        app.state.plugin_manager = self.plugins
        app.state.database = self.database
        app.state.file_storage = self.storage
        app.state.storage_backend = self.storage_backend
        app.state.audit_log = self.audit
        app.state.auth_service = self.auth

    @asynccontextmanager
    async def lifespan(self, _app):
        await self.database.init_schema()
        try:
            yield
        finally:
            if self.owns_jobs:
                self.jobs.shutdown()
            if self.owns_database:
                await self.database.close()


def _build_jobs(output_root, job_manager):
    if job_manager is not None:
        return job_manager
    if os.environ.get('JOB_BACKEND', 'local').lower() == 'redis':
        return RedisJobManager(
            redis_url=os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'),
            namespace=os.environ.get('REDIS_NAMESPACE', 'bioagent'),
        )
    return JobManager(
        max_workers=job_max_workers_from_env(),
        store_path=output_root / 'jobs.sqlite3',
        tool_executor=build_tool_executor_from_env(),
    )


def _build_storage(project_root, output_root, file_storage):
    configured_backend = os.environ.get('STORAGE_BACKEND', 'local').strip().lower()
    if file_storage is not None:
        return file_storage, getattr(file_storage, 'backend', configured_backend)
    configured_root = os.environ.get('UPLOAD_ROOT')
    upload_root = Path(configured_root) if configured_root else output_root / 'uploads'
    if not upload_root.is_absolute():
        upload_root = project_root / upload_root
    max_bytes = int(os.environ.get('UPLOAD_MAX_BYTES', str(50 * 1024 * 1024)))
    total_quota_bytes = int(os.environ.get(
        'UPLOAD_TOTAL_QUOTA_BYTES', str(10 * 1024 * 1024 * 1024)
    ))
    max_decompressed_bytes = int(os.environ.get(
        'UPLOAD_MAX_DECOMPRESSED_BYTES', str(200 * 1024 * 1024)
    ))
    max_compression_ratio = float(os.environ.get(
        'UPLOAD_MAX_COMPRESSION_RATIO', '100'
    ))
    storage_limits = {
        'max_bytes': max_bytes,
        'total_quota_bytes': total_quota_bytes,
        'max_decompressed_bytes': max_decompressed_bytes,
        'max_compression_ratio': max_compression_ratio,
    }
    if configured_backend == 'local':
        storage = LocalFileStorage(upload_root, **storage_limits)
    elif configured_backend == 's3':
        storage = S3FileStorage(
            upload_root,
            bucket=os.environ.get('S3_BUCKET', ''),
            prefix=os.environ.get('S3_PREFIX', 'bio-agent'),
            endpoint_url=os.environ.get('S3_ENDPOINT_URL') or None,
            region_name=os.environ.get('S3_REGION') or None,
            access_key_id=os.environ.get('AWS_ACCESS_KEY_ID') or None,
            secret_access_key=os.environ.get('AWS_SECRET_ACCESS_KEY') or None,
            **storage_limits,
        )
    else:
        raise ValueError(f'unsupported STORAGE_BACKEND: {configured_backend}')
    return storage, getattr(storage, 'backend', configured_backend)


def build_api_runtime(
    project_root,
    output_root,
    *,
    job_manager=None,
    plugin_manager=None,
    database=None,
    file_storage=None,
    audit_log=None,
):
    project_root = Path(project_root)
    output_root = Path(output_root)
    storage, storage_backend = _build_storage(
        project_root,
        output_root,
        file_storage,
    )
    plugins = plugin_manager or PluginManager(
        state_path=output_root / 'plugin_state.json'
    )
    runtime_database = database or Database()
    audit = audit_log or AuditLogger(output_root / 'audit.jsonl')
    auth = AuthService.from_env()
    login_rate_limiter = LoginRateLimiter.from_env()
    jobs = _build_jobs(output_root, job_manager)
    return ApiRuntime(
        jobs=jobs,
        plugins=plugins,
        database=runtime_database,
        storage=storage,
        audit=audit,
        auth=auth,
        login_rate_limiter=login_rate_limiter,
        job_backend=getattr(jobs, 'backend', 'local'),
        storage_backend=storage_backend,
        owns_jobs=job_manager is None,
        owns_database=database is None,
    )
