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

from sqlalchemy import text

from src.database import Database
from src.job_execution import InlineToolExecutor
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
            self.assertEqual(revision, '0006_job_run_context')
            self.assertIn('run_context', columns)
            self.assertIsNone(legacy_context)

            run_context = {
                'schema_version': 1,
                'run_id': uuid4().hex,
                'trace_id': uuid4().hex,
                'tool': 'research_catalog',
                'domain': 'research',
            }
            await database.upsert_job({
                'job_id': job_id,
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-09-07T00:00:00+00:00',
                '_arguments': {},
                'run_context': run_context,
                'trace_id': run_context['trace_id'],
            })
            stored = await database.get_job(job_id)
            self.assertEqual(stored['run_context'], run_context)
            self.assertEqual(stored['trace_id'], run_context['trace_id'])
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
        finally:
            keys = list(client.scan_iter(f'{namespace}:*'))
            if keys:
                client.delete(*keys)
            manager.shutdown()

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


if __name__ == '__main__':
    unittest.main()
