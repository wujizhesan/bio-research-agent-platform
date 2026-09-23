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
from sqlalchemy.exc import DBAPIError

from src.auth import Principal, roles_sha256
from src.database import (
    Database,
    database_worker_scope,
    set_database_principal,
)
from src.job_execution import InlineToolExecutor
from src.job_state_store import DatabaseDispatchSource, DatabaseStateWriter
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

    def test_postgres_rls_separates_api_tenants_and_worker(self):
        asyncio.run(self._postgres_rls_tenant_isolation())

    def test_postgres_signed_context_rejects_spoofed_gucs(self):
        asyncio.run(self._postgres_signed_context_rejects_spoofed_gucs())

    def test_postgres_auth_session_revocation_is_immediate(self):
        asyncio.run(self._postgres_auth_session_revocation_is_immediate())

    def test_postgres_artifact_recovery_claim_is_exclusive(self):
        if not os.environ.get('MAINTENANCE_DATABASE_URL'):
            self.skipTest('MAINTENANCE_DATABASE_URL is not configured')
        asyncio.run(self._postgres_artifact_recovery_claim_is_exclusive())

    def test_postgres_project_storage_reservation_prevents_overcommit(self):
        asyncio.run(self._postgres_project_storage_reservation_prevents_overcommit())

    def test_postgres_maintenance_registers_legacy_artifact_atomically(self):
        if not os.environ.get('MAINTENANCE_DATABASE_URL'):
            self.skipTest('MAINTENANCE_DATABASE_URL is not configured')
        asyncio.run(
            self._postgres_maintenance_registers_legacy_artifact_atomically()
        )

    def test_postgres_storage_deletion_audit_is_append_only(self):
        if not os.environ.get('MAINTENANCE_DATABASE_URL'):
            self.skipTest('MAINTENANCE_DATABASE_URL is not configured')
        asyncio.run(self._postgres_storage_deletion_audit_is_append_only())

    def test_postgres_global_audit_is_durable_and_append_only(self):
        if not os.environ.get('MAINTENANCE_DATABASE_URL'):
            self.skipTest('MAINTENANCE_DATABASE_URL is not configured')
        asyncio.run(self._postgres_global_audit_is_durable_and_append_only())

    async def _postgres_global_audit_is_durable_and_append_only(self):
        owner = Database(os.environ['DATABASE_URL'])
        api = Database(os.environ['API_DATABASE_URL'])
        maintenance = Database(os.environ['MAINTENANCE_DATABASE_URL'])
        event_id = uuid4().hex
        event = {
            'event_id': event_id,
            'at': '2026-09-21T00:00:00+00:00',
            'request_id': uuid4().hex,
            'trace_id': uuid4().hex,
            'actor': 'alice',
            'roles': ['researcher'],
            'action': 'file.downloaded',
            'resource_type': 'file',
            'resource_id': uuid4().hex,
            'metadata': {'project_id': 'audit-project'},
        }
        try:
            set_database_principal(Principal('alice', ('researcher',), 'jwt'))
            stored = await api.append_audit_event(event)
            self.assertEqual(stored['event_id'], event_id)
            set_database_principal()
            events = await maintenance.list_audit_events(actor='alice')
            selected = next(item for item in events if item['event_id'] == event_id)
            self.assertEqual(selected['action'], 'file.downloaded')
            with self.assertRaises(DBAPIError):
                async with maintenance.engine.begin() as connection:
                    await connection.execute(text(
                        'UPDATE audit_events SET action = :action '
                        'WHERE event_id = :event_id'
                    ), {'action': 'tampered', 'event_id': event_id})
        finally:
            set_database_principal()
            await api.close()
            await maintenance.close()
            async with owner.engine.begin() as connection:
                await connection.execute(text(
                    'DELETE FROM audit_events WHERE event_id = :event_id'
                ), {'event_id': event_id})
            await owner.close()

    async def _postgres_storage_deletion_audit_is_append_only(self):
        owner = Database(os.environ['DATABASE_URL'])
        api = Database(os.environ['API_DATABASE_URL'])
        maintenance = Database(os.environ['MAINTENANCE_DATABASE_URL'])
        suffix = uuid4().hex
        project_id = f'deletion-audit-{suffix}'
        file_id = uuid4().hex
        created_at = '2026-09-21T00:00:00+00:00'
        try:
            await owner.create_project(
                project_id,
                'Deletion audit integration',
                None,
                'alice',
                created_at,
            )
            set_database_principal(Principal('alice', ('researcher',), 'jwt'))
            await api.begin_file_upload(
                file_id,
                project_id,
                'audit.csv',
                's3',
                f'bio-agent/{file_id}/audit.csv',
                16,
                100,
                created_at,
            )
            await api.activate_file_upload(
                file_id,
                {
                    'filename': 'audit.csv',
                    'storage_key': f'bio-agent/{file_id}/audit.csv',
                    'version_id': 'version-audit',
                    'sha256': 'f' * 64,
                    'size_bytes': 12,
                },
                '2026-09-21T00:00:01+00:00',
            )
            await api.request_file_deletion(
                file_id,
                'alice',
                'delete-audit-request',
                '2026-09-21T00:00:02+00:00',
            )
            set_database_principal()
            claimed = await maintenance.claim_recoverable_files(
                'maintenance-delete',
                '1970-01-01T00:00:00+00:00',
            )
            selected = next(item for item in claimed if item['file_id'] == file_id)
            self.assertEqual(selected['status'], 'deleting')
            await maintenance.finish_file_recovery(
                file_id,
                'maintenance-delete',
                'deleted',
            )
            events = await maintenance.list_storage_deletion_events('file', file_id)
            self.assertEqual(
                [event['status'] for event in events],
                ['delete_requested', 'deleted'],
            )
            self.assertEqual(events[1]['previous_hash'], events[0]['event_hash'])
            with self.assertRaises(DBAPIError):
                async with maintenance.engine.begin() as connection:
                    await connection.execute(text(
                        'UPDATE storage_deletion_events SET status = :status '
                        'WHERE event_id = :event_id'
                    ), {
                        'status': 'tampered',
                        'event_id': events[0]['event_id'],
                    })
        finally:
            set_database_principal()
            await api.close()
            await maintenance.close()
            async with owner.engine.begin() as connection:
                await connection.execute(text(
                    'DELETE FROM storage_deletion_events '
                    'WHERE project_id = :project_id'
                ), {'project_id': project_id})
                await connection.execute(text(
                    'DELETE FROM file_records WHERE file_id = :file_id'
                ), {'file_id': file_id})
                await connection.execute(text(
                    'DELETE FROM projects WHERE project_id = :project_id'
                ), {'project_id': project_id})
            await owner.close()

    async def _postgres_maintenance_registers_legacy_artifact_atomically(self):
        owner = Database(os.environ['DATABASE_URL'])
        maintenance = Database(os.environ['MAINTENANCE_DATABASE_URL'])
        suffix = uuid4().hex
        project_id = f'legacy-artifact-{suffix}'
        job_id = f'legacy-artifact-job-{suffix}'
        publication_id = uuid4().hex + uuid4().hex
        record = {
            'publication_id': publication_id,
            'artifact_id': uuid4().hex,
            'job_id': job_id,
            'project_id': project_id,
            'execution_key': uuid4().hex,
            'fencing_token': 'legacy-backfill',
            'attempt': 1,
            'parameter': 'legacy_output_0',
            'kind': 'file',
            'storage_backend': 's3',
            'filename': 'result.json',
            'content_type': 'application/json',
            'size_bytes': 8,
            'sha256': 'a' * 64,
            'storage_key': f'bio-agent/artifacts/{project_id}/result.json',
            'version_id': 'version-1',
            'reference': 'bio+s3://research-results/result',
            'created_at': '2026-09-21T00:01:00+00:00',
        }
        try:
            await owner.create_project(
                project_id,
                'Legacy artifact backfill',
                None,
                'maintenance-test',
                '2026-09-21T00:00:00+00:00',
            )
            await owner.upsert_job({
                'job_id': job_id,
                'project_id': project_id,
                'tool': 'legacy_tool',
                'status': 'completed',
                'created_at': '2026-09-21T00:00:00+00:00',
                'finished_at': '2026-09-21T00:01:00+00:00',
                '_arguments': {},
                'artifacts': [record],
            })
            quarantined = await maintenance.quarantine_legacy_job_artifact(
                record,
                'checksum_mismatch: test',
            )
            self.assertEqual(quarantined['status'], 'quarantined')
            committed = await maintenance.register_legacy_job_artifact(
                record,
                quota_bytes=100,
            )
            repeated = await maintenance.register_legacy_job_artifact(
                record,
                quota_bytes=100,
            )
            usage = await maintenance.get_project_storage_usage(project_id)
            self.assertEqual(committed['status'], 'committed')
            self.assertEqual(repeated['status'], 'committed')
            self.assertEqual((usage['used_bytes'], usage['reserved_bytes']), (8, 0))
        finally:
            async with owner.engine.begin() as connection:
                await connection.execute(
                    text('DELETE FROM job_records WHERE job_id = :job_id'),
                    {'job_id': job_id},
                )
                await connection.execute(
                    text('DELETE FROM projects WHERE project_id = :project_id'),
                    {'project_id': project_id},
                )
            await maintenance.close()
            await owner.close()

    async def _postgres_project_storage_reservation_prevents_overcommit(self):
        owner = Database(os.environ['DATABASE_URL'])
        first = Database(os.environ['DATABASE_URL'])
        second = Database(os.environ['DATABASE_URL'])
        suffix = uuid4().hex
        project_id = f'storage-quota-{suffix}'
        file_ids = ('a' + suffix[:31], 'b' + suffix[:31])
        created_at = '2026-09-21T00:00:00+00:00'
        try:
            await owner.create_project(
                project_id,
                'Storage quota concurrency',
                None,
                'storage-test',
                created_at,
            )

            async def reserve(database, file_id):
                return await database.begin_file_upload(
                    file_id,
                    project_id,
                    f'{file_id}.csv',
                    's3',
                    f'bio-agent/{file_id}/input.csv',
                    60,
                    100,
                    created_at,
                )

            outcomes = await asyncio.gather(
                reserve(first, file_ids[0]),
                reserve(second, file_ids[1]),
                return_exceptions=True,
            )
            self.assertEqual(sum(isinstance(item, dict) for item in outcomes), 1)
            self.assertEqual(sum(isinstance(item, ValueError) for item in outcomes), 1)
            usage = await owner.get_project_storage_usage(project_id)
            self.assertEqual(usage['used_bytes'], 0)
            self.assertEqual(usage['reserved_bytes'], 60)
        finally:
            await first.close()
            await second.close()
            async with owner.engine.begin() as connection:
                await connection.execute(
                    text('DELETE FROM file_records WHERE project_id = :project_id'),
                    {'project_id': project_id},
                )
                await connection.execute(
                    text('DELETE FROM projects WHERE project_id = :project_id'),
                    {'project_id': project_id},
                )
            await owner.close()

    async def _postgres_artifact_recovery_claim_is_exclusive(self):
        owner = Database(os.environ['DATABASE_URL'])
        first = Database(os.environ['MAINTENANCE_DATABASE_URL'])
        second = Database(os.environ['MAINTENANCE_DATABASE_URL'])
        suffix = uuid4().hex
        project_id = f'artifact-maintenance-{suffix}'
        job_id = f'artifact-maintenance-job-{suffix}'
        publication_id = uuid4().hex + uuid4().hex
        file_id = uuid4().hex
        try:
            await owner.create_project(
                project_id,
                'Artifact maintenance',
                None,
                'maintenance-test',
                '2026-09-21T00:00:00+00:00',
            )
            await owner.upsert_job({
                'job_id': job_id,
                'project_id': project_id,
                'tool': 'research_catalog',
                'status': 'failed',
                'created_at': '2026-09-21T00:00:00+00:00',
                'finished_at': '2026-09-21T00:01:00+00:00',
                '_arguments': {},
                'error': 'worker lost',
            })
            reservation = {
                'publication_id': publication_id,
                'artifact_id': uuid4().hex,
                'job_id': job_id,
                'project_id': project_id,
                'execution_key': uuid4().hex,
                'fencing_token': '1',
                'attempt': 1,
                'parameter': 'output_path',
                'kind': 'file',
                'storage_backend': 's3',
                'filename': 'result.json',
                'storage_key': f'bio-agent/artifacts/{project_id}/result.json',
            }
            await owner.begin_execution_attempt(
                reservation['execution_key'],
                job_id,
                '1',
                1,
            )
            await owner.reserve_job_artifacts([reservation])
            await owner.mark_job_artifacts_uploaded([{
                **reservation,
                'content_type': 'application/json',
                'size_bytes': 8,
                'sha256': 'f' * 64,
                'version_id': 'version-1',
                'reference': 'bio+s3://bucket/result',
            }])
            await owner.orphan_job_artifacts([publication_id], 'worker lost')
            claimed = await asyncio.gather(
                first.claim_recoverable_job_artifacts(
                    'maintenance-a',
                    '9999-01-01T00:00:00+00:00',
                ),
                second.claim_recoverable_job_artifacts(
                    'maintenance-b',
                    '9999-01-01T00:00:00+00:00',
                ),
            )
            winners = [items for items in claimed if items]
            self.assertEqual(len(winners), 1)
            winner = winners[0][0]
            self.assertEqual(winner['status'], 'reclaiming')
            with self.assertRaises(RuntimeError):
                await owner.store_execution_result_with_artifacts(
                    reservation['execution_key'],
                    job_id,
                    {
                        'schema': 'bioagent.execution-result.v1',
                        'result': {'status': 'ok'},
                        'artifacts': [{'publication_id': publication_id}],
                    },
                    [publication_id],
                    fencing_token='1',
                )
            await first.finish_job_artifact_recovery(
                publication_id,
                winner['recovery_token'],
                'deleted',
            )
            await owner.begin_file_upload(
                file_id,
                project_id,
                'abandoned.csv',
                's3',
                f'bio-agent/{file_id}/abandoned.csv',
                20,
                100,
                '2026-09-21T00:02:00+00:00',
            )
            await owner.fail_file_upload(
                file_id,
                'upload interrupted',
                '2026-09-21T00:03:00+00:00',
            )
            file_claims = await asyncio.gather(
                first.claim_recoverable_files(
                    'file-maintenance-a',
                    '9999-01-01T00:00:00+00:00',
                ),
                second.claim_recoverable_files(
                    'file-maintenance-b',
                    '9999-01-01T00:00:00+00:00',
                ),
            )
            file_winners = [items for items in file_claims if items]
            self.assertEqual(len(file_winners), 1)
            claimed_file = file_winners[0][0]
            await first.finish_file_recovery(
                file_id,
                claimed_file['recovery_token'],
                'deleted',
            )
            usage = await owner.get_project_storage_usage(project_id)
            self.assertEqual((usage['used_bytes'], usage['reserved_bytes']), (0, 0))
            with self.assertRaises(DBAPIError):
                await first.create_project(
                    f'forbidden-{suffix}',
                    'Forbidden',
                    None,
                    'maintenance-test',
                    '2026-09-21T00:00:00+00:00',
                )
        finally:
            async with owner.engine.begin() as connection:
                await connection.execute(
                    text('DELETE FROM file_records WHERE file_id = :file_id'),
                    {'file_id': file_id},
                )
                await connection.execute(
                    text('DELETE FROM job_records WHERE job_id = :job_id'),
                    {'job_id': job_id},
                )
                await connection.execute(
                    text('DELETE FROM projects WHERE project_id = :project_id'),
                    {'project_id': project_id},
                )
            await first.close()
            await second.close()
            await owner.close()

    async def _postgres_signed_context_rejects_spoofed_gucs(self):
        import asyncpg

        owner = Database(os.environ['DATABASE_URL'])
        api = Database(os.environ['API_DATABASE_URL'])
        project_id = f'signed-context-{uuid4().hex}'
        created_at = '2026-09-20T00:00:00+00:00'
        try:
            await owner.create_project(
                project_id,
                'Signed tenant context',
                None,
                'alice',
                created_at,
            )
            set_database_principal(Principal('alice', ('researcher',), 'jwt'))
            self.assertIsNotNone(await api.get_project(project_id))
            raw_url = os.environ['API_DATABASE_URL'].replace(
                'postgresql+asyncpg://',
                'postgresql://',
            )
            connection = await asyncpg.connect(raw_url)
            try:
                async with connection.transaction():
                    await connection.execute(
                        "SELECT set_config('bioagent.subject', 'alice', true), "
                        "set_config('bioagent.is_admin', 'true', true)"
                    )
                    self.assertFalse(await connection.fetchval(
                        'SELECT bioagent_context_is_admin()'
                    ))
                    self.assertEqual(await connection.fetchval(
                        'SELECT bioagent_context_subject()'
                    ), '')
                    self.assertEqual(await connection.fetchval(
                        'SELECT count(*) FROM projects WHERE project_id = $1',
                        project_id,
                    ), 0)
            finally:
                await connection.close()
        finally:
            set_database_principal()
            await api.close()
            async with owner.engine.begin() as connection:
                await connection.execute(
                    text('DELETE FROM projects WHERE project_id = :project_id'),
                    {'project_id': project_id},
                )
            await owner.close()

    async def _postgres_auth_session_revocation_is_immediate(self):
        api = Database(os.environ['API_DATABASE_URL'])
        owner = Database(os.environ['DATABASE_URL'])
        role_hash = roles_sha256(('researcher',))
        session = None
        try:
            session = await api.create_auth_session('session-alice', role_hash, 3600)
            self.assertTrue(await api.validate_auth_session(
                session['jti'],
                'session-alice',
                session['token_version'],
                role_hash,
            ))
            set_database_principal(Principal(
                'session-alice',
                ('researcher',),
                'jwt',
                session['jti'],
                session['token_version'],
            ))
            self.assertTrue(await api.revoke_auth_session(session['jti']))
            self.assertFalse(await api.validate_auth_session(
                session['jti'],
                'session-alice',
                session['token_version'],
                role_hash,
            ))
        finally:
            set_database_principal()
            await api.close()
            if session is not None:
                async with owner.engine.begin() as connection:
                    await connection.execute(
                        text('DELETE FROM auth_subjects WHERE subject = :subject'),
                        {'subject': 'session-alice'},
                    )
            await owner.close()

    def test_worker_job_capability_scope_end_to_end(self):
        import redis

        client, namespace = self._real_redis()
        worker_client = redis.Redis.from_url(
            os.environ['REDIS_URL'], decode_responses=True
        )
        project_id = f'worker-scope-project-{uuid4().hex}'
        created_at = '2026-09-20T00:00:00+00:00'
        api_jobs = RedisJobManager(
            redis_client=client,
            namespace=namespace,
            tool_executor=InlineToolExecutor(
                lambda _tool, arguments: {'echo': arguments['value']}
            ),
        )
        writer = DatabaseStateWriter(
            os.environ['WORKER_DATABASE_URL'],
            require_job_scope=True,
        )
        dispatcher = DatabaseDispatchSource(os.environ['DISPATCHER_DATABASE_URL'])
        dispatcher_jobs = RedisJobManager(
            redis_client=client,
            namespace=namespace,
        )
        worker_jobs = RedisJobManager(
            redis_client=worker_client,
            namespace=namespace,
            state_store=writer,
            worker_id='scoped-integration-worker',
            tool_executor=InlineToolExecutor(
                lambda _tool, arguments: {'echo': arguments['value']}
            ),
        )
        job_id = None
        try:
            async def create_project():
                database = Database(os.environ['DATABASE_URL'])
                try:
                    await database.create_project(
                        project_id,
                        'Worker scope integration',
                        None,
                        'alice',
                        created_at,
                    )
                finally:
                    await database.close()

            asyncio.run(create_project())
            set_database_principal(Principal('alice', ('researcher',), 'jwt'))
            durable = api_jobs.prepare_durable(
                'research_catalog',
                {'value': 'scoped'},
                project_id=project_id,
            )
            self.assertIn('_execution_key', durable)
            job_id = durable['job_id']
            async def stage_job():
                database = Database(os.environ['API_DATABASE_URL'])
                try:
                    await database.stage_job(
                        durable,
                        project_id=project_id,
                        ownership_created_at=created_at,
                    )
                finally:
                    await database.close()

            asyncio.run(stage_job())
            with self.assertRaises(DBAPIError):
                writer.load_dispatchable()
            self.assertIn(
                job_id,
                {item['job_id'] for item in dispatcher.load_dispatchable()},
            )
            keys = list(client.scan_iter(f'{namespace}:*'))
            if keys:
                client.delete(*keys)
            claimed = dispatcher.claim_dispatchable(
                limit=10,
                lease_seconds=30,
                claim_ticket_ttl_seconds=300,
            )
            claimed_job = next(
                item for item in claimed if item['job_id'] == job_id
            )
            self.assertEqual(
                dispatcher_jobs.rebuild_durable_queue(
                    loader=lambda limit: [claimed_job]
                ),
                [job_id],
            )
            dispatcher.complete_claims([{
                'job_id': job_id,
                'generation': claimed_job['_dispatch_generation'],
                'succeeded': True,
            }])
            self.assertEqual(worker_jobs._next_job(), job_id)
            worker_jobs._complete_queued_item(job_id, 0.05)
            writer.flush()
            async def load_job():
                database = Database(os.environ['DATABASE_URL'])
                try:
                    return await database.get_job(job_id)
                finally:
                    await database.close()

            stored = asyncio.run(load_job())
            self.assertEqual(stored['status'], 'completed')
            self.assertEqual(stored['result'], {'echo': 'scoped'})
            self.assertEqual(
                stored['execution']['worker_id'],
                'scoped-integration-worker',
            )
        finally:
            set_database_principal()
            worker_jobs.shutdown()
            dispatcher_jobs.shutdown()
            api_jobs.shutdown()
            writer.close()
            if job_id is not None:
                async def cleanup():
                    database = Database(os.environ['DATABASE_URL'])
                    try:
                        async with database.engine.begin() as connection:
                            await connection.execute(
                                text('DELETE FROM job_records WHERE job_id = :job_id'),
                                {'job_id': job_id},
                            )
                            await connection.execute(
                                text('DELETE FROM projects WHERE project_id = :project_id'),
                                {'project_id': project_id},
                            )
                    finally:
                        await database.close()
                asyncio.run(cleanup())

    def test_postgres_idempotency_survives_redis_loss(self):
        asyncio.run(self._postgres_idempotency_survives_redis_loss())

    async def _postgres_idempotency_survives_redis_loss(self):
        owner = Database(os.environ['DATABASE_URL'])
        api = Database(os.environ['API_DATABASE_URL'])
        suffix = uuid4().hex
        project_id = f'idempotency-project-{suffix}'
        first_job = f'idempotency-first-{suffix}'
        duplicate_job = f'idempotency-duplicate-{suffix}'
        key = f'scoped:{uuid4().hex}{uuid4().hex}'
        created_at = '2026-09-20T00:00:00+00:00'
        try:
            await owner.create_project(
                project_id, 'Idempotency integration', None, 'alice', created_at
            )
            set_database_principal(Principal('alice', ('researcher',), 'jwt'))
            first = await api.stage_job({
                'job_id': first_job,
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': created_at,
                '_arguments': {'query': 'egfr'},
                '_execution_key': uuid4().hex,
            }, project_id=project_id, idempotency_subject='alice',
                idempotency_key=key, idempotency_payload_hash='a' * 64)
            duplicate = await api.stage_job({
                'job_id': duplicate_job,
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': created_at,
                '_arguments': {'query': 'egfr'},
                '_execution_key': uuid4().hex,
            }, project_id=project_id, idempotency_subject='alice',
                idempotency_key=key, idempotency_payload_hash='a' * 64)
            self.assertFalse(first['deduplicated'])
            self.assertEqual(duplicate, {
                'job_id': first_job,
                'deduplicated': True,
            })
            self.assertIsNone(await api.get_job(duplicate_job))
            with self.assertRaisesRegex(ValueError, 'different job payload'):
                await api.get_idempotent_job(
                    key, 'alice', project_id, 'b' * 64
                )
        finally:
            set_database_principal()
            await api.close()
            async with owner.engine.begin() as connection:
                await connection.execute(
                    text('DELETE FROM job_records WHERE job_id IN (:first, :duplicate)'),
                    {'first': first_job, 'duplicate': duplicate_job},
                )
                await connection.execute(
                    text('DELETE FROM projects WHERE project_id = :project_id'),
                    {'project_id': project_id},
                )
            await owner.close()

    async def _postgres_rls_tenant_isolation(self):
        owner = Database(os.environ['DATABASE_URL'])
        api = Database(os.environ['API_DATABASE_URL'])
        dispatcher = Database(os.environ['DISPATCHER_DATABASE_URL'])
        worker = Database(os.environ['WORKER_DATABASE_URL'])
        suffix = uuid4().hex
        alice_project = f'rls-alice-{suffix}'
        bob_project = f'rls-bob-{suffix}'
        alice_job = f'rls-alice-job-{suffix}'
        bob_job = f'rls-bob-job-{suffix}'
        alice_file = f'rls-alice-file-{suffix}'
        bob_file = f'rls-bob-file-{suffix}'
        alice_upload = uuid4().hex
        alice_capability = uuid4().hex
        bob_capability = uuid4().hex
        created_at = '2026-09-19T00:00:00+00:00'
        try:
            await owner.create_project(
                alice_project, 'Alice RLS', None, 'alice', created_at
            )
            await owner.create_project(
                bob_project, 'Bob RLS', None, 'bob', created_at
            )
            for job_id, project_id, capability in (
                (alice_job, alice_project, alice_capability),
                (bob_job, bob_project, bob_capability),
            ):
                await owner.stage_job({
                    'job_id': job_id,
                    'tool': 'research_catalog',
                    'status': 'queued',
                    'created_at': created_at,
                    '_arguments': {},
                    '_execution_key': capability,
                    '_revision': 1,
                }, project_id=project_id)
            await owner.assign_file_project(
                alice_file, alice_project, created_at, filename='alice.txt'
            )
            await owner.assign_file_project(
                bob_file, bob_project, created_at, filename='bob.txt'
            )

            set_database_principal(Principal('alice', ('researcher',), 'jwt'))
            self.assertIsNotNone(await api.get_project(alice_project))
            self.assertIsNone(await api.get_project(bob_project))
            self.assertEqual(
                {item['subject'] for item in await api.list_project_members(alice_project)},
                {'alice'},
            )
            self.assertEqual(await api.list_project_members(bob_project), [])
            await api.upsert_project_member(
                alice_project, 'charlie', 'viewer', created_at
            )
            self.assertEqual(
                {item['subject'] for item in await api.list_project_members(alice_project)},
                {'alice', 'charlie'},
            )
            with self.assertRaises(DBAPIError):
                await api.upsert_project_member(
                    bob_project, 'alice', 'viewer', created_at
                )
            with self.assertRaises(DBAPIError):
                await api.upsert_project_member(
                    alice_project, 'alice', 'viewer', created_at
                )
            self.assertTrue(await api.delete_project_member(
                alice_project, 'charlie'
            ))
            with self.assertRaisesRegex(ValueError, 'owner membership'):
                await api.delete_project_member(alice_project, 'alice')
            self.assertIsNotNone(await api.get_job(alice_job))
            self.assertIsNone(await api.get_job(bob_job))
            self.assertIsNotNone(await api.get_file_record(alice_file))
            self.assertIsNone(await api.get_file_record(bob_file))
            await api.begin_file_upload(
                alice_upload,
                alice_project,
                'alice-upload.csv',
                's3',
                f'bio-agent/{alice_upload}/alice-upload.csv',
                16,
                100,
                created_at,
            )
            self.assertEqual(
                (await api.get_project_storage_usage(alice_project))['reserved_bytes'],
                16,
            )
            with self.assertRaises(ValueError):
                await api.begin_file_upload(
                    uuid4().hex,
                    bob_project,
                    'forbidden.csv',
                    's3',
                    f'bio-agent/{uuid4().hex}/forbidden.csv',
                    16,
                    100,
                    created_at,
                )
            await api.discard_failed_file_upload(
                alice_upload,
                'test cleanup',
                created_at,
            )
            self.assertTrue(await api.list_job_events(alice_job))
            self.assertEqual(await api.list_job_events(bob_job), [])
            self.assertEqual(
                {item['job_id'] for item in await api.list_jobs_for_principal('alice')},
                {alice_job},
            )
            with self.assertRaises(DBAPIError):
                await api.upsert_job_with_project({
                    'job_id': bob_job,
                    'tool': 'research_catalog',
                    'status': 'running',
                    'created_at': created_at,
                    '_arguments': {},
                    '_revision': 2,
                }, bob_project)

            set_database_principal()
            with self.assertRaises(DBAPIError):
                await worker.get_project(alice_project)
            self.assertIsNone(await worker.get_job(alice_job))
            self.assertIsNone(await worker.get_job(bob_job))
            with self.assertRaises(DBAPIError):
                await worker.get_file_record(bob_file)
            self.assertEqual(await worker.list_job_events(bob_job), [])
            with self.assertRaises(DBAPIError):
                await worker.list_worker_dispatchable_jobs()
            self.assertEqual(
                {item['job_id'] for item in await dispatcher.list_dispatcher_jobs()},
                {alice_job, bob_job},
            )

            first_dispatch = next(
                item
                for item in await dispatcher.claim_dispatch_batch(
                    'dispatcher-rls',
                    limit=100,
                    lease_seconds=30,
                    claim_ticket_ttl_seconds=300,
                )
                if item['job_id'] == bob_job
            )
            first_claim = await worker.claim_worker_job(
                bob_job,
                bob_capability,
                'worker-bob',
                first_dispatch['_claim_ticket'],
                30,
            )
            self.assertIsNone(await worker.claim_worker_job(
                bob_job,
                bob_capability,
                'worker-bob-replay',
                first_dispatch['_claim_ticket'],
                30,
            ))
            running = {
                'job_id': bob_job,
                'project_id': bob_project,
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': created_at,
                '_arguments': {},
                '_execution_key': bob_capability,
                '_worker_id': 'worker-bob',
                '_fencing_token': first_claim['fencing_token'],
                '_attempts': first_claim['attempt'],
                '_revision': 2,
            }
            with database_worker_scope(
                bob_job,
                bob_capability,
                'worker-bob',
                first_claim['fencing_token'],
                first_claim['attempt'],
            ):
                await worker.upsert_worker_job(
                    running,
                    bob_capability,
                    'worker-bob',
                    first_claim['fencing_token'],
                    first_claim['attempt'],
                )
                self.assertEqual(
                    (await worker.get_job(bob_job))['status'],
                    'running',
                )
                self.assertIsNone(await worker.get_job(alice_job))
                publication_id = uuid4().hex + uuid4().hex
                reserved_artifacts = await worker.reserve_job_artifacts([{
                    'publication_id': publication_id,
                    'artifact_id': uuid4().hex,
                    'job_id': bob_job,
                    'project_id': bob_project,
                    'execution_key': bob_capability,
                    'fencing_token': first_claim['fencing_token'],
                    'attempt': first_claim['attempt'],
                    'parameter': 'output_path',
                    'kind': 'file',
                    'storage_backend': 's3',
                    'filename': 'result.json',
                    'storage_key': f'bio-agent/artifacts/{bob_project}/result.json',
                }])
                self.assertEqual(reserved_artifacts[0]['status'], 'reserved')
                await worker.mark_job_artifacts_uploaded([{
                    'publication_id': publication_id,
                    'content_type': 'application/json',
                    'size_bytes': 8,
                    'sha256': 'e' * 64,
                    'storage_key': f'bio-agent/artifacts/{bob_project}/result.json',
                    'version_id': 'version-1',
                    'reference': 'bio+s3://research-results/result',
                }])
                await worker.commit_job_artifacts([publication_id])
                self.assertEqual(
                    await worker.list_job_artifacts(alice_job),
                    [],
                )
                with self.assertRaises(DBAPIError):
                    await worker.reserve_job_artifacts([{
                        'publication_id': uuid4().hex + uuid4().hex,
                        'artifact_id': uuid4().hex,
                        'job_id': alice_job,
                        'project_id': alice_project,
                        'execution_key': alice_capability,
                        'fencing_token': first_claim['fencing_token'],
                        'attempt': first_claim['attempt'],
                        'parameter': 'output_path',
                        'kind': 'file',
                        'storage_backend': 's3',
                        'filename': 'result.json',
                        'storage_key': f'bio-agent/artifacts/{alice_project}/result.json',
                    }])

            set_database_principal(Principal('bob', ('researcher',), 'jwt'))
            visible_artifact = await api.get_job_artifact(
                bob_job,
                reserved_artifacts[0]['artifact_id'],
                statuses={'committed'},
            )
            self.assertEqual(visible_artifact['publication_id'], publication_id)
            await api.upsert_project_member(
                bob_project,
                'charlie',
                'editor',
                created_at,
            )
            set_database_principal(Principal('charlie', ('researcher',), 'jwt'))
            with self.assertRaisesRegex(ValueError, 'not found'):
                await api.request_job_artifact_deletion(
                    bob_job,
                    reserved_artifacts[0]['artifact_id'],
                    'charlie',
                    'editor-delete-request',
                    '2026-09-19T00:01:00+00:00',
                )
            set_database_principal(Principal('bob', ('researcher',), 'jwt'))
            deletion = await api.request_job_artifact_deletion(
                bob_job,
                reserved_artifacts[0]['artifact_id'],
                'bob',
                'owner-delete-request',
                '2026-09-19T00:02:00+00:00',
            )
            self.assertEqual(deletion['status'], 'delete_requested')
            deletion_events = await api.list_storage_deletion_events(
                'job_artifact',
                publication_id,
            )
            self.assertEqual(
                [event['status'] for event in deletion_events],
                ['delete_requested'],
            )
            set_database_principal(Principal('charlie', ('researcher',), 'jwt'))
            self.assertEqual(
                await api.list_storage_deletion_events(
                    'job_artifact',
                    publication_id,
                ),
                [],
            )
            set_database_principal(Principal('alice', ('researcher',), 'jwt'))
            self.assertIsNone(await api.get_job_artifact(
                bob_job,
                reserved_artifacts[0]['artifact_id'],
                statuses={'committed'},
            ))
            set_database_principal()

            async with owner.engine.begin() as connection:
                await connection.execute(
                    text(
                        'UPDATE job_records SET lease_until = 0 '
                        'WHERE job_id = :job_id'
                    ),
                    {'job_id': bob_job},
                )
            await dispatcher.complete_dispatch_claims(
                'dispatcher-rls',
                [{
                    'job_id': bob_job,
                    'generation': first_dispatch['_dispatch_generation'],
                    'succeeded': True,
                }],
                reconcile_seconds=0,
            )
            second_dispatch = next(
                item
                for item in await dispatcher.claim_dispatch_batch(
                    'dispatcher-rls-new',
                    limit=100,
                    lease_seconds=30,
                    claim_ticket_ttl_seconds=300,
                )
                if item['job_id'] == bob_job
            )
            second_claim = await worker.claim_worker_job(
                bob_job,
                bob_capability,
                'worker-bob-new',
                second_dispatch['_claim_ticket'],
                30,
            )
            takeover = {
                **running,
                '_worker_id': 'worker-bob-new',
                '_fencing_token': second_claim['fencing_token'],
                '_attempts': second_claim['attempt'],
                '_revision': 3,
            }
            with database_worker_scope(
                bob_job,
                bob_capability,
                'worker-bob-new',
                second_claim['fencing_token'],
                second_claim['attempt'],
            ):
                await worker.upsert_worker_job(
                    takeover,
                    bob_capability,
                    'worker-bob-new',
                    second_claim['fencing_token'],
                    second_claim['attempt'],
                )
            with database_worker_scope(
                bob_job,
                bob_capability,
                'worker-bob',
                first_claim['fencing_token'],
                first_claim['attempt'],
            ):
                with self.assertRaises(PermissionError):
                    await worker.upsert_worker_job(
                        running,
                        bob_capability,
                        'worker-bob',
                        first_claim['fencing_token'],
                        first_claim['attempt'],
                    )
            poisoned = {
                **takeover,
                '_worker_id': 'worker-poison',
                '_fencing_token': 'attacker-token',
                '_attempts': 2147483646,
                '_revision': 4,
            }
            with database_worker_scope(
                bob_job,
                bob_capability,
                'worker-poison',
                'attacker-token',
                2147483646,
            ):
                with self.assertRaises(PermissionError):
                    await worker.upsert_worker_job(
                        poisoned,
                        bob_capability,
                        'worker-poison',
                        'attacker-token',
                        2147483646,
                    )

            set_database_principal(Principal('root', ('admin',), 'jwt'))
            self.assertIsNotNone(await api.get_job(alice_job))
            self.assertIsNotNone(await api.get_job(bob_job))
        finally:
            set_database_principal()
            await api.close()
            await dispatcher.close()
            await worker.close()
            async with owner.engine.begin() as connection:
                await connection.execute(
                    text(
                        'DELETE FROM file_records WHERE file_id IN '
                        '(:alice, :bob, :upload)'
                    ),
                    {
                        'alice': alice_file,
                        'bob': bob_file,
                        'upload': alice_upload,
                    },
                )
                await connection.execute(
                    text('DELETE FROM job_records WHERE job_id IN (:alice, :bob)'),
                    {'alice': alice_job, 'bob': bob_job},
                )
                await connection.execute(
                    text('DELETE FROM projects WHERE project_id IN (:alice, :bob)'),
                    {'alice': alice_project, 'bob': bob_project},
                )
            await owner.close()

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
        project_id = f'ci-project-{uuid4().hex}'
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
            self.assertEqual(revision, '0026_durable_audit_events')
            self.assertIn('run_context', columns)
            self.assertTrue({
                'execution_identity',
                'routing',
                'execution',
                'resolution',
                'project_id',
            }.issubset(columns))
            self.assertIsNone(legacy_context)
            async with database.engine.connect() as connection:
                legacy_project = await connection.scalar(
                    text('SELECT project_id FROM job_records WHERE job_id = :job_id'),
                    {'job_id': os.environ['CI_LEGACY_JOB_ID']},
                )
                file_records_exists = await connection.scalar(text(
                    "SELECT to_regclass('public.file_records') IS NOT NULL"
                ))
                protected_rows = (await connection.execute(text(
                    "SELECT relname, relrowsecurity, relforcerowsecurity "
                    "FROM pg_class WHERE relname IN ("
                    "'job_records', 'file_records', 'job_projects', 'file_projects', "
                    "'job_events', 'job_outbox', 'job_execution_results', "
                    "'job_artifacts', 'projects', 'project_members', "
                    "'job_idempotency', 'project_storage_usage', "
                    "'storage_reservations', 'storage_deletion_events', "
                    "'audit_events')"
                ))).all()
                protected_tables = {
                    name: (row_security, force_row_security)
                    for name, row_security, force_row_security in protected_rows
                }
            self.assertEqual(legacy_project, 'system-legacy')
            self.assertTrue(file_records_exists)
            self.assertEqual(len(protected_tables), 15)
            self.assertTrue(all(enabled for enabled, _forced in protected_tables.values()))
            self.assertTrue(all(
                protected_tables[table][1]
                for table in (
                    'job_records', 'file_records', 'job_projects', 'file_projects',
                    'job_events', 'job_outbox', 'job_execution_results',
                    'job_artifacts', 'job_idempotency',
                    'project_storage_usage', 'storage_reservations',
                    'storage_deletion_events', 'audit_events',
                )
            ))

            run_context = {
                'schema_version': 1,
                'run_id': uuid4().hex,
                'trace_id': uuid4().hex,
                'tool': 'research_catalog',
                'domain': 'research',
            }
            execution_identity = {'fingerprint': uuid4().hex}
            await database.create_project(
                project_id,
                'CI project',
                None,
                'ci-user',
                '2026-09-07T00:00:00+00:00',
            )
            await database.stage_job({
                'job_id': job_id,
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-09-07T00:00:00+00:00',
                '_arguments': {},
                'run_context': run_context,
                'trace_id': run_context['trace_id'],
                'execution_identity': execution_identity,
                'routing': {'route_id': 'postgres-route'},
            }, project_id=project_id)
            stored = await database.get_job(job_id)
            self.assertEqual(stored['run_context'], run_context)
            self.assertEqual(stored['trace_id'], run_context['trace_id'])
            self.assertEqual(stored['execution_identity'], execution_identity)
            self.assertEqual(await database.get_job_project(job_id), project_id)
            await database.upsert_job({
                'job_id': job_id,
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': '2026-09-07T00:00:00+00:00',
                '_arguments': {},
                '_revision': 2,
            })
            self.assertEqual(
                (await database.get_job(job_id))['project_id'],
                project_id,
            )
            self.assertEqual(
                (await database.list_job_events(job_id))[-1]['job']['project_id'],
                project_id,
            )
            self.assertIn(
                job_id,
                {item['job_id'] for item in await database.list_dispatchable_jobs()},
            )
        finally:
            async with database.engine.begin() as connection:
                await connection.execute(
                    text('DELETE FROM job_records WHERE job_id = :job_id'),
                    {'job_id': job_id},
                )
                await connection.execute(
                    text('DELETE FROM projects WHERE project_id = :project_id'),
                    {'project_id': project_id},
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
