import asyncio
import os
from pathlib import Path
from queue import Queue
import sqlite3
import tempfile
from threading import Lock
import unittest
from unittest.mock import patch

from src.auth import Principal, roles_sha256
from src.database import Database, set_database_principal
from src.job_state_store import DatabaseStateWriter


class DatabaseStateTests(unittest.TestCase):
    def test_auth_session_revocation_and_subject_version_are_durable(self):
        with tempfile.TemporaryDirectory(prefix='bio_auth_sessions_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'auth.sqlite3').as_posix()}"
            database = Database(url)
            try:
                asyncio.run(database.init_schema())
                role_hash = roles_sha256(('researcher',))
                session = asyncio.run(database.create_auth_session(
                    'alice',
                    role_hash,
                    3600,
                ))
                self.assertTrue(asyncio.run(database.validate_auth_session(
                    session['jti'],
                    'alice',
                    session['token_version'],
                    role_hash,
                )))
                set_database_principal(Principal(
                    'alice',
                    ('researcher',),
                    'jwt',
                    session['jti'],
                    session['token_version'],
                ))
                self.assertTrue(asyncio.run(
                    database.revoke_auth_session(session['jti'])
                ))
                self.assertFalse(asyncio.run(database.validate_auth_session(
                    session['jti'],
                    'alice',
                    session['token_version'],
                    role_hash,
                )))
                replacement = asyncio.run(database.create_auth_session(
                    'alice',
                    role_hash,
                    3600,
                ))
                set_database_principal(Principal(
                    'root',
                    ('admin',),
                    'jwt',
                    'admin-session-placeholder',
                ))
                self.assertTrue(asyncio.run(
                    database.revoke_subject_sessions('alice', disabled=True)
                ))
                self.assertFalse(asyncio.run(database.validate_auth_session(
                    replacement['jti'],
                    'alice',
                    replacement['token_version'],
                    role_hash,
                )))
                with self.assertRaisesRegex(PermissionError, 'disabled'):
                    asyncio.run(database.create_auth_session(
                        'alice',
                        role_hash,
                        3600,
                    ))
            finally:
                set_database_principal()
                asyncio.run(database.close())

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
                    asyncio.run(database.create_project(
                        'project-1',
                        'Outbox project',
                        None,
                        'alice',
                        '2026-09-15T00:00:00+00:00',
                    ))
                    asyncio.run(database.stage_job(
                        record,
                        project_id='project-1',
                        ownership_created_at='2026-09-15T00:00:00+00:00',
                    ))
                    dispatchable = asyncio.run(database.list_dispatchable_jobs())
                    project_id = asyncio.run(database.get_job_project('outbox-job'))
                    asyncio.run(database.mark_job_dispatched(
                        'outbox-job', '2026-09-15T00:00:01+00:00'
                    ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(len(dispatchable), 1)
            self.assertEqual(dispatchable[0]['job_id'], 'outbox-job')
            self.assertEqual(dispatchable[0]['_arguments'], {'seed': 17})
            self.assertEqual(project_id, 'project-1')

    def test_job_stage_rolls_back_when_project_binding_fails(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_ownership_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'ownership.sqlite3').as_posix()}"
            record = {
                'job_id': 'unowned-job',
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-09-15T00:00:00+00:00',
                '_arguments': {},
                'resources': {},
                'priority': 0,
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    with self.assertRaisesRegex(ValueError, 'project not found'):
                        asyncio.run(database.stage_job(
                            record,
                            project_id='missing-project',
                        ))
                    stored = asyncio.run(database.get_job('unowned-job'))
                    dispatchable = asyncio.run(database.list_dispatchable_jobs())
                finally:
                    asyncio.run(database.close())
            self.assertIsNone(stored)
            self.assertEqual(dispatchable, [])

    def test_dispatch_claims_page_past_reconciled_jobs(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_dispatch_lease_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'dispatch.sqlite3').as_posix()}"
            created_at = '2026-09-20T00:00:00+00:00'
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.create_project(
                        'project-1', 'Dispatch project', None, 'alice', created_at
                    ))
                    for index in range(3):
                        asyncio.run(database.stage_job({
                            'job_id': f'dispatch-job-{index}',
                            'tool': 'research_catalog',
                            'status': 'queued',
                            'created_at': f'2026-09-20T00:00:0{index}+00:00',
                            '_arguments': {},
                            '_execution_key': f'execution-capability-{index:032d}',
                            'resources': {},
                            'priority': 0,
                        }, project_id='project-1'))
                    first = asyncio.run(database.claim_dispatch_batch(
                        'dispatcher-a', limit=1, lease_seconds=30
                    ))
                    asyncio.run(database.complete_dispatch_claims(
                        'dispatcher-a',
                        [{
                            'job_id': first[0]['job_id'],
                            'generation': first[0]['_dispatch_generation'],
                            'succeeded': True,
                        }],
                        reconcile_seconds=60,
                    ))
                    second = asyncio.run(database.claim_dispatch_batch(
                        'dispatcher-b', limit=1, lease_seconds=30
                    ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(first[0]['job_id'], 'dispatch-job-0')
            self.assertEqual(second[0]['job_id'], 'dispatch-job-1')
            self.assertNotEqual(first[0]['_claim_ticket'], second[0]['_claim_ticket'])

    def test_worker_claim_ticket_is_single_use(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_claim_ticket_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'claim.sqlite3').as_posix()}"
            created_at = '2026-09-20T00:00:00+00:00'
            capability = 'worker-capability-' + ('a' * 32)
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.create_project(
                        'project-1', 'Claim project', None, 'alice', created_at
                    ))
                    asyncio.run(database.stage_job({
                        'job_id': 'claim-ticket-job',
                        'tool': 'research_catalog',
                        'status': 'queued',
                        'created_at': created_at,
                        '_arguments': {},
                        '_execution_key': capability,
                        'resources': {},
                        'priority': 0,
                    }, project_id='project-1'))
                    dispatched = asyncio.run(database.claim_dispatch_batch(
                        'dispatcher-a', limit=1, lease_seconds=30
                    ))[0]
                    first = asyncio.run(database.claim_worker_job(
                        'claim-ticket-job',
                        capability,
                        'worker-a',
                        dispatched['_claim_ticket'],
                        30,
                    ))
                    replay = asyncio.run(database.claim_worker_job(
                        'claim-ticket-job',
                        capability,
                        'worker-a',
                        dispatched['_claim_ticket'],
                        30,
                    ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(first['attempt'], 1)
            self.assertEqual(first['fencing_token'], '1')
            self.assertIsNone(replay)

    def test_tenant_ownership_is_immutable_and_queries_are_scoped(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_tenant_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'tenant.sqlite3').as_posix()}"
            created_at = '2026-09-19T00:00:00+00:00'
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.create_project(
                        'project-a', 'Project A', None, 'alice', created_at
                    ))
                    asyncio.run(database.create_project(
                        'project-b', 'Project B', None, 'bob', created_at
                    ))
                    for job_id, project_id in (
                        ('job-a', 'project-a'),
                        ('job-b', 'project-b'),
                    ):
                        asyncio.run(database.upsert_job_with_project({
                            'job_id': job_id,
                            'tool': 'research_catalog',
                            'status': 'queued',
                            'created_at': created_at,
                            '_arguments': {},
                            '_revision': 1,
                        }, project_id))
                    asyncio.run(database.upsert_job({
                        'job_id': 'job-a',
                        'tool': 'research_catalog',
                        'status': 'running',
                        'created_at': created_at,
                        '_arguments': {},
                        '_revision': 2,
                    }))
                    alice_jobs = asyncio.run(database.list_jobs_for_principal('alice'))
                    admin_jobs = asyncio.run(database.list_jobs_for_principal(
                        'admin', is_admin=True
                    ))
                    stored = asyncio.run(database.get_job('job-a'))
                    events = asyncio.run(database.list_job_events('job-a'))
                    with self.assertRaisesRegex(ValueError, 'another project'):
                        asyncio.run(database.upsert_job_with_project({
                            'job_id': 'job-a',
                            'tool': 'research_catalog',
                            'status': 'running',
                            'created_at': created_at,
                            '_arguments': {},
                        }, 'project-b'))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(stored['project_id'], 'project-a')
            self.assertEqual(events[-1]['job']['project_id'], 'project-a')
            self.assertEqual({item['job_id'] for item in alice_jobs}, {'job-a'})
            self.assertEqual({item['job_id'] for item in admin_jobs}, {'job-a', 'job-b'})

    def test_project_member_can_be_removed_but_owner_is_preserved(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_members_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'members.sqlite3').as_posix()}"
            created_at = '2026-09-20T00:00:00+00:00'
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.create_project(
                        'project-a', 'Project A', None, 'alice', created_at
                    ))
                    asyncio.run(database.upsert_project_member(
                        'project-a', 'bob', 'viewer', created_at
                    ))
                    self.assertTrue(asyncio.run(
                        database.delete_project_member('project-a', 'bob')
                    ))
                    self.assertFalse(asyncio.run(
                        database.delete_project_member('project-a', 'bob')
                    ))
                    with self.assertRaisesRegex(ValueError, 'owner membership'):
                        asyncio.run(database.delete_project_member(
                            'project-a', 'alice'
                        ))
                finally:
                    asyncio.run(database.close())

    def test_file_record_persists_storage_identity_and_rejects_reassignment(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_file_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'file.sqlite3').as_posix()}"
            created_at = '2026-09-19T00:00:00+00:00'
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    for project_id, owner in (('project-a', 'alice'), ('project-b', 'bob')):
                        asyncio.run(database.create_project(
                            project_id, project_id, None, owner, created_at
                        ))
                    asyncio.run(database.assign_file_project(
                        'file-a',
                        'project-a',
                        created_at,
                        filename='reads.fastq',
                        storage_backend='s3',
                        storage_key='project-a/file-a/reads.fastq',
                        version_id='version-1',
                        sha256='a' * 64,
                        size_bytes=17,
                    ))
                    stored = asyncio.run(database.get_file_record('file-a'))
                    with self.assertRaisesRegex(ValueError, 'another project'):
                        asyncio.run(database.assign_file_project(
                            'file-a', 'project-b', created_at
                        ))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(stored['project_id'], 'project-a')
            self.assertEqual(stored['storage_backend'], 's3')
            self.assertEqual(stored['version_id'], 'version-1')
            self.assertEqual(stored['status'], 'active')

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


    def test_database_idempotency_claim_survives_new_job_attempt(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_idempotency_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'idempotency.sqlite3').as_posix()}"
            created_at = '2026-09-20T00:00:00+00:00'
            key = f"scoped:{'a' * 64}"
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.create_project(
                        'project-idempotency',
                        'Idempotency project',
                        None,
                        'alice',
                        created_at,
                    ))
                    first = asyncio.run(database.stage_job({
                        'job_id': 'job-original',
                        'tool': 'research_catalog',
                        'status': 'queued',
                        'created_at': created_at,
                        '_arguments': {'query': 'egfr'},
                        '_execution_key': 'a' * 32,
                    }, project_id='project-idempotency', idempotency_subject='alice',
                        idempotency_key=key, idempotency_payload_hash='b' * 64))
                    duplicate = asyncio.run(database.stage_job({
                        'job_id': 'job-duplicate',
                        'tool': 'research_catalog',
                        'status': 'queued',
                        'created_at': created_at,
                        '_arguments': {'query': 'egfr'},
                        '_execution_key': 'c' * 32,
                    }, project_id='project-idempotency', idempotency_subject='alice',
                        idempotency_key=key, idempotency_payload_hash='b' * 64))
                    duplicate_row = asyncio.run(database.get_job('job-duplicate'))
                    with self.assertRaisesRegex(ValueError, 'different job payload'):
                        asyncio.run(database.get_idempotent_job(
                            key, 'alice', 'project-idempotency', 'd' * 64
                        ))
                finally:
                    asyncio.run(database.close())
            self.assertFalse(first['deduplicated'])
            self.assertEqual(duplicate, {
                'job_id': 'job-original',
                'deduplicated': True,
            })
            self.assertIsNone(duplicate_row)

    def test_job_artifact_manifest_round_trips(self):
        with tempfile.TemporaryDirectory(prefix='bio_job_artifacts_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'artifacts.sqlite3').as_posix()}"
            database = Database(url)
            artifact = {
                'artifact_id': 'a' * 32,
                'filename': 'report.json',
                'size_bytes': 42,
                'sha256': 'b' * 64,
                'storage_backend': 's3',
                'storage_key': 'bio-agent/artifacts/project/job/report.json',
                'version_id': 'version-1',
            }
            try:
                asyncio.run(database.init_schema())
                asyncio.run(database.upsert_job({
                    'job_id': 'artifact-job',
                    'tool': 'research_catalog',
                    'status': 'completed',
                    'created_at': '2026-09-21T00:00:00+00:00',
                    'finished_at': '2026-09-21T00:00:01+00:00',
                    '_arguments': {},
                    'result': {'status': 'ok'},
                    'artifacts': [artifact],
                }))
                stored = asyncio.run(database.get_job('artifact-job'))
            finally:
                asyncio.run(database.close())
            self.assertEqual(stored['artifacts'], [artifact])

    def test_job_artifact_lifecycle_is_durable_and_terminal(self):
        with tempfile.TemporaryDirectory(prefix='bio_artifact_lifecycle_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'lifecycle.sqlite3').as_posix()}"
            database = Database(url)
            reservation = {
                'publication_id': 'p' * 64,
                'artifact_id': 'a' * 32,
                'job_id': 'artifact-lifecycle-job',
                'project_id': 'system-legacy',
                'execution_key': 'e' * 32,
                'fencing_token': '7',
                'attempt': 1,
                'parameter': 'output_path',
                'kind': 'file',
                'storage_backend': 's3',
                'filename': 'result.json',
                'storage_key': 'bio-agent/artifacts/system/job/result.json',
            }
            try:
                asyncio.run(database.init_schema())
                asyncio.run(database.upsert_job({
                    'job_id': 'artifact-lifecycle-job',
                    'tool': 'research_catalog',
                    'status': 'running',
                    'created_at': '2026-09-21T00:00:00+00:00',
                    '_arguments': {},
                }))
                reserved = asyncio.run(database.reserve_job_artifacts([reservation]))
                self.assertEqual(reserved[0]['status'], 'reserved')
                uploaded = asyncio.run(database.mark_job_artifacts_uploaded([{
                    **reservation,
                    'content_type': 'application/json',
                    'size_bytes': 16,
                    'sha256': 'b' * 64,
                    'version_id': 'version-1',
                    'reference': 'bio+s3://bucket/key',
                }]))
                self.assertEqual(uploaded[0]['status'], 'uploaded')
                committed = asyncio.run(database.commit_job_artifacts(['p' * 64]))
                self.assertEqual(committed[0]['status'], 'committed')
                asyncio.run(database.orphan_job_artifacts(
                    ['p' * 64],
                    'late rollback',
                ))
                stored = asyncio.run(database.list_job_artifacts(
                    'artifact-lifecycle-job'
                ))
            finally:
                asyncio.run(database.close())
            self.assertEqual(stored[0]['status'], 'committed')
            self.assertNotIn('last_error', stored[0])

    def test_artifact_recovery_claim_blocks_late_worker_commit(self):
        with tempfile.TemporaryDirectory(prefix='bio_artifact_claim_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'claim.sqlite3').as_posix()}"
            database = Database(url)
            reservation = {
                'publication_id': 'r' * 64,
                'artifact_id': 'c' * 32,
                'job_id': 'artifact-claim-job',
                'project_id': 'system-legacy',
                'execution_key': 'x' * 32,
                'fencing_token': '9',
                'attempt': 1,
                'parameter': 'output_path',
                'kind': 'file',
                'storage_backend': 's3',
                'filename': 'claim.json',
                'storage_key': 'bio-agent/artifacts/system/job/claim.json',
            }

            async def scenario():
                await database.init_schema()
                await database.upsert_job({
                    'job_id': reservation['job_id'],
                    'tool': 'research_catalog',
                    'status': 'running',
                    'created_at': '2026-09-21T00:00:00+00:00',
                    '_arguments': {},
                })
                await database.begin_execution_attempt(
                    reservation['execution_key'],
                    reservation['job_id'],
                    reservation['fencing_token'],
                    1,
                )
                await database.reserve_job_artifacts([reservation])
                await database.mark_job_artifacts_uploaded([{
                    **reservation,
                    'content_type': 'application/json',
                    'size_bytes': 16,
                    'sha256': 'd' * 64,
                    'version_id': 'version-1',
                    'reference': 'bio+s3://bucket/key',
                }])
                none = await database.claim_recoverable_job_artifacts(
                    'gc-running',
                    '9999-01-01T00:00:00+00:00',
                )
                self.assertEqual(none, [])
                await database.upsert_job({
                    'job_id': reservation['job_id'],
                    'tool': 'research_catalog',
                    'status': 'failed',
                    'created_at': '2026-09-21T00:00:00+00:00',
                    'finished_at': '2026-09-21T00:10:00+00:00',
                    '_arguments': {},
                    'error': 'worker lost',
                })
                claimed = await database.claim_recoverable_job_artifacts(
                    'gc-claimed',
                    '9999-01-01T00:00:00+00:00',
                )
                self.assertEqual(claimed[0]['status'], 'reclaiming')
                with self.assertRaises(RuntimeError):
                    await database.commit_job_artifacts([
                        reservation['publication_id']
                    ])
                with self.assertRaises(RuntimeError):
                    await database.finish_job_artifact_recovery(
                        reservation['publication_id'],
                        'wrong-token',
                        'deleted',
                    )
                deleted = await database.finish_job_artifact_recovery(
                    reservation['publication_id'],
                    'gc-claimed',
                    'deleted',
                )
                self.assertEqual(deleted['status'], 'deleted')

            try:
                asyncio.run(scenario())
            finally:
                asyncio.run(database.close())

    def test_project_storage_quota_reserves_commits_and_releases_atomically(self):
        with tempfile.TemporaryDirectory(prefix='bio_storage_quota_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'quota.sqlite3').as_posix()}"
            database = Database(url)

            async def scenario():
                await database.init_schema()
                await database.create_project(
                    'project-quota',
                    'Quota project',
                    None,
                    'alice',
                    '2026-09-21T00:00:00+00:00',
                )
                await database.begin_file_upload(
                    'a' * 32,
                    'project-quota',
                    'first.csv',
                    's3',
                    'bio-agent/' + 'a' * 32 + '/first.csv',
                    60,
                    100,
                    '2026-09-21T00:00:00+00:00',
                )
                usage = await database.get_project_storage_usage('project-quota')
                self.assertEqual((usage['used_bytes'], usage['reserved_bytes']), (0, 60))
                with self.assertRaisesRegex(ValueError, 'quota'):
                    await database.begin_file_upload(
                        'b' * 32,
                        'project-quota',
                        'second.csv',
                        's3',
                        'bio-agent/' + 'b' * 32 + '/second.csv',
                        50,
                        100,
                        '2026-09-21T00:00:01+00:00',
                    )
                await database.activate_file_upload(
                    'a' * 32,
                    {
                        'filename': 'first.csv',
                        'storage_key': 'bio-agent/' + 'a' * 32 + '/first.csv',
                        'version_id': 'version-1',
                        'sha256': '1' * 64,
                        'size_bytes': 40,
                    },
                    '2026-09-21T00:00:02+00:00',
                )
                await database.begin_file_upload(
                    'b' * 32,
                    'project-quota',
                    'second.csv',
                    's3',
                    'bio-agent/' + 'b' * 32 + '/second.csv',
                    50,
                    100,
                    '2026-09-21T00:00:03+00:00',
                )
                await database.fail_file_upload(
                    'b' * 32,
                    'upload interrupted',
                    '2026-09-21T00:00:04+00:00',
                )
                claimed = await database.claim_recoverable_files(
                    'recovery-1',
                    '9999-01-01T00:00:00+00:00',
                )
                self.assertEqual([item['file_id'] for item in claimed], ['b' * 32])
                await database.finish_file_recovery(
                    'b' * 32,
                    'recovery-1',
                    'deleted',
                )
                usage = await database.get_project_storage_usage('project-quota')
                self.assertEqual((usage['used_bytes'], usage['reserved_bytes']), (40, 0))

            try:
                asyncio.run(scenario())
            finally:
                asyncio.run(database.close())

    def test_explicit_file_deletion_is_idempotent_and_releases_after_confirmation(self):
        with tempfile.TemporaryDirectory(prefix='bio_file_delete_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'delete.sqlite3').as_posix()}"
            database = Database(url)
            file_id = 'd' * 32

            async def scenario():
                await database.init_schema()
                await database.create_project(
                    'project-delete',
                    'Delete project',
                    None,
                    'alice',
                    '2026-09-21T00:00:00+00:00',
                )
                await database.begin_file_upload(
                    file_id,
                    'project-delete',
                    'delete.csv',
                    's3',
                    f'bio-agent/{file_id}/delete.csv',
                    32,
                    100,
                    '2026-09-21T00:00:00+00:00',
                )
                await database.activate_file_upload(
                    file_id,
                    {
                        'filename': 'delete.csv',
                        'storage_key': f'bio-agent/{file_id}/delete.csv',
                        'version_id': 'version-delete',
                        'sha256': '2' * 64,
                        'size_bytes': 24,
                    },
                    '2026-09-21T00:00:01+00:00',
                )
                requested = await database.request_file_deletion(
                    file_id,
                    'alice',
                    'request-one',
                    '2026-09-21T00:00:02+00:00',
                )
                repeated = await database.request_file_deletion(
                    file_id,
                    'alice',
                    'request-two',
                    '2026-09-21T00:00:03+00:00',
                )
                self.assertEqual(requested['status'], 'delete_requested')
                self.assertEqual(repeated['delete_request_id'], 'request-one')
                usage = await database.get_project_storage_usage('project-delete')
                self.assertEqual(usage['used_bytes'], 24)
                claimed = await database.claim_recoverable_files(
                    'delete-worker',
                    '1970-01-01T00:00:00+00:00',
                )
                self.assertEqual(claimed[0]['status'], 'deleting')
                with self.assertRaises(RuntimeError):
                    await database.finish_file_recovery(
                        file_id,
                        'delete-worker',
                        'orphaned',
                    )
                deleted = await database.finish_file_recovery(
                    file_id,
                    'delete-worker',
                    'deleted',
                )
                self.assertIsNotNone(deleted['deleted_at'])
                usage = await database.get_project_storage_usage('project-delete')
                self.assertEqual(usage['used_bytes'], 0)

            try:
                asyncio.run(scenario())
            finally:
                asyncio.run(database.close())

    def test_explicit_artifact_deletion_cannot_restore_committed_state(self):
        with tempfile.TemporaryDirectory(prefix='bio_artifact_delete_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'delete.sqlite3').as_posix()}"
            database = Database(url)
            publication_id = 'v' * 64
            artifact_id = '9' * 32

            async def scenario():
                await database.init_schema()
                await database.upsert_job({
                    'job_id': 'artifact-delete-job',
                    'tool': 'research_catalog',
                    'status': 'completed',
                    'created_at': '2026-09-21T00:00:00+00:00',
                    '_arguments': {},
                })
                reservation = {
                    'publication_id': publication_id,
                    'artifact_id': artifact_id,
                    'job_id': 'artifact-delete-job',
                    'project_id': 'system-legacy',
                    'execution_key': 'w' * 32,
                    'fencing_token': '12',
                    'attempt': 1,
                    'parameter': 'output_path',
                    'kind': 'file',
                    'storage_backend': 's3',
                    'filename': 'delete.json',
                    'storage_key': 'bio-agent/artifacts/system/job/delete.json',
                }
                await database.reserve_job_artifacts([reservation])
                await database.mark_job_artifacts_uploaded([{
                    **reservation,
                    'content_type': 'application/json',
                    'size_bytes': 8,
                    'sha256': '3' * 64,
                    'version_id': 'version-delete',
                    'reference': 'bio+s3://bucket/delete',
                }])
                await database.commit_job_artifacts([publication_id])
                requested = await database.request_job_artifact_deletion(
                    'artifact-delete-job',
                    artifact_id,
                    'local-dev',
                    'artifact-delete-request',
                    '2026-09-21T00:00:02+00:00',
                )
                self.assertEqual(requested['status'], 'delete_requested')
                claimed = await database.claim_recoverable_job_artifacts(
                    'artifact-delete-worker',
                    '1970-01-01T00:00:00+00:00',
                )
                self.assertEqual(claimed[0]['status'], 'deleting')
                with self.assertRaises(RuntimeError):
                    await database.finish_job_artifact_recovery(
                        publication_id,
                        'artifact-delete-worker',
                        'committed',
                    )
                deleted = await database.finish_job_artifact_recovery(
                    publication_id,
                    'artifact-delete-worker',
                    'deleted',
                )
                self.assertIsNotNone(deleted['deleted_at'])

            try:
                asyncio.run(scenario())
            finally:
                asyncio.run(database.close())

    def test_deletion_failure_enters_dead_letter_and_audit_chain_can_retry(self):
        with tempfile.TemporaryDirectory(prefix='bio_delete_dead_letter_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'delete.sqlite3').as_posix()}"
            database = Database(url)
            file_id = '8' * 32

            async def scenario():
                await database.init_schema()
                await database.create_project(
                    'project-dead-letter',
                    'Dead letter project',
                    None,
                    'alice',
                    '2026-09-21T00:00:00+00:00',
                )
                await database.begin_file_upload(
                    file_id,
                    'project-dead-letter',
                    'blocked.csv',
                    's3',
                    f'bio-agent/{file_id}/blocked.csv',
                    16,
                    100,
                    '2026-09-21T00:00:00+00:00',
                )
                await database.activate_file_upload(
                    file_id,
                    {
                        'filename': 'blocked.csv',
                        'storage_key': f'bio-agent/{file_id}/blocked.csv',
                        'version_id': 'version-blocked',
                        'sha256': '4' * 64,
                        'size_bytes': 12,
                    },
                    '2026-09-21T00:00:01+00:00',
                )
                await database.request_file_deletion(
                    file_id,
                    'alice',
                    'delete-blocked',
                    '2026-09-21T00:00:02+00:00',
                )
                claimed = await database.claim_recoverable_files(
                    'delete-worker',
                    '1970-01-01T00:00:00+00:00',
                )
                self.assertEqual(claimed[0]['delete_attempts'], 1)
                failed = await database.finish_file_recovery(
                    file_id,
                    'delete-worker',
                    'delete_failed',
                    error='access denied',
                )
                self.assertEqual(failed['status'], 'delete_dead_letter')
                self.assertIsNone(failed.get('delete_next_attempt_at'))
                events = await database.list_storage_deletion_events(
                    'file',
                    file_id,
                )
                self.assertEqual(
                    [event['status'] for event in events],
                    ['delete_requested', 'delete_dead_letter'],
                )
                self.assertEqual(events[1]['previous_hash'], events[0]['event_hash'])
                retried = await database.retry_file_deletion(
                    file_id,
                    'alice',
                    'retry-blocked',
                    '2026-09-21T00:00:03+00:00',
                )
                self.assertEqual(retried['status'], 'delete_requested')
                self.assertEqual(retried['delete_attempts'], 0)
                repeated = await database.retry_file_deletion(
                    file_id,
                    'alice',
                    'retry-blocked',
                    '2026-09-21T00:00:04+00:00',
                )
                self.assertTrue(repeated['_deduplicated'])
                events = await database.list_storage_deletion_events('file', file_id)
                self.assertEqual(events[-1]['status'], 'retry_requested')
                self.assertEqual(events[-1]['previous_hash'], events[-2]['event_hash'])
                self.assertEqual(
                    [event['status'] for event in events].count('retry_requested'),
                    1,
                )
                metrics = await database.storage_deletion_metrics()
                retry_events = [
                    item for item in metrics['events']
                    if item['resource_type'] == 'file'
                    and item['status'] == 'retry_requested'
                ]
                self.assertEqual(retry_events[0]['count'], 1)
                usage = await database.get_project_storage_usage(
                    'project-dead-letter'
                )
                self.assertEqual(usage['used_bytes'], 12)

            try:
                with patch.dict(os.environ, {'ARTIFACT_DELETE_MAX_ATTEMPTS': '1'}):
                    asyncio.run(scenario())
            finally:
                asyncio.run(database.close())

    def test_execution_result_and_artifacts_commit_atomically(self):
        with tempfile.TemporaryDirectory(prefix='bio_artifact_atomic_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'atomic.sqlite3').as_posix()}"
            database = Database(url)
            publication_id = 't' * 64

            async def scenario():
                await database.init_schema()
                await database.upsert_job({
                    'job_id': 'artifact-atomic-job',
                    'tool': 'research_catalog',
                    'status': 'running',
                    'created_at': '2026-09-21T00:00:00+00:00',
                    '_arguments': {},
                })
                await database.begin_execution_attempt(
                    'z' * 32,
                    'artifact-atomic-job',
                    '11',
                    1,
                )
                reservation = {
                    'publication_id': publication_id,
                    'artifact_id': 'f' * 32,
                    'job_id': 'artifact-atomic-job',
                    'project_id': 'system-legacy',
                    'execution_key': 'z' * 32,
                    'fencing_token': '11',
                    'attempt': 1,
                    'parameter': 'output_path',
                    'kind': 'file',
                    'storage_backend': 's3',
                    'filename': 'atomic.json',
                    'storage_key': 'bio-agent/artifacts/system/job/atomic.json',
                }
                await database.reserve_job_artifacts([reservation])
                await database.mark_job_artifacts_uploaded([{
                    **reservation,
                    'content_type': 'application/json',
                    'size_bytes': 8,
                    'sha256': 'e' * 64,
                    'version_id': 'version-2',
                    'reference': 'bio+s3://bucket/atomic',
                }])
                durable = {
                    'schema': 'bioagent.execution-result.v1',
                    'result': {'status': 'ok'},
                    'artifacts': [{'publication_id': publication_id}],
                }
                await database.store_execution_result_with_artifacts(
                    'z' * 32,
                    'artifact-atomic-job',
                    durable,
                    [publication_id],
                    fencing_token='11',
                )
                execution = await database.get_execution_result('z' * 32)
                artifacts = await database.list_job_artifacts(
                    'artifact-atomic-job'
                )
                self.assertEqual(execution['status'], 'completed')
                self.assertEqual(artifacts[0]['status'], 'committed')

            try:
                asyncio.run(scenario())
            finally:
                asyncio.run(database.close())


if __name__ == '__main__':
    unittest.main()
