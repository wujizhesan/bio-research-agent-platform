import os
import asyncio
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.audit_log import AuditLogger
from src.database import Database
from src.file_storage import LocalFileStorage
from src.fastapi_app import create_app
import src.fastapi_app as fastapi_module
from src.job_manager import JobManager
from src.plugin_manager import PluginManager


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


class FastApiAppTests(unittest.TestCase):
    def _app(self, root, file_storage=None, audit_log=None):
        app = create_app(
            job_manager=JobManager(max_workers=1, store_path=Path(root) / 'jobs.sqlite3'),
            plugin_manager=PluginManager(state_path=Path(root) / 'plugins.json'),
            database=Database(f"sqlite+aiosqlite:///{(Path(root) / 'api.sqlite3').as_posix()}"),
            file_storage=file_storage,
            audit_log=audit_log or AuditLogger(Path(root) / 'audit.jsonl'),
        )
        return app

    def _close_app(self, app):
        app.state.job_manager.shutdown()
        asyncio.run(app.state.database.close())

    def test_health_openapi_and_database(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_app_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    health = client.get('/health', headers={'X-Request-ID': 'interview-trace-001'})
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(health.json()['database'], 'ok')
                    self.assertEqual(health.headers['x-request-id'], 'interview-trace-001')
                    self.assertIn('/api/v1/jobs', client.get('/openapi.json').json()['paths'])
                    self.assertIn('bio_agent_http_requests_total', client.get('/metrics').text)
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
                            client.post('/api/v1/plugins/cadd/state', json={'enabled': False}, headers=alice_headers).status_code,
                            403,
                        )

                        admin_response = client.post('/api/v1/auth/token', data={'username': 'admin', 'password': 'admin-secret'})
                        self.assertEqual(admin_response.status_code, 200)
                        admin_headers = {'Authorization': f"Bearer {admin_response.json()['access_token']}"}
                        changed = client.post('/api/v1/plugins/cadd/state', json={'enabled': False}, headers=admin_headers)
                        self.assertEqual(changed.status_code, 200)
                        client.post('/api/v1/plugins/cadd/state', json={'enabled': True}, headers=admin_headers)

                        events = [json.loads(line) for line in (Path(raw) / 'audit.jsonl').read_text(encoding='utf-8').splitlines()]
                        self.assertIn('auth.login', {event['action'] for event in events})
                        self.assertIn('plugin.state_change', {event['action'] for event in events})
                        self.assertIn('admin', {event['actor'] for event in events})
                        self.assertTrue(all(event['request_id'] for event in events))
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
                        ('variants.vcf.gz', b'compressed-vcf-fixture'),
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
                    manager._persist(manager._jobs[job['job_id']])
                    with TestClient(app) as client:
                        downloaded = client.get(
                            f"/api/v1/jobs/{job['job_id']}/artifacts",
                            params={'path': str(artifact)},
                        )
                        self.assertEqual(downloaded.status_code, 200)
                        self.assertEqual(downloaded.text, '# report\n')
                        self.assertEqual(downloaded.headers['x-job-id'], job['job_id'])
                        forbidden = client.get(
                            f"/api/v1/jobs/{job['job_id']}/artifacts",
                            params={'path': str(outside)},
                        )
                        self.assertEqual(forbidden.status_code, 404)
                finally:
                    self._close_app(app)


if __name__ == '__main__':
    unittest.main()
