import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
from threading import Event
import time
from uuid import uuid4
import unittest
from unittest.mock import patch

from sqlalchemy import text

from src.database import Database
from src.job_execution import InlineToolExecutor
from src.job_state_store import DatabaseStateWriter
from src.redis_job_manager import RedisJobManager


ENABLED = os.environ.get('RUN_EXTERNAL_SERVICE_TESTS') == '1'


@unittest.skipUnless(ENABLED, 'external service integration tests are disabled')
class ExternalServiceTests(unittest.TestCase):
    def _real_redis(self):
        import redis

        redis_url = os.environ['REDIS_URL']
        namespace = f'ci:{uuid4().hex}'
        client = redis.Redis.from_url(redis_url, decode_responses=True)

        def cleanup():
            cleanup_client = redis.Redis.from_url(
                redis_url,
                decode_responses=True,
            )
            try:
                keys = list(cleanup_client.scan_iter(f'{namespace}:*'))
                if keys:
                    cleanup_client.delete(*keys)
            finally:
                cleanup_client.close()

        self.addCleanup(cleanup)
        return client, namespace

    def test_postgres_migration_and_job_round_trip(self):
        asyncio.run(self._postgres_round_trip())

    def test_postgres_read_only_backpressures_then_recovers(self):
        asyncio.run(self._postgres_read_only_backpressure())

    async def _postgres_read_only_backpressure(self):
        import asyncpg

        raw_url = os.environ['DATABASE_URL'].replace(
            'postgresql+asyncpg://',
            'postgresql://',
        )
        admin = await asyncpg.connect(raw_url)
        database_name = await admin.fetchval('SELECT current_database()')
        if not database_name.replace('_', '').replace('-', '').isalnum():
            self.fail(f'unsafe PostgreSQL database name: {database_name}')
        quoted_database = '"' + database_name.replace('"', '""') + '"'
        job_id = f'ci-read-only-{uuid4().hex}'
        writer = None
        read_only_enabled = False
        try:
            await admin.execute(
                f'ALTER DATABASE {quoted_database} SET default_transaction_read_only TO on'
            )
            read_only_enabled = True
            with patch.dict(os.environ, {
                'AUTO_CREATE_SCHEMA': 'false',
                'STATE_WRITER_MAX_RETRIES': '20',
                'STATE_WRITER_RETRY_BASE_SECONDS': '0.05',
                'STATE_WRITER_QUEUE_MAXSIZE': '4',
                'STATE_WRITER_PAUSE_THRESHOLD': '0.5',
                'STATE_WRITER_RESUME_THRESHOLD': '0.25',
            }, clear=False):
                writer = DatabaseStateWriter(os.environ['DATABASE_URL'])
                writer.save({
                    'job_id': job_id,
                    'tool': 'research_catalog',
                    'status': 'running',
                    'created_at': '2026-09-16T00:00:00+00:00',
                    '_revision': 1,
                })
                await asyncio.sleep(0.2)
                self.assertGreaterEqual(writer.pending(), 1)
                await admin.execute(
                    f'ALTER DATABASE {quoted_database} RESET default_transaction_read_only'
                )
                read_only_enabled = False
                await asyncio.to_thread(writer.flush)
                self.assertTrue(writer.health()['healthy'])
            database = Database(os.environ['DATABASE_URL'])
            try:
                stored = await database.get_job(job_id)
                self.assertEqual(stored['status'], 'running')
            finally:
                await database.close()
        finally:
            if read_only_enabled:
                await admin.execute(
                    f'ALTER DATABASE {quoted_database} RESET default_transaction_read_only'
                )
            await admin.close()
            if writer is not None:
                try:
                    await asyncio.to_thread(writer.close)
                except RuntimeError:
                    pass
            database = Database(os.environ['DATABASE_URL'])
            try:
                async with database.engine.begin() as connection:
                    await connection.execute(
                        text('DELETE FROM job_records WHERE job_id = :job_id'),
                        {'job_id': job_id},
                    )
            finally:
                await database.close()

    async def _postgres_round_trip(self):
        database = Database(os.environ['DATABASE_URL'])
        job_id = f'ci-postgres-{uuid4().hex}'
        try:
            await database.ping()
            async with database.engine.connect() as connection:
                revision = await connection.scalar(text('SELECT version_num FROM alembic_version'))
                columns = {
                    row[0]
                    for row in (
                        await connection.execute(text(
                            "SELECT column_name FROM information_schema.columns "
                            "WHERE table_schema = 'public' AND table_name = 'job_records'"
                        ))
                    )
                }
                legacy_context = await connection.scalar(
                    text('SELECT run_context FROM job_records WHERE job_id = :job_id'),
                    {'job_id': os.environ['CI_LEGACY_JOB_ID']},
                )
            self.assertEqual(revision, '0011_job_resolution')
            self.assertIn('run_context', columns)
            self.assertTrue({
                'execution_identity',
                'routing',
                'execution',
                'resolution',
            }.issubset(columns))
            self.assertIsNone(legacy_context)

            run_context = {
                'schema_version': 1,
                'run_id': uuid4().hex,
                'trace_id': uuid4().hex,
                'tool': 'research_catalog',
                'domain': 'research',
            }
            execution_identity = {'fingerprint': uuid4().hex}
            await database.upsert_job({
                'job_id': job_id,
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-09-07T00:00:00+00:00',
                '_arguments': {},
                'run_context': run_context,
                'trace_id': run_context['trace_id'],
                'execution_identity': execution_identity,
                'routing': {'route_id': 'postgres-route'},
            })
            stored = await database.get_job(job_id)
            self.assertEqual(stored['run_context'], run_context)
            self.assertEqual(stored['trace_id'], run_context['trace_id'])
            self.assertEqual(stored['execution_identity'], execution_identity)
        finally:
            async with database.engine.begin() as connection:
                await connection.execute(
                    text('DELETE FROM job_records WHERE job_id IN (:job_id, :legacy_job_id)'),
                    {
                        'job_id': job_id,
                        'legacy_job_id': os.environ['CI_LEGACY_JOB_ID'],
                    },
                )
            await database.close()

    def test_redis_job_lifecycle_uses_real_server(self):
        import redis

        namespace = f'ci:{uuid4().hex}'
        client = redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
        manager = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            tool_executor=InlineToolExecutor(
                lambda tool, arguments: {
                    'status': 'ok',
                    'tool': tool,
                    'arguments': arguments,
                }
            ),
        )
        try:
            self.assertTrue(client.ping())
            submitted = manager.submit(
                'research_catalog',
                {'seed': 23},
                idempotency_key='ci-real-redis',
            )
            duplicate = manager.submit(
                'research_catalog',
                {'seed': 23},
                idempotency_key='ci-real-redis',
            )
            self.assertTrue(duplicate['deduplicated'])
            self.assertEqual(duplicate['job_id'], submitted['job_id'])

            completed = manager.run_job(submitted['job_id'])
            stored = json.loads(client.get(f'{namespace}:job:{submitted["job_id"]}'))
            self.assertEqual(completed['status'], 'completed')
            self.assertEqual(completed['result']['arguments']['seed'], 23)
            self.assertEqual(stored['run_context']['job_id'], submitted['job_id'])
            self.assertEqual(stored['run_context']['trace_id'], stored['trace_id'])
            self.assertGreaterEqual(client.zcard(f'{namespace}:jobs:index'), 1)
            events = manager.read_job_events(
                submitted['job_id'],
                last_event_id='0-0',
                block_ms=0,
            )
            self.assertGreaterEqual(len(events), 3)
            self.assertEqual(events[-1][1]['status'], 'completed')
            replay = manager.read_job_events(
                submitted['job_id'],
                last_event_id=events[-2][0],
                block_ms=0,
            )
            self.assertEqual(replay[-1][0], events[-1][0])
        finally:
            keys = list(client.scan_iter(f'{namespace}:*'))
            if keys:
                client.delete(*keys)
            manager.shutdown()

    def test_real_redis_routes_pinned_job_only_to_matching_worker(self):
        import redis

        namespace = f'ci:{uuid4().hex}'
        managers = [
            RedisJobManager(
                redis_client=redis.Redis.from_url(
                    os.environ['REDIS_URL'],
                    decode_responses=True,
                ),
                namespace=namespace,
                worker_id=worker_id,
                capability_routing=True,
                enforce_capacity=worker_id != 'api',
            )
            for worker_id in ('api', 'old-worker', 'new-worker')
        ]
        api, old, new = managers
        try:
            expected = api._execution_identity('research_catalog')
            incompatible = dict(expected)
            incompatible['fingerprint'] = 'old-implementation'
            old._execution_catalog['research_catalog'] = incompatible
            new._execution_catalog['research_catalog'] = dict(expected)
            old.heartbeat_worker()
            submitted = api.submit('research_catalog', {})
            self.assertEqual(
                submitted['scheduling']['status'],
                'waiting_for_capability',
            )
            self.assertIsNone(old._next_job())
            new.heartbeat_worker()
            self.assertEqual(new._next_job(), submitted['job_id'])
            completed = new.run_job(submitted['job_id'])
            self.assertEqual(completed['status'], 'completed')
            self.assertEqual(completed['execution']['worker_id'], 'new-worker')
            self.assertEqual(
                completed['execution']['identity']['fingerprint'],
                expected['fingerprint'],
            )
        finally:
            keys = list(api.redis.scan_iter(f'{namespace}:*'))
            if keys:
                api.redis.delete(*keys)
            for manager in managers:
                manager.shutdown()

    def test_postgres_outbox_rebuilds_queue_after_redis_namespace_loss(self):
        client, namespace = self._real_redis()
        original = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            tool_executor=InlineToolExecutor(
                lambda tool, arguments: {
                    'status': 'ok',
                    'tool': tool,
                    'arguments': arguments,
                }
            ),
        )
        job_id = None
        writer = None
        recovered = None
        try:
            submitted = original.submit('research_catalog', {})
            job_id = submitted['job_id']

            async def stage_job():
                database = Database(os.environ['DATABASE_URL'])
                try:
                    await database.stage_job(original.durable_record(job_id))
                finally:
                    await database.close()

            asyncio.run(stage_job())
            keys = list(client.scan_iter(f'{namespace}:*'))
            if keys:
                client.delete(*keys)
            writer = DatabaseStateWriter(os.environ['DATABASE_URL'])
            recovered = RedisJobManager(
                redis_url=os.environ['REDIS_URL'],
                namespace=namespace,
                state_store=writer,
                tool_executor=InlineToolExecutor(
                    lambda tool, arguments: {
                        'status': 'ok',
                        'tool': tool,
                        'arguments': arguments,
                    }
                ),
            )
            self.assertEqual(recovered.rebuild_durable_queue(), [job_id])
            self.assertEqual(recovered._next_job(), job_id)
            recovered._complete_queued_item(job_id, 0.05)
            writer.flush()
            self.assertEqual(recovered.get(job_id)['status'], 'completed')

            async def verify_job():
                database = Database(os.environ['DATABASE_URL'])
                try:
                    return await database.get_job(job_id)
                finally:
                    await database.close()

            self.assertEqual(asyncio.run(verify_job())['status'], 'completed')
            async def verify_events():
                database = Database(os.environ['DATABASE_URL'])
                try:
                    return await database.list_job_events(job_id)
                finally:
                    await database.close()

            durable_events = asyncio.run(verify_events())
            self.assertEqual(durable_events[-1]['status'], 'completed')
            self.assertTrue(durable_events[-1]['terminal'])
        finally:
            if recovered is not None:
                recovered.shutdown()
            if writer is not None:
                writer.close()
            original.shutdown()
            if job_id is not None:
                async def cleanup_job():
                    database = Database(os.environ['DATABASE_URL'])
                    try:
                        async with database.engine.begin() as connection:
                            await connection.execute(
                                text('DELETE FROM job_records WHERE job_id = :job_id'),
                                {'job_id': job_id},
                            )
                    finally:
                        await database.close()

                asyncio.run(cleanup_job())

    def test_postgres_execution_result_prevents_reexecution_after_redis_loss(self):
        import redis

        namespace = f'ci:{uuid4().hex}'
        client = redis.Redis.from_url(os.environ['REDIS_URL'], decode_responses=True)
        original = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            worker_id='original-worker',
        )
        job_id = None
        writer = None
        recovered = None
        executions = []
        try:
            submitted = original.submit('research_catalog', {})
            job_id = submitted['job_id']

            async def stage_job():
                database = Database(os.environ['DATABASE_URL'])
                try:
                    await database.stage_job(original.durable_record(job_id))
                finally:
                    await database.close()

            asyncio.run(stage_job())
            claimed = original._claim(original._load(job_id))
            writer = DatabaseStateWriter(os.environ['DATABASE_URL'])
            attempt = writer.begin_execution_attempt(
                claimed['_execution_key'],
                job_id,
                claimed['_fencing_token'],
                claimed['_attempts'],
            )
            self.assertEqual(attempt['status'], 'running')
            writer.store_execution_result(
                claimed['_execution_key'],
                job_id,
                {'status': 'ok', 'source': 'durable-result'},
                claimed['_fencing_token'],
            )

            keys = list(client.scan_iter(f'{namespace}:*'))
            if keys:
                client.delete(*keys)
            recovered = RedisJobManager(
                redis_url=os.environ['REDIS_URL'],
                namespace=namespace,
                state_store=writer,
                worker_id='replacement-worker',
                tool_executor=InlineToolExecutor(
                    lambda tool, arguments: executions.append((tool, arguments))
                ),
            )
            self.assertEqual(recovered.rebuild_durable_queue(), [job_id])
            result = recovered.run_job(job_id)
            writer.flush()
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['result']['source'], 'durable-result')
            self.assertEqual(executions, [])
        finally:
            if recovered is not None:
                recovered.shutdown()
            if writer is not None:
                writer.close()
            original.shutdown()
            client.close()
            if job_id is not None:
                async def cleanup_job():
                    database = Database(os.environ['DATABASE_URL'])
                    try:
                        async with database.engine.begin() as connection:
                            await connection.execute(
                                text('DELETE FROM job_records WHERE job_id = :job_id'),
                                {'job_id': job_id},
                            )
                    finally:
                        await database.close()

                asyncio.run(cleanup_job())

    def test_killed_worker_job_is_recovered_after_lease_expiry(self):
        import redis

        client, namespace = self._real_redis()
        manager = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            lease_seconds=1,
            tool_executor=InlineToolExecutor(
                lambda tool, arguments: {
                    'status': 'ok',
                    'tool': tool,
                    'arguments': arguments,
                }
            ),
        )
        self.addCleanup(manager.shutdown)
        submitted = manager.submit('research_catalog', {'failure': 'kill'})
        child_code = '''
import os
import time
from src.redis_job_manager import RedisJobManager

manager = RedisJobManager(
    redis_url=os.environ['REDIS_URL'],
    namespace=os.environ['REDIS_TEST_NAMESPACE'],
    worker_id='worker-that-will-be-killed',
    lease_seconds=1,
)
job_id = manager._next_job()
record = manager._load(job_id)
manager._claim(record)
time.sleep(60)
'''
        environment = dict(os.environ)
        environment.update({
            'CADD_SKIP_ASSETS': '1',
            'REDIS_TEST_NAMESPACE': namespace,
        })
        process = subprocess.Popen(
            [sys.executable, '-c', child_code],
            cwd=str(Path(__file__).resolve().parents[2]),
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                raw = client.get(f'{namespace}:job:{submitted["job_id"]}')
                record = json.loads(raw) if raw else {}
                if (
                    record.get('status') == 'running'
                    and record.get('_worker_id') == 'worker-that-will-be-killed'
                ):
                    break
                if process.poll() is not None:
                    self.fail(process.stderr.read())
                time.sleep(0.1)
            else:
                self.fail('worker did not claim the Redis job')
            process.kill()
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=10)

        time.sleep(1.1)
        recovery_client = redis.Redis.from_url(
            os.environ['REDIS_URL'],
            decode_responses=True,
        )
        recoverer = RedisJobManager(
            redis_client=recovery_client,
            namespace=namespace,
            worker_id='recovery-worker',
            lease_seconds=1,
            tool_executor=InlineToolExecutor(
                lambda tool, arguments: {
                    'status': 'ok',
                    'tool': tool,
                    'arguments': arguments,
                }
            ),
        )
        self.addCleanup(recoverer.shutdown)
        self.assertEqual(recoverer.recover_stale_jobs(), [submitted['job_id']])
        self.assertEqual(recoverer._next_job(), submitted['job_id'])
        recoverer._complete_queued_item(submitted['job_id'], 0.05)
        recovered = recoverer.get(submitted['job_id'])
        self.assertEqual(recovered['status'], 'completed')
        self.assertEqual(recovered['attempts'], 2)

    def test_concurrent_lease_recovery_requeues_job_once(self):
        client, namespace = self._real_redis()
        owner = RedisJobManager(redis_client=client, namespace=namespace)
        self.addCleanup(owner.shutdown)
        submitted = owner.submit('research_catalog', {'failure': 'lease'})
        self.assertEqual(owner._next_job(), submitted['job_id'])
        client.lpush(owner._processing_key, submitted['job_id'])
        record = owner._load(submitted['job_id'])
        record.update({
            'status': 'running',
            '_attempts': 1,
            '_worker_id': 'expired-worker',
            '_lease_until': time.time() - 1,
        })
        owner._save(record)
        first = RedisJobManager(
            redis_url=os.environ['REDIS_URL'],
            namespace=namespace,
            worker_id='recovery-a',
        )
        second = RedisJobManager(
            redis_url=os.environ['REDIS_URL'],
            namespace=namespace,
            worker_id='recovery-b',
        )
        self.addCleanup(first.shutdown)
        self.addCleanup(second.shutdown)
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(
                lambda manager: manager.recover_stale_jobs(),
                (first, second),
            ))
        queue = client.lrange(owner._queue_key, 0, -1)
        processing = client.lrange(owner._processing_key, 0, -1)
        self.assertEqual(queue.count(submitted['job_id']), 1)
        self.assertNotIn(submitted['job_id'], processing)
        self.assertEqual(
            sum(submitted['job_id'] in result for result in results),
            1,
        )

    def test_duplicate_delivery_executes_job_once(self):
        import redis

        client, namespace = self._real_redis()
        second_client = redis.Redis.from_url(
            os.environ['REDIS_URL'],
            decode_responses=True,
        )
        started = Event()
        release = Event()
        self.addCleanup(release.set)
        counter_key = f'{namespace}:executions'

        class BlockingExecutor:
            def execute(self, *_args, **_kwargs):
                client.incr(counter_key)
                started.set()
                release.wait(10)
                return {'status': 'ok'}

        executor = BlockingExecutor()
        first = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            worker_id='duplicate-a',
            tool_executor=executor,
        )
        second = RedisJobManager(
            redis_client=second_client,
            namespace=namespace,
            worker_id='duplicate-b',
            tool_executor=executor,
        )
        self.addCleanup(first.shutdown)
        self.addCleanup(second.shutdown)
        submitted = first.submit('research_catalog', {'delivery': 'duplicate'})
        client.lpush(first._queue_key, submitted['job_id'])
        first_item = first._next_job()
        second_item = second._next_job()
        self.assertEqual(first_item, submitted['job_id'])
        self.assertEqual(second_item, submitted['job_id'])
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(first._complete_queued_item, first_item, 0.05)
            self.assertTrue(started.wait(10))
            second._complete_queued_item(second_item, 0.05)
            release.set()
            future.result(timeout=10)
        self.assertEqual(int(client.get(counter_key)), 1)
        self.assertEqual(first.get(submitted['job_id'])['status'], 'completed')
        self.assertEqual(client.lrange(first._processing_key, 0, -1), [])

    def test_connection_pool_disconnect_reconnects_transparently(self):
        client, namespace = self._real_redis()
        manager = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            tool_executor=InlineToolExecutor(
                lambda _tool, arguments: {
                    'status': 'ok',
                    'arguments': arguments,
                }
            ),
        )
        self.addCleanup(manager.shutdown)
        submitted = manager.submit('research_catalog', {'reconnect': True})
        client.connection_pool.disconnect()
        self.assertTrue(client.ping())
        self.assertEqual(manager._next_job(), submitted['job_id'])
        manager._complete_queued_item(submitted['job_id'], 0.05)
        completed = manager.get(submitted['job_id'])
        self.assertEqual(completed['status'], 'completed')
        self.assertTrue(completed['result']['arguments']['reconnect'])

    def test_network_partition_during_cancel_is_observed_after_reconnect(self):
        import redis

        client, namespace = self._real_redis()
        control_client = redis.Redis.from_url(
            os.environ['REDIS_URL'],
            decode_responses=True,
        )
        started = Event()

        class CancellableExecutor:
            def execute(self, _tool, _arguments, cancelled=None, heartbeat=None):
                started.set()
                deadline = time.monotonic() + 10
                while time.monotonic() < deadline:
                    if cancelled and cancelled():
                        return {'status': 'ok', 'cancel_observed': True}
                    if heartbeat:
                        heartbeat()
                    time.sleep(0.05)
                raise RuntimeError('cancellation was not observed after reconnect')

        worker = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            worker_id='partitioned-worker',
            tool_executor=CancellableExecutor(),
        )
        control = RedisJobManager(
            redis_client=control_client,
            namespace=namespace,
            worker_id='control-worker',
        )
        self.addCleanup(worker.shutdown)
        self.addCleanup(control.shutdown)
        submitted = worker.submit('research_catalog', {'cancel': 'during-partition'})
        job_id = worker._next_job()
        original_load = worker._load

        def disconnected_load(_job_id):
            raise ConnectionError('simulated worker network partition')

        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(worker._complete_queued_item, job_id, 0.05)
            self.assertTrue(started.wait(10))
            worker._load = disconnected_load
            cancelled = control.cancel(submitted['job_id'])
            self.assertTrue(cancelled['cancel_requested'])
            time.sleep(0.6)
            self.assertFalse(future.done())
            client.connection_pool.disconnect()
            worker._load = original_load
            future.result(timeout=10)
        completed = control.get(submitted['job_id'])
        self.assertEqual(completed['status'], 'cancelled')

    def test_dead_letter_job_can_be_retried_and_completed(self):
        client, namespace = self._real_redis()

        class RecoverableExecutor:
            failing = True

            def execute(self, *_args, **_kwargs):
                if self.failing:
                    return {'status': 'error', 'error': 'transient failure'}
                return {'status': 'ok', 'recovered': True}

        executor = RecoverableExecutor()
        manager = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            tool_executor=executor,
        )
        self.addCleanup(manager.shutdown)
        submitted = manager.submit('research_catalog', {'failure': 'transient'})
        self.assertEqual(manager._next_job(), submitted['job_id'])
        manager._complete_queued_item(submitted['job_id'], 0.05)
        failed = manager.get(submitted['job_id'])
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['dead_letter_reason'], 'execution_failed')
        self.assertIn(
            submitted['job_id'],
            client.lrange(manager._dead_letter_key, 0, -1),
        )
        executor.failing = False
        retried = manager.retry(submitted['job_id'])
        self.assertEqual(manager._next_job(), retried['job_id'])
        manager._complete_queued_item(retried['job_id'], 0.05)
        completed = manager.get(retried['job_id'])
        self.assertEqual(completed['status'], 'completed')
        self.assertEqual(completed['retry_of'], submitted['job_id'])
        self.assertTrue(completed['result']['recovered'])

    def test_indeterminate_resolution_is_persisted_before_retry(self):
        client, namespace = self._real_redis()
        writer = DatabaseStateWriter(os.environ['DATABASE_URL'])
        manager = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            state_store=writer,
        )
        self.addCleanup(manager.shutdown)
        self.addCleanup(writer.close)
        submitted = manager.submit('research_catalog', {})
        record = manager._load(submitted['job_id'])
        record.update({
            'status': 'indeterminate',
            'error': 'external commit outcome is unknown',
            'indeterminate': {'requires_manual_review': True},
        })
        manager._save(record)
        with self.assertRaisesRegex(ValueError, 'approve_retry'):
            manager.retry(submitted['job_id'])

        resolved = manager.resolve_indeterminate(
            submitted['job_id'],
            'approve_retry',
            'external system confirms no result was committed',
            'integration-reviewer',
            evidence={'ticket': 'CI-1'},
        )
        writer.flush()
        self.assertEqual(resolved['resolution']['decision'], 'approve_retry')
        database = Database(os.environ['DATABASE_URL'])
        try:
            stored = asyncio.run(database.get_job(submitted['job_id']))
        finally:
            asyncio.run(database.close())
        self.assertEqual(stored['resolution']['reviewer'], 'integration-reviewer')
        retried = manager.retry(submitted['job_id'])
        self.assertEqual(retried['retry_of'], submitted['job_id'])


if __name__ == '__main__':
    unittest.main()
