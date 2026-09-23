import asyncio
import gzip
import hashlib
from io import BytesIO
import json
import os
import shutil
import tempfile
import time
import unittest
from uuid import uuid4
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import src.fastapi_app as fastapi_module
from src.audit_log import AuditLogger
from src.auth import AuthService
from src.database import Database, JobOutboxRow
from src.fastapi_app import create_app
from src.file_storage import LocalFileStorage, StoredFile
from src.job_manager import JobManager
from src.plugin_manager import PluginManager
from src.settings import PlatformSettings
from src.storage_workspace import S3ObjectReference


class RedisReadJobManager:
    backend = 'redis'

    def __init__(self):
        self.read_count = 0

    def get(self, job_id):
        self.read_count += 1
        return {
            'job_id': job_id,
            'tool': 'research_catalog',
            'status': 'completed',
            'created_at': '2026-08-23T00:00:00+00:00',
            'result': {'status': 'ok'},
            'attempts': 1,
        }

    def shutdown(self):
        return None

    def list_workers(self):
        return [{
            'worker_id': 'worker-1',
            'draining': False,
            'active_jobs': 0,
        }]


class RedisEventPubSub:
    def __init__(self):
        self.closed = False

    def get_message(self, ignore_subscribe_messages=True, timeout=0):
        return {
            'type': 'message',
            'data': json.dumps({
                'job_id': 'redis-job',
                'tool': 'research_catalog',
                'status': 'completed',
                'created_at': '2026-08-23T00:00:00+00:00',
                'result': {'status': 'ok'},
                'attempts': 1,
            }),
        }

    def close(self):
        self.closed = True


class RedisEventJobManager(RedisReadJobManager):
    def __init__(self):
        super().__init__()
        self.pubsub = RedisEventPubSub()

    def get(self, job_id):
        self.read_count += 1
        return {
            'job_id': job_id,
            'tool': 'research_catalog',
            'status': 'running',
            'created_at': '2026-08-23T00:00:00+00:00',
            'attempts': 1,
        }

    def subscribe_job_events(self, job_id):
        return self.pubsub


class RedisReplayJobManager(RedisReadJobManager):
    def __init__(self):
        super().__init__()
        self.cursors = []

    def get(self, job_id):
        self.read_count += 1
        return {
            'job_id': job_id,
            'tool': 'research_catalog',
            'status': 'running',
            'created_at': '2026-08-23T00:00:00+00:00',
            'attempts': 1,
        }

    def read_job_events(self, job_id, last_event_id, block_ms, count):
        self.cursors.append(last_event_id)
        return [('2-0', {
            'job_id': job_id,
            'tool': 'research_catalog',
            'status': 'completed',
            'created_at': '2026-08-23T00:00:00+00:00',
            'result': {'status': 'ok'},
            'attempts': 1,
        })]


class RedisLostEventJobManager(RedisReadJobManager):
    def get(self, _job_id):
        self.read_count += 1
        return None

    def read_job_events(self, *_args, **_kwargs):
        return []


class RedisLostCancelJobManager(RedisLostEventJobManager):
    def cancel(self, job_id):
        raise ValueError(f'job not found: {job_id}')


class RedisRevocationJobManager(RedisReadJobManager):
    def get(self, job_id):
        self.read_count += 1
        return {
            'job_id': job_id,
            'project_id': 'revocation-project',
            'tool': 'research_catalog',
            'status': 'running',
            'created_at': '2026-09-20T00:00:00+00:00',
            'attempts': 1,
        }

    def read_job_events(self, *_args, **_kwargs):
        time.sleep(0.05)
        return []


class RedisDurableSubmitJobManager(RedisReadJobManager):
    def __init__(self):
        super().__init__()
        self.order = []
        self.record = None

    def prepare(self, tool, arguments, **_kwargs):
        self.order.append('prepare')
        self.record = {
            'job_id': 'durable-submit-job',
            'tool': tool,
            'status': 'queued',
            'created_at': '2026-09-15T00:00:00+00:00',
            '_arguments': dict(arguments),
            '_attempts': 0,
            '_cancel_requested': False,
            'resources': {},
            'priority': 0,
        }
        return {
            key: value
            for key, value in self.record.items()
            if not key.startswith('_')
        }

    def durable_record(self, job_id):
        return dict(self.record) if self.record and self.record['job_id'] == job_id else None

    def dispatch(self, job_id):
        self.order.append('dispatch')
        return self.get(job_id)

    def get(self, job_id):
        if self.record is None or self.record['job_id'] != job_id:
            return None
        return {
            key: value
            for key, value in self.record.items()
            if not key.startswith('_')
        }


