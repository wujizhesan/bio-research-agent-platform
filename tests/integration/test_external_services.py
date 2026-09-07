import asyncio
import json
import os
from uuid import uuid4
import unittest

from sqlalchemy import text

from src.database import Database
from src.job_execution import InlineToolExecutor
from src.redis_job_manager import RedisJobManager


ENABLED = os.environ.get('RUN_EXTERNAL_SERVICE_TESTS') == '1'


@unittest.skipUnless(ENABLED, 'external service integration tests are disabled')
class ExternalServiceTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
