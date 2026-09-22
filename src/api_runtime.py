"""Runtime resource assembly for the FastAPI adapter."""

from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

try:
    from .audit_log import AuditLogger
    from .auth import AuthService, LoginRateLimiter
    from .database import Database
    from .file_security import build_file_security_pipeline_from_env
    from .file_storage import LocalFileStorage, S3FileStorage
    from .job_manager import JobManager
    from .job_execution import build_tool_executor_from_env, job_max_workers_from_env
    from .plugin_manager import PluginManager
    from .redis_job_manager import RedisJobManager
    from .settings import PlatformSettings
except ImportError:
    from audit_log import AuditLogger
    from auth import AuthService, LoginRateLimiter
    from database import Database
    from file_security import build_file_security_pipeline_from_env
    from file_storage import LocalFileStorage, S3FileStorage
    from job_manager import JobManager
    from job_execution import build_tool_executor_from_env, job_max_workers_from_env
    from plugin_manager import PluginManager
    from redis_job_manager import RedisJobManager
    from settings import PlatformSettings


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
    settings: object = None

    def bind(self, app):
        app.state.job_manager = self.jobs
        app.state.job_backend = self.job_backend
        app.state.plugin_manager = self.plugins
        app.state.database = self.database
        app.state.file_storage = self.storage
        app.state.storage_backend = self.storage_backend
        app.state.audit_log = self.audit
        app.state.auth_service = self.auth
        app.state.configuration = (
            self.settings.public_snapshot() if self.settings else None
        )

    @asynccontextmanager
    async def lifespan(self, _app):
        await self.database.init_schema()
        try:
            yield
        finally:
            if self.owns_jobs:
                self.jobs.shutdown()
            try:
                await self.login_rate_limiter.close()
            finally:
                if self.owns_database:
                    await self.database.close()


def _build_jobs(output_root, job_manager, settings=None):
    if job_manager is not None:
        return job_manager
    settings = settings or PlatformSettings.from_env()
    if settings.job_backend == 'redis':
        return RedisJobManager(
            redis_url=settings.redis_url,
            namespace=settings.redis_namespace,
            settings=settings,
        )
    return JobManager(
        max_workers=job_max_workers_from_env(),
        store_path=output_root / 'jobs.sqlite3',
        tool_executor=build_tool_executor_from_env(),
    )


def _build_storage(project_root, output_root, file_storage, settings=None):
    settings = settings or PlatformSettings.from_env()
    configured_backend = settings.storage_backend
    if file_storage is not None:
        return file_storage, getattr(file_storage, 'backend', configured_backend)
    if settings.app_env == 'production':
        if configured_backend != 's3':
            raise ValueError('production requires STORAGE_BACKEND=s3')
    configured_root = settings.upload_root
    upload_root = Path(configured_root) if configured_root else output_root / 'uploads'
    if not upload_root.is_absolute():
        upload_root = project_root / upload_root
    storage_limits = {
        'max_bytes': settings.upload_max_bytes,
        'total_quota_bytes': settings.upload_total_quota_bytes,
        'max_decompressed_bytes': settings.upload_max_decompressed_bytes,
        'max_compression_ratio': settings.upload_max_compression_ratio,
        'security_pipeline': build_file_security_pipeline_from_env(),
    }
    if configured_backend == 'local':
        storage = LocalFileStorage(upload_root, **storage_limits)
    elif configured_backend == 's3':
        storage = S3FileStorage(
            upload_root,
            bucket=settings.s3_bucket,
            prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.s3_region or None,
            expected_bucket_owner=settings.s3_expected_bucket_owner or None,
            access_key_id=settings.aws_access_key_id or None,
            secret_access_key=settings.aws_secret_access_key or None,
            session_token=settings.aws_session_token or None,
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
    settings=None,
):
    project_root = Path(project_root)
    output_root = Path(output_root)
    settings = settings or PlatformSettings.from_env()
    storage, storage_backend = _build_storage(
        project_root,
        output_root,
        file_storage,
        settings,
    )
    settings.validate('api')
    plugins = plugin_manager or PluginManager(
        state_path=output_root / 'plugin_state.json'
    )
    runtime_database = database or Database(settings.database_url)
    audit = audit_log or AuditLogger(
        output_root / 'audit.jsonl',
        database=runtime_database,
    )
    auth = AuthService.from_env()
    login_rate_limiter = LoginRateLimiter.from_env(
        redis_url=settings.redis_url if settings.job_backend == 'redis' else None,
        namespace=settings.redis_namespace,
        production=settings.app_env in {'production', 'prod'},
    )
    jobs = _build_jobs(output_root, job_manager, settings)
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
        settings=settings,
    )