class FastApiAppTests(unittest.TestCase):
    def _app(self, root, file_storage=None, audit_log=None, settings=None):
        app = create_app(
            job_manager=JobManager(max_workers=1, store_path=Path(root) / 'jobs.sqlite3'),
            plugin_manager=PluginManager(state_path=Path(root) / 'plugins.json'),
            database=Database(f"sqlite+aiosqlite:///{(Path(root) / 'api.sqlite3').as_posix()}"),
            file_storage=file_storage,
            audit_log=audit_log or AuditLogger(Path(root) / 'audit.jsonl'),
            settings=settings,
        )
        return app

    def _close_app(self, app):
        app.state.job_manager.shutdown()
        asyncio.run(app.state.database.close())

    def _wait_for_job(self, client, job_id):
        for _ in range(200):
            response = client.get(f'/api/v1/jobs/{job_id}')
            self.assertEqual(response.status_code, 200)
            record = response.json()['job']
            if record['status'] in {'completed', 'failed', 'cancelled'}:
                return record
            time.sleep(0.05)
        self.fail(f'job did not reach a terminal state: {job_id}')

    def _persist_artifact(self, app, record, artifact, status='committed'):
        publication_id = hashlib.sha256(
            f"{record['job_id']}:{artifact['artifact_id']}".encode('utf-8')
        ).hexdigest()
        reservation = {
            **artifact,
            'publication_id': publication_id,
            'job_id': record['job_id'],
            'project_id': record.get('project_id') or 'system-legacy',
            'execution_key': record.get('_execution_key') or uuid4().hex,
            'fencing_token': str(record.get('_fencing_token') or 'local-test'),
            'attempt': max(int(record.get('_attempts', 1) or 1), 1),
            'parameter': 'output_path',
            'kind': 'file',
            'reserved_bytes': max(int(artifact['size_bytes']), 1),
        }

        async def persist():
            await app.state.database.upsert_job(record)
            await app.state.database.reserve_job_artifacts([reservation])
            if status == 'reserved':
                return
            await app.state.database.mark_job_artifacts_uploaded([reservation])
            if status == 'uploaded':
                return
            await app.state.database.commit_job_artifacts([publication_id])

        asyncio.run(persist())

    def test_health_openapi_and_database(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_app_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    health = client.get('/health', headers={
                        'X-Request-ID': 'interview-trace-001',
                        'traceparent': '00-0123456789abcdef0123456789abcdef-0123456789abcdef-01',
                    })
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(health.json()['database'], 'ok')
                    self.assertEqual(health.json()['dependencies']['job_backend'], 'ok')
                    self.assertEqual(health.json()['dependencies']['storage'], 'ok')
                    self.assertEqual(health.json()['storage_backend'], 'local')
                    self.assertEqual(health.json()['configuration']['schema_version'], 1)
                    self.assertEqual(len(health.json()['configuration']['fingerprint']), 64)
                    self.assertEqual(health.headers['x-request-id'], 'interview-trace-001')
                    self.assertEqual(
                        health.headers['x-trace-id'],
                        '0123456789abcdef0123456789abcdef',
                    )
                    self.assertEqual(
                        health.json()['observability']['trace_id'],
                        '0123456789abcdef0123456789abcdef',
                    )
                    self.assertIn('/api/v1/jobs', client.get('/openapi.json').json()['paths'])
                    self.assertIn('bio_agent_http_requests_total', client.get('/metrics').text)
                    client.get('/missing/high-cardinality-value')
                    metrics = client.get('/metrics').text
                    self.assertIn('/_unmatched', metrics)
                    self.assertNotIn('/missing/high-cardinality-value', metrics)
                    self.assertIn('bio_agent_storage_deletion_backlog', metrics)
                    self.assertIn(
                        'bio_agent_storage_deletion_oldest_age_seconds',
                        metrics,
                    )
                    self.assertIn('bio_agent_storage_deletion_event_count', metrics)
                    submitted = client.post(
                        '/api/v1/jobs',
                        json={'tool': 'research_catalog', 'arguments': {}},
                        headers={
                            'X-Request-ID': 'job-request-001',
                            'X-Trace-ID': 'job-trace-001',
                        },
                    )
                    self.assertEqual(submitted.status_code, 202)
                    job = submitted.json()['job']
                    self.assertEqual(job['trace_id'], 'job-trace-001')
                    self.assertEqual(job['request_id'], 'job-request-001')
                    completed = self._wait_for_job(client, job['job_id'])
                    self.assertEqual(completed['trace_id'], 'job-trace-001')
            finally:
                self._close_app(app)

    def test_redis_submission_commits_outbox_without_api_dispatch(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_outbox_') as raw:
            manager = RedisDurableSubmitJobManager()
            database = Database(
                f"sqlite+aiosqlite:///{(Path(raw) / 'api.sqlite3').as_posix()}"
            )
            original_stage = database.stage_job

            async def stage(record, **kwargs):
                manager.order.append('stage')
                await original_stage(record, **kwargs)

            database.stage_job = stage
            app = create_app(
                job_manager=manager,
                plugin_manager=PluginManager(
                    state_path=Path(raw) / 'plugins.json'
                ),
                database=database,
                audit_log=AuditLogger(Path(raw) / 'audit.jsonl'),
            )
            try:
                with TestClient(app) as client:
                    project = client.post('/api/v1/projects', json={
                        'name': 'Durable project',
                    }).json()['project']
                    response = client.post('/api/v1/jobs', json={
                        'tool': 'research_catalog',
                        'arguments': {},
                        'project_id': project['project_id'],
                    })
                self.assertEqual(response.status_code, 202)
                self.assertEqual(manager.order, ['prepare', 'stage'])
                dispatchable = asyncio.run(database.list_dispatchable_jobs())
                self.assertEqual(
                    [item['job_id'] for item in dispatchable],
                    ['durable-submit-job'],
                )
                self.assertEqual(
                    asyncio.run(database.get_job_project('durable-submit-job')),
                    project['project_id'],
                )
            finally:
                self._close_app(app)

    def test_live_and_ready_verify_deployment_identity(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_readiness_') as raw, patch.dict(
            os.environ,
            {
                'APP_RELEASE_TAG': 'v0.2.0-rc.1',
                'APP_GIT_SHA': 'a' * 40,
                'APP_IMAGE_REFERENCE': 'backend@sha256:' + 'b' * 64,
            },
        ):
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    live = client.get('/live')
                    ready = client.get(
                        '/ready',
                        headers={
                            'X-Expected-Release': 'v0.2.0-rc.1',
                            'X-Expected-Commit': 'a' * 40,
                        },
                    )
                    mismatch = client.get(
                        '/ready',
                        headers={'X-Expected-Release': 'v0.1.0'},
                    )
                self.assertEqual(live.status_code, 200)
                self.assertEqual(live.json()['deployment']['git_sha'], 'a' * 40)
                self.assertEqual(ready.status_code, 200)
                self.assertEqual(ready.json()['deployment']['release_tag'], 'v0.2.0-rc.1')
                self.assertEqual(mismatch.status_code, 409)
                self.assertEqual(mismatch.json()['status'], 'version_mismatch')
            finally:
                self._close_app(app)

    def test_readiness_failure_is_sanitized(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_readiness_failure_') as raw:
            app = self._app(raw)
            app.state.database.ping = AsyncMock(
                side_effect=RuntimeError('postgresql://user:secret@database/internal')
            )
            try:
                with TestClient(app) as client:
                    response = client.get('/ready')
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()['dependencies']['database'], 'unavailable')
                self.assertNotIn('secret', response.text)
                self.assertNotIn('database/internal', response.text)
            finally:
                self._close_app(app)

    def test_frontend_error_is_correlated_and_logged(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_frontend_error_') as raw:
            app = self._app(raw)
            captured = []

            def capture(event, *args, **fields):
                if event == 'frontend.error.captured':
                    captured.append((fields, fastapi_module.current_context()))

            try:
                with TestClient(app) as client, patch.object(
                    fastapi_module, 'log_event', side_effect=capture,
                ):
                    response = client.post(
                        '/api/v1/telemetry/frontend-errors',
                        headers={'X-Trace-ID': 'frontend-trace-1'},
                        json={
                            'boundary_name': 'job-result',
                            'error_name': 'TypeError',
                            'message': 'render failed',
                            'component_stack': 'at JobResultSummary',
                            'trace_id': 'frontend-trace-1',
                            'job_id': 'job-1',
                            'plugin_id': 'sequence',
                            'path': '/workspace',
                            'occurred_at': '2026-09-07T00:00:00Z',
                        },
                    )
                self.assertEqual(response.status_code, 202)
                self.assertEqual(response.json()['trace_id'], 'frontend-trace-1')
                self.assertEqual(len(captured), 1)
                fields, context = captured[0]
                self.assertEqual(fields['boundary_name'], 'job-result')
                self.assertEqual(fields['error_name'], 'TypeError')
                self.assertEqual(context['trace_id'], 'frontend-trace-1')
                self.assertEqual(context['job_id'], 'job-1')
                self.assertEqual(context['plugin'], 'sequence')
            finally:
                self._close_app(app)

    def test_project_workspace_and_members(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_projects_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    created = client.post('/api/v1/projects', json={
                        'name': 'EGFR research',
                        'description': 'Shared workspace',
                    })
                    self.assertEqual(created.status_code, 201)
                    project = created.json()['project']
                    project_id = project['project_id']
                    self.assertEqual(project['owner_subject'], 'local-dev')

                    listed = client.get('/api/v1/projects')
                    self.assertEqual(listed.status_code, 200)
                    self.assertEqual(listed.json()['projects'][0]['project_id'], project_id)

                    member = client.post(f'/api/v1/projects/{project_id}/members', json={
                        'subject': 'alice',
                        'role': 'editor',
                    })
                    self.assertEqual(member.status_code, 201)
                    self.assertEqual(member.json()['member']['role'], 'editor')

                    members = client.get(f'/api/v1/projects/{project_id}/members')
                    self.assertEqual(members.status_code, 200)
                    self.assertEqual({item['subject'] for item in members.json()['members']}, {'local-dev', 'alice'})

                    uploaded = client.post(
                        '/api/v1/files',
                        data={'project_id': project_id},
                        files={'upload': ('notes.txt', b'project notes', 'text/plain')},
                    )
                    self.assertEqual(uploaded.status_code, 201)
                    self.assertEqual(uploaded.json()['file']['project_id'], project_id)

                    submitted = client.post('/api/v1/jobs', json={
                        'tool': 'research_catalog',
                        'arguments': {},
                        'project_id': project_id,
                    })
                    self.assertEqual(submitted.status_code, 202)
                    job_id = submitted.json()['job']['job_id']
                    self.assertEqual(submitted.json()['job']['project_id'], project_id)
                    completed = self._wait_for_job(client, job_id)
                    self.assertEqual(completed['project_id'], project_id)
                    filtered = client.get(f'/api/v1/jobs?project_id={project_id}')
                    self.assertEqual(filtered.status_code, 200)
                    self.assertIn(job_id, {item['job_id'] for item in filtered.json()['jobs']})
            finally:
                self._close_app(app)

    def test_capabilities_expose_rest_sse_mcp_and_embedded_surfaces(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_capabilities_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    response = client.get('/api/v1/capabilities')
                    self.assertEqual(response.status_code, 200)
                    payload = response.json()
                    self.assertGreater(payload['tool_count'], 0)
                    self.assertEqual(
                        set(payload['interfaces']),
                        {'rest', 'sse', 'mcp', 'embedded', 'a2a'},
                    )
                    self.assertEqual(payload['interfaces']['rest']['openapi'], '/openapi.json')
                    self.assertEqual(payload['interfaces']['sse']['status'], 'available')
                    self.assertIn(payload['interfaces']['mcp']['status'], {'available', 'dependency_missing'})
                    self.assertEqual(payload['interfaces']['embedded']['status'], 'available')
                    self.assertEqual(payload['interfaces']['a2a']['endpoint'], '/a2a')
                    self.assertIn('/api/v1/capabilities', client.get('/openapi.json').json()['paths'])
            finally:
                self._close_app(app)

    def test_a2a_agent_card_and_jsonrpc_task_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_a2a_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    card = client.get('/.well-known/agent-card.json')
                    self.assertEqual(card.status_code, 200)
                    self.assertEqual(card.json()['protocolVersion'], '0.3.0')
                    self.assertTrue(card.json()['url'].endswith('/a2a'))
                    self.assertEqual(card.json()['capabilities']['streaming'], True)

                    sent = client.post('/a2a', json={
                        'jsonrpc': '2.0',
                        'id': 'send-1',
                        'method': 'message/send',
                        'params': {
                            'message': {
                                'role': 'user',
                                'messageId': 'a2a-message-1',
                                'parts': [{'kind': 'text', 'text': 'list available research capabilities'}],
                                'metadata': {'tool': 'research_catalog', 'arguments': {}},
                            },
                        },
                    })
                    self.assertEqual(sent.status_code, 200)
                    task = sent.json()['result']['task']
                    self.assertEqual(task['kind'], 'task')
                    self.assertEqual(task['id'], task['metadata']['bio.job_id'])

                    final = task
                    for _ in range(100):
                        queried = client.post('/a2a', json={
                            'jsonrpc': '2.0',
                            'id': 'get-1',
                            'method': 'tasks/get',
                            'params': {'id': task['id']},
                        })
                        final = queried.json()['result']['task']
                        if final['status']['state'] in {'completed', 'failed', 'canceled'}:
                            break
                        time.sleep(0.05)
                    self.assertEqual(final['status']['state'], 'completed')
                    self.assertIn('artifacts', final)

                    with client.stream('POST', '/a2a', json={
                        'jsonrpc': '2.0',
                        'id': 'stream-1',
                        'method': 'message/stream',
                        'params': {
                            'message': {
                                'role': 'user',
                                'messageId': 'a2a-stream-message-1',
                                'parts': [{'kind': 'text', 'text': 'stream research capabilities'}],
                                'metadata': {'tool': 'research_catalog', 'arguments': {}},
                            },
                        },
                    }) as events:
                        body = ''.join(events.iter_text())
                        self.assertEqual(events.status_code, 200)
                        self.assertEqual(events.headers['content-type'].split(';', 1)[0], 'text/event-stream')
                    self.assertIn('"kind": "status-update"', body)
                    self.assertIn('"final": true', body)
                    self.assertIn('"kind": "artifact-update"', body)

                    unknown = client.post('/a2a', json={
                        'jsonrpc': '2.0',
                        'id': 'unknown-1',
                        'method': 'unknown/method',
                        'params': {},
                    })
                    self.assertEqual(unknown.json()['error']['code'], -32601)
            finally:
                self._close_app(app)

    def test_a2a_task_access_respects_project_membership(self):
        users = {
            'alice': {'password': 'alice-secret', 'roles': ['researcher']},
            'bob': {'password': 'bob-secret', 'roles': ['researcher']},
        }
        with tempfile.TemporaryDirectory(prefix='fastapi_a2a_tenant_') as raw:
            with patch.dict(os.environ, {
                'CADD_API_TOKEN': '',
                'CADD_JWT_SECRET': 'test-secret-' * 4,
                'CADD_AUTH_USERS_JSON': json.dumps(users),
            }, clear=False):
                app = self._app(raw)
                try:
                    with TestClient(app) as client:
                        headers = {}
                        for username in users:
                            response = client.post('/api/v1/auth/token', data={
                                'username': username,
                                'password': f'{username}-secret',
                            })
                            headers[username] = {
                                'Authorization': f"Bearer {response.json()['access_token']}"
                            }
                        project = client.post(
                            '/api/v1/projects',
                            headers=headers['alice'],
                            json={'name': 'Alice project'},
                        ).json()['project']
                        submitted = client.post(
                            '/api/v1/jobs',
                            headers=headers['alice'],
                            json={
                                'tool': 'research_catalog',
                                'arguments': {},
                                'project_id': project['project_id'],
                            },
                        )
                        job_id = submitted.json()['job']['job_id']
                        for method in ('tasks/get', 'tasks/cancel'):
                            denied = client.post(
                                '/a2a',
                                headers=headers['bob'],
                                json={
                                    'jsonrpc': '2.0',
                                    'id': method,
                                    'method': method,
                                    'params': {'id': job_id},
                                },
                            )
                            self.assertEqual(denied.json()['error']['code'], -32003)
                finally:
                    self._close_app(app)

    def test_project_owner_can_revoke_member_but_not_owner(self):
        users = {
            'alice': {'password': 'alice-secret', 'roles': ['researcher']},
            'bob': {'password': 'bob-secret', 'roles': ['researcher']},
        }
        with tempfile.TemporaryDirectory(prefix='fastapi_member_revoke_') as raw:
            with patch.dict(os.environ, {
                'CADD_API_TOKEN': '',
                'CADD_JWT_SECRET': 'test-secret-' * 4,
                'CADD_AUTH_USERS_JSON': json.dumps(users),
            }, clear=False):
                app = self._app(raw)
                try:
                    with TestClient(app) as client:
                        headers = {}
                        for username in users:
                            response = client.post('/api/v1/auth/token', data={
                                'username': username,
                                'password': f'{username}-secret',
                            })
                            headers[username] = {
                                'Authorization': f"Bearer {response.json()['access_token']}"
                            }
                        project = client.post(
                            '/api/v1/projects',
                            headers=headers['alice'],
                            json={'name': 'Revocation project'},
                        ).json()['project']
                        project_id = project['project_id']
                        added = client.post(
                            f'/api/v1/projects/{project_id}/members',
                            headers=headers['alice'],
                            json={'subject': 'bob', 'role': 'viewer'},
                        )
                        self.assertEqual(added.status_code, 201)
                        self.assertEqual(
                            client.get(
                                f'/api/v1/projects/{project_id}',
                                headers=headers['bob'],
                            ).status_code,
                            200,
                        )
                        removed = client.delete(
                            f'/api/v1/projects/{project_id}/members/bob',
                            headers=headers['alice'],
                        )
                        self.assertEqual(removed.status_code, 204)
                        self.assertEqual(
                            client.get(
                                f'/api/v1/projects/{project_id}',
                                headers=headers['bob'],
                            ).status_code,
                            403,
                        )
                        self.assertEqual(
                            client.delete(
                                f'/api/v1/projects/{project_id}/members/alice',
                                headers=headers['alice'],
                            ).status_code,
                            400,
                        )
                        self.assertEqual(
                            client.post(
                                f'/api/v1/projects/{project_id}/members',
                                headers=headers['alice'],
                                json={'subject': 'alice', 'role': 'viewer'},
                            ).status_code,
                            400,
                        )
                finally:
                    self._close_app(app)

    def test_redis_job_reads_use_redis_before_database(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_redis_read_') as raw:
            manager = RedisReadJobManager()
            app = create_app(
                job_manager=manager,
                plugin_manager=PluginManager(state_path=Path(raw) / 'plugins.json'),
                database=Database(f"sqlite+aiosqlite:///{(Path(raw) / 'api.sqlite3').as_posix()}"),
                audit_log=AuditLogger(Path(raw) / 'audit.jsonl'),
            )
            try:
                with TestClient(app) as client:
                    response = client.get('/api/v1/jobs/redis-job')
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()['job']['status'], 'completed')
                    self.assertEqual(manager.read_count, 1)
                    workers = client.get('/api/v1/workers')
                    self.assertEqual(workers.status_code, 200)
                    self.assertEqual(
                        workers.json()['workers'][0]['worker_id'],
                        'worker-1',
                    )
            finally:
                self._close_app(app)

    def test_redis_sse_uses_pubsub_and_closes_subscription(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_redis_sse_') as raw:
            manager = RedisEventJobManager()
            app = create_app(
                job_manager=manager,
                plugin_manager=PluginManager(state_path=Path(raw) / 'plugins.json'),
                database=Database(f"sqlite+aiosqlite:///{(Path(raw) / 'api.sqlite3').as_posix()}"),
                audit_log=AuditLogger(Path(raw) / 'audit.jsonl'),
            )
            try:
                with TestClient(app) as client:
                    with client.stream('GET', '/api/v1/jobs/redis-job/events') as events:
                        body = ''.join(events.iter_text())
                    self.assertEqual(events.status_code, 200)
                    self.assertIn('"status": "completed"', body)
                    self.assertTrue(manager.pubsub.closed)
            finally:
                self._close_app(app)

    def test_redis_sse_replays_from_last_event_id(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_redis_replay_') as raw:
            manager = RedisReplayJobManager()
            app = create_app(
                job_manager=manager,
                plugin_manager=PluginManager(state_path=Path(raw) / 'plugins.json'),
                database=Database(f"sqlite+aiosqlite:///{(Path(raw) / 'api.sqlite3').as_posix()}"),
                audit_log=AuditLogger(Path(raw) / 'audit.jsonl'),
            )
            try:
                with TestClient(app) as client:
                    with client.stream(
                        'GET',
                        '/api/v1/jobs/redis-job/events',
                        headers={'Last-Event-ID': '1-0'},
                    ) as events:
                        body = ''.join(events.iter_text())
                    self.assertEqual(events.status_code, 200)
                    self.assertIn('id: 2-0', body)
                    self.assertIn('"status": "completed"', body)
                    self.assertEqual(manager.cursors, ['1-0'])
            finally:
                self._close_app(app)

    def test_redis_sse_falls_back_to_durable_terminal_event(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_durable_sse_') as raw:
            manager = RedisLostEventJobManager()
            database = Database(
                f"sqlite+aiosqlite:///{(Path(raw) / 'api.sqlite3').as_posix()}"
            )
            asyncio.run(database.init_schema())
            asyncio.run(database.upsert_job({
                'job_id': 'durable-event-job',
                'tool': 'research_catalog',
                'status': 'completed',
                'created_at': '2026-09-16T00:00:00+00:00',
                'finished_at': '2026-09-16T00:00:01+00:00',
                'result': {'status': 'ok'},
                '_revision': 4,
                '_event_id': '4000-0',
            }))
            app = create_app(
                job_manager=manager,
                plugin_manager=PluginManager(state_path=Path(raw) / 'plugins.json'),
                database=database,
                audit_log=AuditLogger(Path(raw) / 'audit.jsonl'),
            )
            try:
                with TestClient(app) as client:
                    with client.stream(
                        'GET',
                        '/api/v1/jobs/durable-event-job/events',
                        headers={'Last-Event-ID': '3999-0'},
                    ) as events:
                        body = ''.join(events.iter_text())
                    self.assertEqual(events.status_code, 200)
                    self.assertIn('id: r-4', body)
                    self.assertIn('"status": "completed"', body)
                    self.assertIn('"replay_gap": true', body)
            finally:
                self._close_app(app)

    def test_redis_sse_replays_running_job_after_stream_loss(self):
        class RedisDisconnectedJobManager(RedisLostEventJobManager):
            def read_job_events(self, *_args, **_kwargs):
                raise ConnectionError('Redis unavailable')

        with tempfile.TemporaryDirectory(prefix='fastapi_running_sse_') as raw:
            manager = RedisDisconnectedJobManager()
            database = Database(
                f"sqlite+aiosqlite:///{(Path(raw) / 'api.sqlite3').as_posix()}"
            )
            asyncio.run(database.init_schema())
            asyncio.run(database.upsert_jobs([{
                'job_id': 'running-event-job',
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-09-23T00:00:00+00:00',
                '_revision': 1,
                '_event_id': '1001-0',
            }, {
                'job_id': 'running-event-job',
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': '2026-09-23T00:00:00+00:00',
                '_revision': 2,
                '_event_id': '1002-0',
            }]))
            app = create_app(
                job_manager=manager,
                plugin_manager=PluginManager(state_path=Path(raw) / 'plugins.json'),
                database=database,
                audit_log=AuditLogger(Path(raw) / 'audit.jsonl'),
            )
            try:
                with TestClient(app) as client:
                    with client.stream(
                        'GET',
                        '/api/v1/jobs/running-event-job/events?timeout_seconds=1&last_event_id=r-1',
                    ) as events:
                        body = ''.join(events.iter_text())
                    self.assertEqual(events.status_code, 200)
                    self.assertIn('id: r-2', body)
                    self.assertIn('"status": "running"', body)
                    self.assertNotIn('"replay_gap": true', body)
                    with client.stream(
                        'GET',
                        '/api/v1/jobs/running-event-job/events?timeout_seconds=1',
                        headers={'Last-Event-ID': '9999-0'},
                    ) as events:
                        legacy_body = ''.join(events.iter_text())
                    self.assertEqual(events.status_code, 200)
                    self.assertIn('id: r-2', legacy_body)
                    self.assertIn('"replay_gap": true', legacy_body)
            finally:
                self._close_app(app)

    def test_sse_closes_when_project_membership_is_revoked(self):
        users = {
            'alice': {'password': 'alice-secret', 'roles': ['researcher']},
            'bob': {'password': 'bob-secret', 'roles': ['researcher']},
        }
        with tempfile.TemporaryDirectory(prefix='fastapi_sse_revoke_') as raw:
            database = Database(
                f"sqlite+aiosqlite:///{(Path(raw) / 'api.sqlite3').as_posix()}"
            )
            asyncio.run(database.init_schema())
            asyncio.run(database.create_project(
                'revocation-project',
                'Revocation project',
                None,
                'alice',
                '2026-09-20T00:00:00+00:00',
            ))
            asyncio.run(database.upsert_project_member(
                'revocation-project',
                'bob',
                'viewer',
                '2026-09-20T00:00:00+00:00',
            ))
            asyncio.run(database.stage_job({
                'job_id': 'revocation-job',
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': '2026-09-20T00:00:00+00:00',
                '_arguments': {},
            }, project_id='revocation-project'))
            original_get_member = database.get_project_member
            membership_checks = 0

            async def get_member_then_revoke(project_id, subject):
                nonlocal membership_checks
                membership_checks += 1
                if membership_checks > 1:
                    return None
                return await original_get_member(project_id, subject)

            database.get_project_member = get_member_then_revoke
            with patch.dict(os.environ, {
                'CADD_API_TOKEN': '',
                'CADD_JWT_SECRET': 'test-secret-' * 4,
                'CADD_AUTH_USERS_JSON': json.dumps(users),
            }, clear=False):
                app = create_app(
                    job_manager=RedisRevocationJobManager(),
                    plugin_manager=PluginManager(
                        state_path=Path(raw) / 'plugins.json'
                    ),
                    database=database,
                    audit_log=AuditLogger(Path(raw) / 'audit.jsonl'),
                )
                try:
                    with TestClient(app) as client:
                        token_response = client.post('/api/v1/auth/token', data={
                            'username': 'bob',
                            'password': 'bob-secret',
                        })
                        headers = {
                            'Authorization': (
                                f"Bearer {token_response.json()['access_token']}"
                            )
                        }
                        with client.stream(
                            'GET',
                            '/api/v1/jobs/revocation-job/events?timeout_seconds=3',
                            headers=headers,
                        ) as events:
                            body = ''.join(events.iter_text())
                    self.assertEqual(events.status_code, 200)
                    self.assertIn('event: access_revoked', body)
                    self.assertIn('"status": "access_revoked"', body)
                    self.assertGreaterEqual(membership_checks, 2)
                finally:
                    self._close_app(app)

    def test_token_protects_api_but_not_health(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_auth_') as raw:
            with patch.dict(os.environ, {'CADD_API_TOKEN': 'test-token'}, clear=False):
                app = self._app(raw)
                try:
                    with TestClient(app) as client:
                        self.assertEqual(client.get('/health').status_code, 200)
                        self.assertEqual(client.get('/api/v1/plugins').status_code, 401)
                        self.assertEqual(client.get('/api/v1/plugins', headers={'Authorization': 'Bearer test-token'}).status_code, 200)
                finally:
                    self._close_app(app)

    def test_metrics_scrape_token_is_limited_to_metrics_endpoint(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_metrics_auth_') as raw:
            token_path = Path(raw) / 'metrics-token'
            token_path.write_text('metrics-secret-' * 3, encoding='utf-8')
            with patch.dict(os.environ, {
                'APP_ENV': 'development',
                'CADD_API_TOKEN': '',
                'CADD_JWT_SECRET': 'test-metrics-jwt-secret-' * 2,
                'CADD_AUTH_USERS_JSON': '{}',
                'METRICS_SCRAPE_TOKEN': '',
                'METRICS_SCRAPE_TOKEN_FILE': str(token_path),
            }, clear=False):
                app = self._app(raw)
                headers = {
                    'Authorization': f"Bearer {'metrics-secret-' * 3}",
                }
                try:
                    with TestClient(app) as client:
                        response = client.get('/metrics', headers=headers)
                        self.assertEqual(response.status_code, 200)
                        self.assertIn(
                            'bio_agent_storage_deletion_backlog',
                            response.text,
                        )
                        self.assertEqual(
                            client.get('/api/v1/plugins', headers=headers).status_code,
                            401,
                        )
                        self.assertEqual(client.get('/metrics').status_code, 401)
                finally:
                    self._close_app(app)

    def test_logout_and_admin_subject_revocation_invalidate_tokens(self):
        users = {
            'alice': {'password': 'alice-secret', 'roles': ['researcher']},
            'admin': {'password': 'admin-secret', 'roles': ['admin']},
        }
        with tempfile.TemporaryDirectory(prefix='fastapi_session_revoke_') as raw:
            with patch.dict(os.environ, {
                'CADD_API_TOKEN': '',
                'CADD_JWT_SECRET': 'test-session-secret-' * 2,
                'CADD_AUTH_USERS_JSON': json.dumps(users),
            }, clear=False):
                app = self._app(raw)
                try:
                    with TestClient(app) as client:
                        alice = client.post('/api/v1/auth/token', data={
                            'username': 'alice',
                            'password': 'alice-secret',
                        }).json()['access_token']
                        alice_headers = {'Authorization': f'Bearer {alice}'}
                        self.assertEqual(
                            client.get('/api/v1/plugins', headers=alice_headers).status_code,
                            200,
                        )
                        self.assertEqual(
                            client.post('/api/v1/auth/logout', headers=alice_headers).status_code,
                            204,
                        )
                        self.assertEqual(
                            client.get('/api/v1/plugins', headers=alice_headers).status_code,
                            401,
                        )
                        alice = client.post('/api/v1/auth/token', data={
                            'username': 'alice',
                            'password': 'alice-secret',
                        }).json()['access_token']
                        admin = client.post('/api/v1/auth/token', data={
                            'username': 'admin',
                            'password': 'admin-secret',
                        }).json()['access_token']
                        revoke = client.post(
                            '/api/v1/auth/subjects/alice/revoke',
                            json={'disabled': True},
                            headers={'Authorization': f'Bearer {admin}'},
                        )
                        self.assertEqual(revoke.status_code, 200)
                        self.assertEqual(
                            client.get(
                                '/api/v1/plugins',
                                headers={'Authorization': f'Bearer {alice}'},
                            ).status_code,
                            401,
                        )
                        self.assertEqual(
                            client.post('/api/v1/auth/token', data={
                                'username': 'alice',
                                'password': 'alice-secret',
                            }).status_code,
                            401,
                        )
                finally:
                    self._close_app(app)

    def test_browser_session_uses_httponly_cookie_and_requires_csrf(self):
        users = {
            'alice': {'password': 'alice-secret', 'roles': ['researcher']},
        }
        with tempfile.TemporaryDirectory(prefix='fastapi_browser_session_') as raw:
            with patch.dict(os.environ, {
                'CADD_API_TOKEN': '',
                'CADD_JWT_SECRET': 'test-browser-session-secret-' * 2,
                'CADD_AUTH_USERS_JSON': json.dumps(users),
            }, clear=False):
                app = self._app(raw)
                try:
                    with TestClient(app) as client:
                        token_response = client.post('/api/v1/auth/token', data={
                            'username': 'alice',
                            'password': 'alice-secret',
                        })
                        self.assertEqual(token_response.headers['cache-control'], 'no-store')
                        self.assertEqual(token_response.headers['pragma'], 'no-cache')
                        token = token_response.json()['access_token']
                        exchange = client.post(
                            '/api/v1/auth/session',
                            headers={'Authorization': f'Bearer {token}'},
                        )
                        self.assertEqual(exchange.status_code, 200)
                        self.assertEqual(exchange.headers['cache-control'], 'no-store')
                        self.assertEqual(exchange.headers['pragma'], 'no-cache')
                        self.assertIn('HttpOnly', exchange.headers['set-cookie'])
                        self.assertIn('SameSite=strict', exchange.headers['set-cookie'])
                        self.assertEqual(
                            client.get('/api/v1/plugins').status_code,
                            200,
                        )
                        self.assertEqual(
                            client.post(
                                '/api/v1/projects',
                                json={'name': 'CSRF denied'},
                            ).status_code,
                            403,
                        )
                        csrf_token = client.cookies.get('bioagent_csrf')
                        created = client.post(
                            '/api/v1/projects',
                            json={'name': 'CSRF accepted'},
                            headers={'X-CSRF-Token': csrf_token},
                        )
                        self.assertEqual(created.status_code, 201)
                        logout = client.post(
                            '/api/v1/auth/logout',
                            headers={'X-CSRF-Token': csrf_token},
                        )
                        self.assertEqual(logout.status_code, 204)
                        self.assertEqual(
                            client.get('/api/v1/plugins').status_code,
                            401,
                        )
                finally:
                    self._close_app(app)

    def test_localhost_alias_is_allowed_by_cors(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_cors_') as raw:
            env = {
                'CADD_API_TOKEN': 'test-token',
                'CORS_ORIGINS': 'http://localhost:5173,http://127.0.0.1:5173',
            }
            with patch.dict(os.environ, env, clear=False):
                app = self._app(raw)
                try:
                    with TestClient(app) as client:
                        response = client.get(
                            '/api/v1/plugins',
                            headers={
                                'Authorization': 'Bearer test-token',
                                'Origin': 'http://127.0.0.1:5173',
                            },
                        )
                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(
                            response.headers.get('access-control-allow-origin'),
                            'http://127.0.0.1:5173',
                        )
                        self.assertEqual(
                            response.headers.get('access-control-allow-credentials'),
                            'true',
                        )
                        self.assertEqual(
                            response.headers.get('x-content-type-options'),
                            'nosniff',
                        )
                        self.assertEqual(
                            response.headers.get('x-frame-options'),
                            'DENY',
                        )
                finally:
                    self._close_app(app)

    def test_job_submission_and_persistence_read_model(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_jobs_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    response = client.post('/api/v1/jobs', json={'tool': 'research_catalog', 'arguments': {}})
                    self.assertEqual(response.status_code, 202)
                    job_id = response.json()['job']['job_id']
                    for _ in range(100):
                        record = client.get(f'/api/v1/jobs/{job_id}').json()['job']
                        if record['status'] in {'completed', 'failed'}:
                            break
                        time.sleep(0.05)
                    self.assertEqual(record['status'], 'completed')
                    self.assertEqual(client.get('/api/v1/jobs').status_code, 200)
            finally:
                self._close_app(app)

    def test_job_resource_request_and_scheduler_capacity_are_exposed(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_resources_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    scheduler = client.get('/api/v1/scheduler/resources')
                    self.assertEqual(scheduler.status_code, 200)
                    self.assertIn('capacity', scheduler.json()['scheduler'])
                    submitted = client.post('/api/v1/jobs', json={
                        'tool': 'research_catalog',
                        'arguments': {},
                        'priority': 25,
                        'resources': {
                            'cpu_cores': 1,
                            'memory_mb': 512,
                            'gpu_count': 0,
                            'gpu_memory_mb': 0,
                            'labels': [],
                        },
                    })
                    self.assertEqual(submitted.status_code, 202)
                    job = submitted.json()['job']
                    self.assertEqual(job['priority'], 25)
                    self.assertEqual(job['resources']['memory_mb'], 512)
                    rejected = client.post('/api/v1/jobs', json={
                        'tool': 'research_catalog',
                        'arguments': {},
                        'resources': {'gpu_count': 1},
                    })
                    self.assertEqual(rejected.status_code, 400)
                    self.assertIn('gpu_count', rejected.json()['detail'])
            finally:
                self._close_app(app)

    def test_research_plan_execute_and_artifact_download_close_the_loop(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_research_loop_') as raw:
            app = self._app(raw)
            output_root = fastapi_module.OUTPUT_ROOT / f'test_research_loop_{Path(raw).name}'
            try:
                with TestClient(app) as client:
                    upload = client.post(
                        '/api/v1/files',
                        files={'upload': ('reads.fastq', b'@read1\nACGT\n+\nIIII\n', 'text/plain')},
                    )
                    self.assertEqual(upload.status_code, 201)
                    uploaded_path = upload.json()['file']['path']

                    plan_response = client.post('/api/v1/jobs', json={
                        'tool': 'research_plan',
                        'arguments': {
                            'task': 'Run FASTQ sequencing quality control',
                            'inputs': {
                                'input_path': uploaded_path,
                                'input_type': 'fastq',
                            },
                            'planner_mode': 'deterministic',
                            'output_dir': str(output_root / 'research_run'),
                        },
                    })
                    self.assertEqual(plan_response.status_code, 202)
                    plan_job = self._wait_for_job(client, plan_response.json()['job']['job_id'])
                    self.assertEqual(plan_job['status'], 'completed')
                    plan = plan_job['result']
                    self.assertEqual(plan['status'], 'planned')
                    self.assertTrue(plan['execution']['ready'])

                    execute_response = client.post('/api/v1/jobs', json={
                        'tool': 'research_execute',
                        'arguments': {
                            'workflow': plan['execution']['workflow'],
                            'domains': plan['selected_domains'],
                            'output_path': str(output_root / 'research_run' / 'manifest.json'),
                            'report_path': str(output_root / 'research_run' / 'report.md'),
                            'dry_run': False,
                            'continue_on_error': False,
                        },
                    })
                    self.assertEqual(execute_response.status_code, 202)
                    execute_job_id = execute_response.json()['job']['job_id']
                    with client.stream('GET', f'/api/v1/jobs/{execute_job_id}/events') as events:
                        event_body = ''.join(events.iter_text())
                    self.assertEqual(events.status_code, 200)
                    self.assertIn('"status": "completed"', event_body)
                    execute_job = self._wait_for_job(client, execute_job_id)
                    self.assertEqual(execute_job['status'], 'completed')
                    self.assertEqual(execute_job['result']['status'], 'completed')
                    manifest_path = execute_job['result']['manifest']['manifest_path']
                    self.assertTrue(Path(manifest_path).is_file())

                    artifact = client.get(
                        f"/api/v1/jobs/{execute_job['job_id']}/artifacts",
                        params={'path': manifest_path},
                    )
                    self.assertEqual(artifact.status_code, 200)
                    self.assertEqual(json.loads(artifact.content)['status'], 'completed')
                    report_path = execute_job['result']['report']['path']
                    report_artifact = client.get(
                        f"/api/v1/jobs/{execute_job['job_id']}/artifacts",
                        params={'path': report_path},
                    )
                    self.assertEqual(report_artifact.status_code, 200)
                    self.assertIn(b'# Bioinformatics Research Agent Report', report_artifact.content)
                    audit_actions = {
                        json.loads(line)['action']
                        for line in (Path(raw) / 'audit.jsonl').read_text(encoding='utf-8').splitlines()
                    }
                    self.assertTrue({'file.upload', 'job.submit', 'job.artifact_download'} <= audit_actions)
            finally:
                self._close_app(app)
                shutil.rmtree(output_root, ignore_errors=True)

    def test_jwt_login_and_role_permissions(self):
        users = {
            'alice': {'password': 'secret', 'roles': ['researcher']},
            'admin': {'password': 'admin-secret', 'roles': ['admin']},
        }
        with tempfile.TemporaryDirectory(prefix='fastapi_jwt_') as raw:
            env = {
                'CADD_API_TOKEN': '',
                'CADD_JWT_SECRET': 'test-secret-' * 4,
                'CADD_AUTH_USERS_JSON': json.dumps(users),
                'AUTH_TOKEN_TTL_SECONDS': '3600',
            }
            with patch.dict(os.environ, env, clear=False):
                app = self._app(raw)
                try:
                    with TestClient(app) as client:
                        self.assertEqual(client.get('/api/v1/plugins').status_code, 401)
                        alice_response = client.post('/api/v1/auth/token', data={'username': 'alice', 'password': 'secret'})
                        self.assertEqual(alice_response.status_code, 200)
                        alice_token = alice_response.json()['access_token']
                        alice_headers = {'Authorization': f'Bearer {alice_token}'}
                        self.assertEqual(client.get('/api/v1/plugins', headers=alice_headers).status_code, 200)
                        self.assertEqual(
                            client.post(
                                '/api/v1/jobs',
                                json={'tool': 'research_catalog', 'arguments': {}},
                                headers=alice_headers,
                            ).status_code,
                            422,
                        )
                        self.assertEqual(
                            client.post(
                                '/api/v1/files',
                                files={'upload': ('notes.txt', b'notes', 'text/plain')},
                                headers=alice_headers,
                            ).status_code,
                            422,
                        )
                        self.assertEqual(
                            client.get(
                                f'/api/v1/files/{"a" * 32}',
                                headers=alice_headers,
                            ).status_code,
                            403,
                        )
                        self.assertEqual(
                            client.post('/api/v1/plugins/cadd/state', json={'enabled': False}, headers=alice_headers).status_code,
                            403,
                        )
                        self.assertEqual(
                            client.post('/api/v1/plugins/cadd/health', headers=alice_headers).status_code,
                            403,
                        )

                        admin_response = client.post('/api/v1/auth/token', data={'username': 'admin', 'password': 'admin-secret'})
                        self.assertEqual(admin_response.status_code, 200)
                        admin_headers = {'Authorization': f"Bearer {admin_response.json()['access_token']}"}
                        changed = client.post('/api/v1/plugins/cadd/state', json={'enabled': False}, headers=admin_headers)
                        self.assertEqual(changed.status_code, 200)
                        client.post('/api/v1/plugins/cadd/state', json={'enabled': True}, headers=admin_headers)
                        manifest = client.get('/api/v1/plugins', headers=admin_headers).json()['plugins'][0]['manifest']
                        validation = client.post('/api/v1/plugins/validate', json=manifest, headers=admin_headers)
                        self.assertEqual(validation.status_code, 200)
                        self.assertTrue(validation.json()['validation']['compatible'])
                        health = client.post('/api/v1/plugins/cadd/health', headers=admin_headers)
                        self.assertEqual(health.status_code, 200)
                        self.assertTrue(health.json()['plugin']['health']['healthy'])

                        events = [json.loads(line) for line in (Path(raw) / 'audit.jsonl').read_text(encoding='utf-8').splitlines()]
                        self.assertIn('auth.login', {event['action'] for event in events})
                        self.assertIn('plugin.state_change', {event['action'] for event in events})
                        self.assertIn('plugin.validate', {event['action'] for event in events})
                        self.assertIn('plugin.health_check', {event['action'] for event in events})
                        self.assertIn('admin', {event['actor'] for event in events})
                        self.assertTrue(all(event['request_id'] for event in events))
                        self.assertTrue(all(event['trace_id'] for event in events))
                finally:
                    self._close_app(app)

    def test_idempotency_header_deduplicates_submission(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_idempotency_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    headers = {'Idempotency-Key': 'api-request-1'}
                    first = client.post('/api/v1/jobs', json={'tool': 'research_catalog', 'arguments': {}}, headers=headers)
                    second = client.post('/api/v1/jobs', json={'tool': 'research_catalog', 'arguments': {}}, headers=headers)
                    self.assertEqual(first.status_code, 202)
                    self.assertEqual(second.status_code, 202)
                    self.assertEqual(first.json()['job']['job_id'], second.json()['job']['job_id'])
                    self.assertEqual(second.json()['status'], 'deduplicated')
            finally:
                self._close_app(app)

    def test_idempotency_key_is_scoped_to_project(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_idempotency_scope_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    project_ids = [
                        client.post('/api/v1/projects', json={'name': name})
                        .json()['project']['project_id']
                        for name in ('Project A', 'Project B')
                    ]
                    responses = [
                        client.post(
                            '/api/v1/jobs',
                            json={
                                'tool': 'research_catalog',
                                'arguments': {},
                                'project_id': project_id,
                            },
                            headers={'Idempotency-Key': 'shared-key'},
                        )
                        for project_id in project_ids
                    ]
                self.assertTrue(all(item.status_code == 202 for item in responses))
                self.assertNotEqual(
                    responses[0].json()['job']['job_id'],
                    responses[1].json()['job']['job_id'],
                )
            finally:
                self._close_app(app)

    def test_production_requires_project_for_admin_submissions(self):
        settings = PlatformSettings.from_env({
            'APP_ENV': 'production',
            'PUBLIC_BASE_URL': 'https://testserver',
            'CORS_ORIGINS': 'https://testserver',
            'TRUSTED_PROXY_CIDRS': '127.0.0.1/32',
            'JOB_BACKEND': 'redis',
            'STORAGE_BACKEND': 's3',
            'S3_BUCKET': 'test-bucket',
            'DATABASE_ROLE': 'api',
        })
        with tempfile.TemporaryDirectory(prefix='fastapi_project_required_') as raw, patch(
            'src.api_runtime.AuthService.from_env',
            return_value=AuthService(),
        ):
            app = create_app(
                job_manager=JobManager(
                    max_workers=1,
                    store_path=Path(raw) / 'jobs.sqlite3',
                ),
                plugin_manager=PluginManager(
                    state_path=Path(raw) / 'plugins.json',
                ),
                database=Database(
                    f"sqlite+aiosqlite:///{(Path(raw) / 'api.sqlite3').as_posix()}"
                ),
                file_storage=LocalFileStorage(Path(raw) / 'uploads'),
                audit_log=AuditLogger(Path(raw) / 'audit.jsonl'),
                settings=settings,
            )
            try:
                with TestClient(app) as client:
                    self.assertEqual(client.get('/health').status_code, 200)
                    self.assertEqual(client.get('/docs').status_code, 404)
                    self.assertEqual(client.get('/openapi.json').status_code, 404)
                    self.assertEqual(
                        client.get('/health', headers={'Host': 'evil.example'}).status_code,
                        400,
                    )
                    job = client.post('/api/v1/jobs', json={
                        'tool': 'research_catalog',
                        'arguments': {},
                    })
                    upload = client.post(
                        '/api/v1/files',
                        files={'upload': ('notes.txt', b'notes', 'text/plain')},
                    )
                self.assertEqual(job.status_code, 422)
                self.assertEqual(upload.status_code, 422)
            finally:
                self._close_app(app)

    def test_login_rate_limit_returns_429_after_failed_attempts(self):
        users = {'alice': {'password': 'secret', 'roles': ['researcher']}}
        with tempfile.TemporaryDirectory(prefix='fastapi_rate_limit_') as raw:
            with patch.dict(os.environ, {
                'CADD_API_TOKEN': '',
                'CADD_JWT_SECRET': 'test-secret-' * 4,
                'CADD_AUTH_USERS_JSON': json.dumps(users),
                'AUTH_LOGIN_RATE_LIMIT': '1',
                'AUTH_LOGIN_RATE_WINDOW_SECONDS': '60',
            }, clear=False):
                app = self._app(raw)
                try:
                    with TestClient(app) as client:
                        first = client.post('/api/v1/auth/token', data={'username': 'alice', 'password': 'wrong'})
                        second = client.post('/api/v1/auth/token', data={'username': 'alice', 'password': 'wrong'})
                        self.assertEqual(first.status_code, 401)
                        self.assertEqual(second.status_code, 429)
                        self.assertEqual(second.headers['retry-after'], '60')
                finally:
                    self._close_app(app)

    def test_cancel_endpoint_returns_terminal_job(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_cancel_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    response = client.post('/api/v1/jobs', json={'tool': 'research_catalog', 'arguments': {}})
                    job_id = response.json()['job']['job_id']
                    for _ in range(100):
                        record = client.get(f'/api/v1/jobs/{job_id}').json()['job']
                        if record['status'] in {'completed', 'failed'}:
                            break
                        time.sleep(0.05)
                    cancelled = client.post(f'/api/v1/jobs/{job_id}/cancel')
                    self.assertEqual(cancelled.status_code, 202)
                    response_status = cancelled.json()['status']
                    self.assertIn(response_status, {'already_terminal', 'cancellation_requested', 'cancelled'})
                    if response_status == 'cancellation_requested':
                        for _ in range(100):
                            record = client.get(f'/api/v1/jobs/{job_id}').json()['job']
                            if record['status'] == 'cancelled':
                                break
                            time.sleep(0.05)
                        self.assertEqual(record['status'], 'cancelled')
            finally:
                self._close_app(app)

    def test_cancel_deferred_job_after_redis_loss(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_deferred_cancel_') as raw:
            database = Database(
                f"sqlite+aiosqlite:///{(Path(raw) / 'api.sqlite3').as_posix()}"
            )
            asyncio.run(database.init_schema())
            asyncio.run(database.stage_job({
                'job_id': 'deferred-job',
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-09-23T00:00:00+00:00',
                '_arguments': {},
                '_execution_key': 'deferred-job-capability-' + ('a' * 32),
                'execution_semantics': 'pure',
                'resources': {},
                'priority': 0,
            }))

            async def schedule_retry():
                async with database.sessions() as session:
                    outbox = await session.get(JobOutboxRow, 'deferred-job')
                    retry_at = time.time() + 300
                    outbox.next_attempt_at = retry_at
                    outbox.payload = {
                        **outbox.payload,
                        '_retry_not_before': retry_at,
                        'scheduling': {
                            'status': 'waiting_for_external_service',
                            'retry_at': retry_at,
                        },
                    }
                    await session.commit()

            asyncio.run(schedule_retry())
            app = create_app(
                job_manager=RedisLostCancelJobManager(),
                plugin_manager=PluginManager(state_path=Path(raw) / 'plugins.json'),
                database=database,
                audit_log=AuditLogger(Path(raw) / 'audit.jsonl'),
            )
            try:
                with TestClient(app) as client:
                    response = client.post('/api/v1/jobs/deferred-job/cancel')
                    self.assertEqual(response.status_code, 202)
                    self.assertEqual(response.json()['status'], 'cancelled')
                    self.assertEqual(response.json()['job']['status'], 'cancelled')
                self.assertEqual(asyncio.run(database.get_job('deferred-job'))['status'], 'cancelled')
                self.assertEqual(asyncio.run(database.list_dispatchable_jobs()), [])
            finally:
                self._close_app(app)

    def test_retry_endpoint_creates_child_job_with_original_arguments(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_retry_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    response = client.post('/api/v1/jobs', json={'tool': 'research_catalog', 'arguments': {}})
                    job_id = response.json()['job']['job_id']
                    for _ in range(100):
                        record = client.get(f'/api/v1/jobs/{job_id}').json()['job']
                        if record['status'] in {'completed', 'failed'}:
                            break
                        time.sleep(0.05)
                    retried = client.post(f'/api/v1/jobs/{job_id}/retry')
                    self.assertEqual(retried.status_code, 202)
                    self.assertEqual(retried.json()['status'], 'accepted')
                    child = retried.json()['job']
                    self.assertEqual(child['retry_of'], job_id)
                    self.assertEqual(child['tool'], 'research_catalog')
            finally:
                self._close_app(app)

    def test_required_artifact_argument_is_rejected_before_queueing(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_artifact_contract_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    response = client.post(
                        '/api/v1/jobs',
                        json={'tool': 'cadd_run_screening', 'arguments': {}},
                    )
                    self.assertEqual(response.status_code, 400)
                    self.assertIn('artifact argument is required: out', response.json()['detail'])
                    self.assertEqual(
                        client.get('/api/v1/jobs').json()['jobs'],
                        [],
                    )
            finally:
                self._close_app(app)

    def test_indeterminate_job_requires_resolution_before_retry(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_resolution_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    submitted = client.post(
                        '/api/v1/jobs',
                        json={'tool': 'research_catalog', 'arguments': {}},
                    ).json()['job']
                    self._wait_for_job(client, submitted['job_id'])
                    manager = app.state.job_manager
                    with manager._lock:
                        record = manager._jobs[submitted['job_id']]
                        record['status'] = 'indeterminate'
                        record['error'] = 'commit outcome is unknown'
                        record['indeterminate'] = {'requires_manual_review': True}
                        record.pop('resolution', None)
                        manager._persist(record)

                    rejected = client.post(
                        f"/api/v1/jobs/{submitted['job_id']}/retry"
                    )
                    self.assertEqual(rejected.status_code, 400)
                    self.assertIn('approve_retry', rejected.json()['detail'])

                    resolved = client.post(
                        f"/api/v1/jobs/{submitted['job_id']}/resolve",
                        json={
                            'decision': 'approve_retry',
                            'reason': 'external system confirms no result was committed',
                            'evidence': {'ticket': 'INC-42'},
                        },
                    )
                    self.assertEqual(resolved.status_code, 200)
                    source = resolved.json()['job']
                    self.assertEqual(source['resolution']['decision'], 'approve_retry')
                    self.assertEqual(source['resolution']['reviewer'], 'local-dev')

                    duplicate = client.post(
                        f"/api/v1/jobs/{submitted['job_id']}/resolve",
                        json={
                            'decision': 'approve_retry',
                            'reason': 'attempt to approve twice',
                        },
                    )
                    self.assertEqual(duplicate.status_code, 409)

                    retried = client.post(
                        f"/api/v1/jobs/{submitted['job_id']}/retry"
                    )
                    self.assertEqual(retried.status_code, 202)
                    self.assertEqual(
                        retried.json()['job']['retry_of'],
                        submitted['job_id'],
                    )
            finally:
                self._close_app(app)

    def test_job_events_stream_reaches_terminal_state(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_events_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    response = client.post('/api/v1/jobs', json={'tool': 'research_catalog', 'arguments': {}})
                    job_id = response.json()['job']['job_id']
                    with client.stream('GET', f'/api/v1/jobs/{job_id}/events') as events:
                        body = ''.join(events.iter_text())
                        self.assertEqual(events.status_code, 200)
                        self.assertEqual(events.headers['content-type'].split(';', 1)[0], 'text/event-stream')
                    self.assertIn('event: job', body)
                    self.assertIn('"status": "completed"', body)
            finally:
                self._close_app(app)

    def test_job_events_ticket_authenticates_native_eventsource_stream(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_event_ticket_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    response = client.post('/api/v1/jobs', json={'tool': 'research_catalog', 'arguments': {}})
                    job_id = response.json()['job']['job_id']
                    ticket_response = client.post(f'/api/v1/jobs/{job_id}/events/ticket')
                    self.assertEqual(ticket_response.status_code, 200)
                    ticket_payload = ticket_response.json()
                    self.assertEqual(ticket_payload['expires_in'], 60)
                    with client.stream(
                        'GET',
                        f'/api/v1/jobs/{job_id}/events',
                        params={'ticket': ticket_payload['ticket']},
                    ) as events:
                        body = ''.join(events.iter_text())
                    self.assertEqual(events.status_code, 200)
                    self.assertIn('event: job', body)
                    self.assertEqual(
                        client.get(
                            f'/api/v1/jobs/{job_id}/events',
                            params={'ticket': 'invalid-ticket'},
                        ).status_code,
                        401,
                    )
            finally:
                self._close_app(app)

    def test_job_events_returns_not_found(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_events_missing_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    response = client.get('/api/v1/jobs/missing/events')
                    self.assertEqual(response.status_code, 404)
            finally:
                self._close_app(app)

    def test_file_upload_is_safely_stored_and_downloadable(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_files_') as raw:
            storage = LocalFileStorage(Path(raw) / 'uploads')
            app = self._app(raw, storage)
            try:
                with TestClient(app) as client:
                    content = b'gene_id,sample_a\nTP53,12\n'
                    response = client.post(
                        '/api/v1/files',
                        files={'upload': ('../../expression.csv', content, 'text/csv')},
                    )
                    self.assertEqual(response.status_code, 201)
                    uploaded = response.json()['file']
                    self.assertEqual(uploaded['filename'], 'expression.csv')
                    self.assertEqual(uploaded['size_bytes'], len(content))
                    self.assertEqual(Path(uploaded['path']).resolve(), Path(raw).resolve() / 'uploads' / uploaded['file_id'] / 'expression.csv')
                    self.assertRegex(uploaded['file_id'], r'^[a-f0-9]{32}$')
                    self.assertEqual(len(uploaded['sha256']), 64)
                    self.assertTrue((Path(raw) / 'uploads' / uploaded['file_id'] / 'expression.csv').is_file())

                    downloaded = client.get(uploaded['download_url'])
                    self.assertEqual(downloaded.status_code, 200)
                    self.assertEqual(downloaded.content, content)
                    self.assertEqual(downloaded.headers['x-file-sha256'], uploaded['sha256'])
                    self.assertIn('expression.csv', downloaded.headers['content-disposition'])
            finally:
                self._close_app(app)

    def test_file_upload_is_discarded_when_ownership_commit_fails(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_file_ownership_') as raw:
            upload_root = Path(raw) / 'uploads'
            storage = LocalFileStorage(upload_root)
            app = self._app(raw, storage)
            try:
                with TestClient(app) as client:
                    project_id = client.post(
                        '/api/v1/projects',
                        json={'name': 'Ownership failure'},
                    ).json()['project']['project_id']
                    app.state.database.activate_file_upload = AsyncMock(
                        side_effect=RuntimeError('database unavailable')
                    )
                    response = client.post(
                        '/api/v1/files',
                        data={'project_id': project_id},
                        files={'upload': ('notes.txt', b'notes', 'text/plain')},
                    )
                self.assertEqual(response.status_code, 503)
                self.assertEqual(list(upload_root.iterdir()), [])
            finally:
                self._close_app(app)

    def test_remote_file_download_forwards_reference_and_releases_workspace(self):
        class RemoteStorage(LocalFileStorage):
            backend = 's3'

            def __init__(self, root):
                super().__init__(root)
                self.reference = None
                self.materialized_path = None

            def get(self, file_id, reference=None):
                self.reference = reference
                directory = self.root / '.downloads' / file_id
                directory.mkdir(parents=True)
                self.materialized_path = directory / 'expression.csv'
                self.materialized_path.write_bytes(b'gene,value\nTP53,12\n')
                return StoredFile(
                    file_id=file_id,
                    filename='expression.csv',
                    content_type='text/csv',
                    size_bytes=self.materialized_path.stat().st_size,
                    sha256='a' * 64,
                    path=self.materialized_path,
                    version_id='version-1',
                )

            def release(self, stored):
                stored.path.unlink(missing_ok=True)

        with tempfile.TemporaryDirectory(prefix='fastapi_remote_file_') as raw:
            storage = RemoteStorage(Path(raw) / 'uploads')
            app = self._app(raw, storage)
            try:
                with TestClient(app) as client:
                    response = client.get(
                        f'/api/v1/files/{"a" * 32}',
                        params={'storage_reference': 'bio+s3://bucket/key'},
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(storage.reference, 'bio+s3://bucket/key')
                self.assertEqual(response.headers['x-file-version'], 'version-1')
                self.assertFalse(storage.materialized_path.exists())
            finally:
                self._close_app(app)

    def test_file_upload_rejects_unsupported_and_oversized_files(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_file_validation_') as raw:
            storage = LocalFileStorage(Path(raw) / 'uploads', max_bytes=4)
            app = self._app(raw, storage)
            try:
                with TestClient(app) as client:
                    unsupported = client.post('/api/v1/files', files={'upload': ('payload.exe', b'ab', 'application/octet-stream')})
                    self.assertEqual(unsupported.status_code, 400)
                    oversized = client.post('/api/v1/files', files={'upload': ('payload.csv', b'12345', 'text/csv')})
                    self.assertEqual(oversized.status_code, 400)
                    self.assertEqual(list((Path(raw) / 'uploads').iterdir()), [])
            finally:
                self._close_app(app)

    def test_file_upload_accepts_vcf_and_vcf_gz_files(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_vcf_files_') as raw:
            storage = LocalFileStorage(Path(raw) / 'uploads')
            app = self._app(raw, storage)
            try:
                with TestClient(app) as client:
                    for filename, content in (
                        ('variants.vcf', b'##fileformat=VCFv4.3\n'),
                        (
                            'variants.vcf.gz',
                            gzip.compress(b'##fileformat=VCFv4.3\n#CHROM\tPOS\n'),
                        ),
                    ):
                        response = client.post(
                            '/api/v1/files',
                            files={'upload': (filename, content, 'application/octet-stream')},
                        )
                        self.assertEqual(response.status_code, 201)
                        uploaded = response.json()['file']
                        self.assertEqual(uploaded['filename'], filename)
                        self.assertEqual(uploaded['size_bytes'], len(content))
            finally:
                self._close_app(app)

    def test_job_artifact_download_is_result_scoped(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_artifacts_') as raw:
            output_root = Path(raw) / 'output'
            output_root.mkdir()
            artifact = output_root / 'report.md'
            artifact.write_bytes(b'# report\n')
            outside = Path(raw) / 'secret.txt'
            outside.write_text('secret\n', encoding='utf-8')
            with patch.object(fastapi_module, 'OUTPUT_ROOT', output_root):
                app = self._app(raw)
                try:
                    manager = app.state.job_manager
                    job = manager.submit('research_catalog', {})
                    for _ in range(100):
                        record = manager.get(job['job_id'])
                        if record['status'] == 'completed':
                            break
                        time.sleep(0.05)
                    manager._jobs[job['job_id']]['result'] = {'report_path': str(artifact)}
                    durable_artifact = {
                        'artifact_id': 'a' * 32,
                        'filename': artifact.name,
                        'content_type': 'text/markdown',
                        'size_bytes': artifact.stat().st_size,
                        'sha256': hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        'storage_backend': 'local',
                        'path': str(artifact),
                    }
                    manager._jobs[job['job_id']]['artifacts'] = [durable_artifact]
                    manager._persist(manager._jobs[job['job_id']])
                    with TestClient(app) as client:
                        downloaded = client.get(
                            f"/api/v1/jobs/{job['job_id']}/artifacts",
                            params={'path': str(artifact)},
                        )
                        self.assertEqual(downloaded.status_code, 200)
                        self.assertEqual(downloaded.text, '# report\n')
                        self.assertEqual(downloaded.headers['x-job-id'], job['job_id'])
                        self.assertIn(
                            'attachment',
                            downloaded.headers['content-disposition'].lower(),
                        )
                        self.assertEqual(
                            downloaded.headers['content-security-policy'],
                            "sandbox; default-src 'none'",
                        )
                        self.assertEqual(downloaded.headers['deprecation'], 'true')
                        self._persist_artifact(
                            app,
                            manager._jobs[job['job_id']],
                            durable_artifact,
                            status='uploaded',
                        )
                        uncommitted = client.get(
                            f"/api/v1/jobs/{job['job_id']}/artifacts/{'a' * 32}",
                        )
                        self.assertEqual(uncommitted.status_code, 404)
                        visible_uncommitted = client.get(
                            f"/api/v1/jobs/{job['job_id']}"
                        ).json()['job']['artifacts']
                        self.assertEqual(visible_uncommitted, [])
                        self._persist_artifact(
                            app,
                            manager._jobs[job['job_id']],
                            durable_artifact,
                        )
                        visible_committed = client.get(
                            f"/api/v1/jobs/{job['job_id']}"
                        ).json()['job']['artifacts']
                        self.assertEqual(
                            visible_committed[0]['status'],
                            'committed',
                        )
                        published = client.get(
                            f"/api/v1/jobs/{job['job_id']}/artifacts/{'a' * 32}",
                        )
                        self.assertEqual(published.status_code, 200)
                        self.assertEqual(published.text, '# report\n')
                        self.assertEqual(
                            published.headers['x-artifact-sha256'],
                            hashlib.sha256(artifact.read_bytes()).hexdigest(),
                        )
                        artifact.write_text('tampered\n', encoding='utf-8')
                        tampered = client.get(
                            f"/api/v1/jobs/{job['job_id']}/artifacts/{'a' * 32}",
                        )
                        self.assertEqual(tampered.status_code, 502)
                        deletion = client.delete(
                            f"/api/v1/jobs/{job['job_id']}/artifacts/{'a' * 32}",
                        )
                        self.assertEqual(deletion.status_code, 202)
                        hidden_legacy = client.get(
                            f"/api/v1/jobs/{job['job_id']}/artifacts",
                            params={'path': str(artifact)},
                        )
                        self.assertEqual(hidden_legacy.status_code, 404)
                        forbidden = client.get(
                            f"/api/v1/jobs/{job['job_id']}/artifacts",
                            params={'path': str(outside)},
                        )
                        self.assertEqual(forbidden.status_code, 404)
                finally:
                    self._close_app(app)

    def test_file_and_artifact_deletion_requests_are_hidden_and_idempotent(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_delete_storage_') as raw:
            output_root = Path(raw) / 'output'
            output_root.mkdir()
            artifact_path = output_root / 'result.json'
            artifact_path.write_text('{"status":"ok"}\n', encoding='utf-8')
            audit_path = Path(raw) / 'audit.jsonl'
            with patch.object(fastapi_module, 'OUTPUT_ROOT', output_root):
                app = self._app(raw, audit_log=AuditLogger(audit_path))
                try:
                    with TestClient(app) as client:
                        manager = app.state.job_manager
                        submitted = manager.submit('research_catalog', {})
                        for _ in range(100):
                            record = manager.get(submitted['job_id'])
                            if record['status'] == 'completed':
                                break
                            time.sleep(0.05)
                        artifact = {
                            'artifact_id': '7' * 32,
                            'filename': artifact_path.name,
                            'content_type': 'application/json',
                            'size_bytes': artifact_path.stat().st_size,
                            'sha256': hashlib.sha256(artifact_path.read_bytes()).hexdigest(),
                            'storage_backend': 'local',
                            'path': str(artifact_path),
                        }
                        self._persist_artifact(
                            app,
                            manager._jobs[submitted['job_id']],
                            artifact,
                        )
                        upload = client.post(
                            '/api/v1/files',
                            files={'upload': ('input.csv', b'value\n1\n', 'text/csv')},
                        )
                        self.assertEqual(upload.status_code, 201)
                        file_id = upload.json()['file']['file_id']
                        headers = {'Idempotency-Key': 'delete-once'}
                        deleted_file = client.delete(
                            f'/api/v1/files/{file_id}',
                            headers=headers,
                        )
                        self.assertEqual(deleted_file.status_code, 202)
                        repeated_file = client.delete(
                            f'/api/v1/files/{file_id}',
                            headers=headers,
                        )
                        self.assertEqual(repeated_file.status_code, 202)
                        self.assertEqual(
                            deleted_file.json()['file']['delete_request_id'],
                            repeated_file.json()['file']['delete_request_id'],
                        )
                        self.assertEqual(
                            client.get(f'/api/v1/files/{file_id}').status_code,
                            404,
                        )
                        file_status = client.get(
                            f'/api/v1/files/{file_id}/deletion'
                        )
                        self.assertEqual(file_status.status_code, 200)
                        self.assertEqual(
                            [event['status'] for event in file_status.json()['events']],
                            ['delete_requested'],
                        )
                        self.assertEqual(
                            client.post(
                                f'/api/v1/files/{file_id}/deletion/retry',
                                headers=headers,
                            ).status_code,
                            409,
                        )
                        deleted_artifact = client.delete(
                            f"/api/v1/jobs/{submitted['job_id']}/artifacts/{'7' * 32}",
                            headers=headers,
                        )
                        self.assertEqual(deleted_artifact.status_code, 202)
                        self.assertEqual(
                            deleted_artifact.json()['artifact']['status'],
                            'delete_requested',
                        )
                        self.assertEqual(
                            client.get(
                                f"/api/v1/jobs/{submitted['job_id']}/artifacts/{'7' * 32}"
                            ).status_code,
                            404,
                        )
                        artifact_status = client.get(
                            f"/api/v1/jobs/{submitted['job_id']}/artifacts/{'7' * 32}/deletion"
                        )
                        self.assertEqual(artifact_status.status_code, 200)
                        self.assertEqual(
                            artifact_status.json()['events'][0]['status'],
                            'delete_requested',
                        )
                    actions = {
                        json.loads(line)['action']
                        for line in audit_path.read_text(encoding='utf-8').splitlines()
                    }
                    self.assertIn('file.delete_requested', actions)
                    self.assertIn('job.artifact_delete_requested', actions)
                finally:
                    self._close_app(app)

    def test_legacy_path_artifact_download_can_be_disabled(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_legacy_artifact_') as raw:
            output_root = Path(raw) / 'output'
            output_root.mkdir()
            artifact = output_root / 'legacy.txt'
            artifact.write_text('legacy\n', encoding='utf-8')
            settings = PlatformSettings.from_env({
                'ALLOW_LEGACY_ARTIFACT_PATHS': 'false',
            })
            with patch.object(fastapi_module, 'OUTPUT_ROOT', output_root):
                app = self._app(raw, settings=settings)
                try:
                    manager = app.state.job_manager
                    job = manager.submit('research_catalog', {})
                    for _ in range(100):
                        if manager.get(job['job_id'])['status'] == 'completed':
                            break
                        time.sleep(0.05)
                    manager._jobs[job['job_id']]['result'] = {
                        'report_path': str(artifact),
                    }
                    manager._persist(manager._jobs[job['job_id']])
                    with TestClient(app) as client:
                        response = client.get(
                            f"/api/v1/jobs/{job['job_id']}/artifacts",
                            params={'path': str(artifact)},
                        )
                    self.assertEqual(response.status_code, 410)
                    self.assertIn('artifact_id', response.json()['detail'])
                finally:
                    self._close_app(app)

    def test_version_locked_s3_job_artifact_download(self):
        class S3Client:
            def __init__(self, body, sha256):
                self.body = body
                self.sha256 = sha256
                self.requests = []

            def get_object(self, **request):
                self.requests.append(request)
                return {
                    'Body': BytesIO(self.body),
                    'ContentLength': len(self.body),
                    'Metadata': {'sha256': self.sha256},
                    'VersionId': 'version-7',
                }

        class S3Storage:
            backend = 's3'
            bucket = 'research-results'
            prefix = 'bio-agent'
            expected_bucket_owner = '123456789012'

            def __init__(self, client):
                self.client = client

        with tempfile.TemporaryDirectory(prefix='fastapi_s3_artifact_') as raw:
            body = b'{"status":"ok"}\n'
            sha256 = hashlib.sha256(body).hexdigest()
            s3_client = S3Client(body, sha256)
            app = self._app(raw, file_storage=S3Storage(s3_client))
            try:
                manager = app.state.job_manager
                job = manager.submit('research_catalog', {})
                for _ in range(100):
                    if manager.get(job['job_id'])['status'] == 'completed':
                        break
                    time.sleep(0.05)
                durable_artifact = {
                    'artifact_id': 'b' * 32,
                    'filename': 'result.json',
                    'content_type': 'application/json',
                    'size_bytes': len(body),
                    'sha256': sha256,
                    'storage_backend': 's3',
                    'storage_key': 'bio-agent/artifacts/project/job/result.json',
                    'version_id': 'version-7',
                    'reference': S3ObjectReference(
                        'research-results',
                        'bio-agent/artifacts/project/job/result.json',
                        'version-7',
                        sha256,
                        len(body),
                    ).serialize(),
                }
                manager._jobs[job['job_id']]['artifacts'] = [durable_artifact]
                manager._persist(manager._jobs[job['job_id']])
                with TestClient(app) as client:
                    self._persist_artifact(
                        app,
                        manager._jobs[job['job_id']],
                        durable_artifact,
                    )
                    response = client.get(
                        f"/api/v1/jobs/{job['job_id']}/artifacts/{'b' * 32}",
                    )
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, body)
                self.assertEqual(response.headers['x-artifact-sha256'], sha256)
                self.assertEqual(s3_client.requests, [{
                    'Bucket': 'research-results',
                    'Key': 'bio-agent/artifacts/project/job/result.json',
                    'VersionId': 'version-7',
                    'ExpectedBucketOwner': '123456789012',
                }])
            finally:
                self._close_app(app)


if __name__ == '__main__':
    unittest.main()
