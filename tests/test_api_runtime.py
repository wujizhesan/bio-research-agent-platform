import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.api_runtime import ApiRuntime, _build_jobs, build_api_runtime


class FakeJobs:
    backend = 'injected'

    def __init__(self):
        self.stopped = False

    def shutdown(self):
        self.stopped = True


class FakeDatabase:
    def __init__(self):
        self.initialized = False
        self.closed = False

    async def init_schema(self):
        self.initialized = True

    async def close(self):
        self.closed = True


class FakeStorage:
    backend = 'injected-storage'


class FakeRateLimiter:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


class ApiRuntimeTests(unittest.IsolatedAsyncioTestCase):
    def test_injected_resources_are_preserved_and_not_owned(self):
        with tempfile.TemporaryDirectory(prefix='api_runtime_') as raw:
            jobs = FakeJobs()
            database = FakeDatabase()
            plugins = object()
            storage = FakeStorage()
            audit = object()
            runtime = build_api_runtime(
                Path(raw),
                Path(raw) / 'output',
                job_manager=jobs,
                plugin_manager=plugins,
                database=database,
                file_storage=storage,
                audit_log=audit,
            )

        self.assertIs(runtime.jobs, jobs)
        self.assertIs(runtime.database, database)
        self.assertIs(runtime.plugins, plugins)
        self.assertIs(runtime.storage, storage)
        self.assertIs(runtime.audit, audit)
        self.assertEqual(runtime.job_backend, 'injected')
        self.assertEqual(runtime.storage_backend, 'injected-storage')
        self.assertFalse(runtime.owns_jobs)
        self.assertFalse(runtime.owns_database)

    def test_unknown_storage_backend_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='api_runtime_') as raw:
            with patch.dict('os.environ', {'STORAGE_BACKEND': 'unknown'}, clear=False):
                with self.assertRaisesRegex(ValueError, 'unsupported STORAGE_BACKEND'):
                    build_api_runtime(
                        Path(raw),
                        Path(raw) / 'output',
                        job_manager=FakeJobs(),
                        plugin_manager=object(),
                        database=FakeDatabase(),
                        audit_log=object(),
                    )

    def test_production_rejects_local_storage(self):
        with tempfile.TemporaryDirectory(prefix='api_runtime_') as raw:
            values = {'APP_ENV': 'production', 'STORAGE_BACKEND': 'local'}
            with patch.dict('os.environ', values, clear=False):
                with self.assertRaisesRegex(ValueError, 'production requires'):
                    build_api_runtime(
                        Path(raw),
                        Path(raw) / 'output',
                        job_manager=FakeJobs(),
                        plugin_manager=object(),
                        database=FakeDatabase(),
                        audit_log=object(),
                    )

    def test_local_backend_uses_configured_process_isolation(self):
        with tempfile.TemporaryDirectory(prefix='api_runtime_') as raw:
            values = {
                'JOB_BACKEND': 'local',
                'JOB_EXECUTION_MODE': 'process',
                'JOB_MAX_WORKERS': '1',
                'JOB_TIMEOUT_SECONDS': '75',
                'JOB_MEMORY_LIMIT_MB': '1024',
            }
            with patch.dict('os.environ', values, clear=False):
                jobs = _build_jobs(Path(raw), None)
            try:
                self.assertEqual(jobs._tool_executor.mode, 'process')
                self.assertEqual(jobs._tool_executor.limits.timeout_seconds, 75)
                self.assertEqual(jobs._tool_executor.limits.memory_limit_mb, 1024)
                self.assertEqual(jobs._executor._max_workers, 1)
            finally:
                jobs.shutdown()

    def test_required_file_security_builds_clamav_and_cdr_pipeline(self):
        with tempfile.TemporaryDirectory(prefix='api_runtime_') as raw:
            values = {
                'STORAGE_BACKEND': 'local',
                'UPLOAD_ROOT': str(Path(raw) / 'uploads'),
                'FILE_SECURITY_MODE': 'required',
                'FILE_CDR_MODE': 'normalize',
                'CLAMAV_HOST': 'clamav',
                'CLAMAV_PORT': '3310',
            }
            with patch.dict('os.environ', values, clear=False):
                runtime = build_api_runtime(
                    Path(raw),
                    Path(raw) / 'output',
                    job_manager=FakeJobs(),
                    plugin_manager=object(),
                    database=FakeDatabase(),
                    audit_log=object(),
                )
        pipeline = runtime.storage.security_pipeline
        self.assertTrue(pipeline.required)
        self.assertEqual(pipeline.clamav.host, 'clamav')
        self.assertIsNotNone(pipeline.cdr)

    async def test_runtime_closes_its_listener_for_an_injected_postgres_database(self):
        database = FakeDatabase()
        database.url = 'postgresql+asyncpg://api@db/research'
        listener = Mock(close=AsyncMock())
        runtime = ApiRuntime(
            jobs=FakeJobs(), plugins=object(), database=database,
            storage=FakeStorage(), audit=object(), auth=object(),
            login_rate_limiter=FakeRateLimiter(), job_backend='redis',
            storage_backend='local', owns_jobs=False, owns_database=False,
        )
        app = SimpleNamespace(state=SimpleNamespace())
        with patch('src.api_runtime.JobEventListener', return_value=listener) as factory:
            async with runtime.lifespan(app):
                self.assertIs(app.state.job_event_listener, listener)
                listener.start.assert_called_once()
        factory.assert_called_once_with(database.url)
        listener.close.assert_awaited_once()
        self.assertIsNone(app.state.job_event_listener)
        self.assertFalse(database.closed)

    async def test_owned_runtime_closes_resources_after_lifespan(self):
        jobs = FakeJobs()
        database = FakeDatabase()
        limiter = FakeRateLimiter()
        runtime = ApiRuntime(
            jobs=jobs,
            plugins=object(),
            database=database,
            storage=FakeStorage(),
            audit=object(),
            auth=object(),
            login_rate_limiter=limiter,
            job_backend='test',
            storage_backend='test',
            owns_jobs=True,
            owns_database=True,
        )

        async with runtime.lifespan(None):
            self.assertTrue(database.initialized)
            self.assertFalse(database.closed)

        self.assertTrue(jobs.stopped)
        self.assertTrue(database.closed)
        self.assertTrue(limiter.closed)


if __name__ == '__main__':
    unittest.main()
