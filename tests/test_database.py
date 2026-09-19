import asyncio
import os
from pathlib import Path
from queue import Queue
import sqlite3
import tempfile
from threading import Lock
import unittest
from unittest.mock import patch

from src.database import Database
from src.job_state_store import DatabaseStateWriter


class DatabaseStateTests(unittest.TestCase):
    def test_state_writer_admission_uses_hysteresis(self):
        class AliveThread:
            @staticmethod
            def is_alive():
                return True

        writer = DatabaseStateWriter.__new__(DatabaseStateWriter)
        writer.queue_maxsize = 10
        writer.pause_threshold = 0.8
        writer.resume_threshold = 0.5
        writer._queue = Queue(maxsize=10)
        writer._closed = False
        writer._error = None
        writer._thread = AliveThread()
        writer._inflight = 0
        writer._pressure_lock = Lock()
        writer._admission_paused = False
        for index in range(8):
            writer._queue.put(index)
        self.assertFalse(writer.admission()['accepting_work'])
        for _ in range(3):
            writer._queue.get_nowait()
        self.assertTrue(writer.admission()['accepting_work'])

    def test_state_writer_retries_transient_database_failure(self):
        class FlakyDatabase:
            attempts = 0
            saved = []

            def __init__(self, _url=None):
                pass

            async def init_schema(self):
                return None

            async def upsert_jobs(self, records):
                type(self).attempts += 1
                if type(self).attempts == 1:
                    raise RuntimeError('database temporarily read-only')
                type(self).saved.extend(records)

            async def close(self):
                return None

        environment = {
            'STATE_WRITER_MAX_RETRIES': '2',
            'STATE_WRITER_RETRY_BASE_SECONDS': '0.01',
        }
        with patch('src.job_state_store.Database', FlakyDatabase), patch.dict(
            os.environ, environment, clear=False
        ):
            writer = DatabaseStateWriter('postgresql+asyncpg://unused')
            writer.save({
                'job_id': 'retry-job',
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': '2026-09-15T00:00:00+00:00',
            })
            writer.close()
        self.assertEqual(FlakyDatabase.attempts, 2)
        self.assertEqual(FlakyDatabase.saved[0]['job_id'], 'retry-job')

    def test_state_writer_keeps_batch_after_retry_budget_is_exhausted(self):
        class RecoveringDatabase:
            attempts = 0
            saved = []

            def __init__(self, _url=None):
                pass

            async def init_schema(self):
                return None

            async def upsert_jobs(self, records):
                type(self).attempts += 1
                if type(self).attempts <= 3:
                    raise RuntimeError('database unavailable')
                type(self).saved.extend(records)

            async def close(self):
                return None

        environment = {
            'STATE_WRITER_MAX_RETRIES': '2',
            'STATE_WRITER_RETRY_BASE_SECONDS': '0.01',
        }
        with patch('src.job_state_store.Database', RecoveringDatabase), patch.dict(
            os.environ, environment, clear=False
        ):
            writer = DatabaseStateWriter('postgresql+asyncpg://unused')
            writer.save({
                'job_id': 'retained-job',
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': '2026-09-19T00:00:00+00:00',
            })
            writer.close()
        self.assertEqual(RecoveringDatabase.attempts, 4)
        self.assertEqual(RecoveringDatabase.saved[0]['job_id'], 'retained-job')

    def test_job_and_dispatch_outbox_are_staged_together(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_outbox_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'outbox.sqlite3').as_posix()}"
            record = {
                'job_id': 'outbox-job',
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-09-15T00:00:00+00:00',
                '_arguments': {'seed': 17},
                '_execution_key': 'execution-17',
                'resources': {},
                'priority': 0,
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.stage_job(record))
                    dispatchable = asyncio.run(database.list_dispatchable_jobs())
                    asyncio.run(database.mark_job_dispatched(
                        'outbox-job', '2026-09-15T00:00:01+00:00'
                    ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(len(dispatchable), 1)
            self.assertEqual(dispatchable[0]['job_id'], 'outbox-job')
            self.assertEqual(dispatchable[0]['_arguments'], {'seed': 17})

    def test_existing_local_schema_gets_execution_columns(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_upgrade_') as raw:
            path = Path(raw) / 'legacy.sqlite3'
            connection = sqlite3.connect(path)
            connection.execute(
                'CREATE TABLE job_records ('
                'job_id VARCHAR(64) PRIMARY KEY, tool VARCHAR(200) NOT NULL, '
                'status VARCHAR(32) NOT NULL, created_at VARCHAR(64) NOT NULL, '
                'started_at VARCHAR(64), finished_at VARCHAR(64), arguments JSON NOT NULL, '
                'result JSON, error TEXT, retry_of VARCHAR(64))'
            )
            connection.commit()
            connection.close()
            url = f"sqlite+aiosqlite:///{path.as_posix()}"
            record = {
                'job_id': 'legacy-job',
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-08-23T00:00:00+00:00',
                '_arguments': {},
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.upsert_job(record))
                    stored = asyncio.run(database.get_job('legacy-job'))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(stored['status'], 'queued')
            self.assertEqual(stored['attempts'], 0)

    def test_worker_state_writer_persists_execution_state(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_state_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'state.sqlite3').as_posix()}"
            record = {
                'job_id': 'job-1',
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': '2026-08-23T00:00:00+00:00',
                '_arguments': {},
                '_attempts': 2,
                '_cancel_requested': False,
                '_worker_id': 'worker-1',
                '_lease_until': 123.5,
                'resources': {
                    'cpu_cores': 2,
                    'memory_mb': 4096,
                    'gpu_count': 1,
                    'gpu_memory_mb': 12000,
                    'labels': ['cuda'],
                },
                'priority': 50,
                'trace_id': 'trace-worker-1',
                'request_id': 'request-worker-1',
                'run_context': {
                    'schema_version': 1,
                    'run_id': 'run-worker-1',
                    'trace_id': 'trace-worker-1',
                },
                'execution_identity': {'fingerprint': 'implementation-v1'},
                'routing': {'route_id': 'route-v1'},
                'execution': {
                    'worker_id': 'worker-1',
                    'identity': {'fingerprint': 'implementation-v1'},
                },
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                writer = DatabaseStateWriter(url)
                writer.save(record)
                writer.close()

            database = Database(url)
            try:
                stored = asyncio.run(database.get_job('job-1'))
            finally:
                asyncio.run(database.close())
            self.assertEqual(stored['status'], 'running')
            self.assertEqual(stored['attempts'], 2)
            self.assertEqual(stored['resources']['gpu_count'], 1)
            self.assertEqual(stored['priority'], 50)
            self.assertEqual(stored['trace_id'], 'trace-worker-1')
            self.assertEqual(stored['request_id'], 'request-worker-1')
            self.assertEqual(stored['run_context']['run_id'], 'run-worker-1')
            self.assertEqual(
                stored['execution_identity']['fingerprint'],
                'implementation-v1',
            )
            self.assertEqual(stored['routing']['route_id'], 'route-v1')
            self.assertEqual(stored['execution']['worker_id'], 'worker-1')

    def test_job_events_are_deduplicated_and_replayed_after_cursor(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_events_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'events.sqlite3').as_posix()}"
            queued = {
                'job_id': 'event-job',
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-09-16T00:00:00+00:00',
                '_arguments': {'secret_input': 'not-for-events'},
                '_revision': 1,
                '_event_id': '1000-0',
            }
            completed = {
                **queued,
                'status': 'completed',
                'finished_at': '2026-09-16T00:00:01+00:00',
                'result': {'status': 'ok'},
                '_revision': 2,
                '_event_id': '1001-0',
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.upsert_jobs([queued, queued, completed]))
                    events = asyncio.run(database.list_job_events('event-job'))
                    replay = asyncio.run(database.list_job_events(
                        'event-job',
                        after_event_id='1000-0',
                    ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual([item['event_id'] for item in events], ['1000-0', '1001-0'])
            self.assertEqual([item['event_id'] for item in replay], ['1001-0'])
            self.assertTrue(replay[0]['terminal'])
            self.assertNotIn('secret_input', str(events))

    def test_job_events_replay_cursor_beyond_first_page(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_long_events_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'events.sqlite3').as_posix()}"
            records = [
                {
                    'job_id': 'long-event-job',
                    'tool': 'research_catalog',
                    'status': 'completed' if revision == 150 else 'running',
                    'created_at': '2026-09-19T00:00:00+00:00',
                    'finished_at': (
                        '2026-09-19T00:00:01+00:00' if revision == 150 else None
                    ),
                    '_revision': revision,
                    '_event_id': f'{1000 + revision}-0',
                }
                for revision in range(1, 151)
            ]
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.upsert_jobs(records))
                    replay = asyncio.run(database.list_job_events(
                        'long-event-job',
                        after_event_id='1120-0',
                        limit=10,
                    ))
                    reset = asyncio.run(database.list_job_events(
                        'long-event-job',
                        after_event_id='missing-0',
                    ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(
                [item['event_id'] for item in replay],
                [f'{value}-0' for value in range(1121, 1131)],
            )
            self.assertNotIn('replay_gap', replay[0])
            self.assertEqual(reset[0]['event_id'], '1150-0')
            self.assertTrue(reset[0]['replay_gap'])

    def test_execution_result_is_persisted_idempotently(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_result_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'results.sqlite3').as_posix()}"
            job = {
                'job_id': 'result-job',
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': '2026-09-19T00:00:00+00:00',
                '_arguments': {},
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.upsert_job(job))
                    asyncio.run(database.begin_execution_attempt(
                        'execution-result-key',
                        'result-job',
                        'token-1',
                        1,
                    ))
                    first = asyncio.run(database.store_execution_result(
                        'execution-result-key',
                        'result-job',
                        {'status': 'ok', 'value': 17},
                        'token-1',
                    ))
                    second = asyncio.run(database.store_execution_result(
                        'execution-result-key',
                        'result-job',
                        {'status': 'ok', 'value': 99},
                        'token-2',
                    ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(first['result']['value'], 17)
            self.assertEqual(second['result']['value'], 17)
            self.assertEqual(first['result_sha256'], second['result_sha256'])

    def test_stale_execution_attempt_cannot_commit_result(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_fencing_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'fencing.sqlite3').as_posix()}"
            job = {
                'job_id': 'fenced-job',
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': '2026-09-19T00:00:00+00:00',
                '_arguments': {},
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.upsert_job(job))
                    asyncio.run(database.begin_execution_attempt(
                        'fenced-execution', 'fenced-job', 'token-old', 1
                    ))
                    asyncio.run(database.begin_execution_attempt(
                        'fenced-execution', 'fenced-job', 'token-new', 2
                    ))
                    with self.assertRaisesRegex(RuntimeError, 'fencing token is stale'):
                        asyncio.run(database.store_execution_result(
                            'fenced-execution',
                            'fenced-job',
                            {'status': 'ok', 'worker': 'old'},
                            'token-old',
                        ))
                    stored = asyncio.run(database.store_execution_result(
                        'fenced-execution',
                        'fenced-job',
                        {'status': 'ok', 'worker': 'new'},
                        'token-new',
                    ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(stored['result']['worker'], 'new')
            self.assertEqual(stored['attempt'], 2)

    def test_side_effecting_attempt_becomes_indeterminate_on_takeover(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_indeterminate_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'attempt.sqlite3').as_posix()}"
            job = {
                'job_id': 'side-effect-job',
                'tool': 'research_execute',
                'status': 'running',
                'created_at': '2026-09-19T00:00:00+00:00',
                '_arguments': {},
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.upsert_job(job))
                    asyncio.run(database.begin_execution_attempt(
                        'side-effect-execution',
                        'side-effect-job',
                        'token-old',
                        1,
                        'side_effecting',
                    ))
                    takeover = asyncio.run(database.begin_execution_attempt(
                        'side-effect-execution',
                        'side-effect-job',
                        'token-new',
                        2,
                        'side_effecting',
                    ))
                    with self.assertRaisesRegex(RuntimeError, 'status indeterminate'):
                        asyncio.run(database.store_execution_result(
                            'side-effect-execution',
                            'side-effect-job',
                            {'status': 'ok'},
                            'token-old',
                        ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(takeover['status'], 'indeterminate')
            self.assertEqual(takeover['execution_semantics'], 'side_effecting')


if __name__ == '__main__':
    unittest.main()
