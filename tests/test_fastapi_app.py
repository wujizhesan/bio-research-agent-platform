import os
import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from src.database import Database
from src.fastapi_app import create_app
from src.job_manager import JobManager
from src.plugin_manager import PluginManager


class FastApiAppTests(unittest.TestCase):
    def _app(self, root):
        app = create_app(
            job_manager=JobManager(max_workers=1, store_path=Path(root) / 'jobs.sqlite3'),
            plugin_manager=PluginManager(state_path=Path(root) / 'plugins.json'),
            database=Database(f"sqlite+aiosqlite:///{(Path(root) / 'api.sqlite3').as_posix()}"),
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
                    health = client.get('/health')
                    self.assertEqual(health.status_code, 200)
                    self.assertEqual(health.json()['database'], 'ok')
                    self.assertIn('/api/v1/jobs', client.get('/openapi.json').json()['paths'])
                    self.assertIn('bio_agent_http_requests_total', client.get('/metrics').text)
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

    def test_job_events_returns_not_found(self):
        with tempfile.TemporaryDirectory(prefix='fastapi_events_missing_') as raw:
            app = self._app(raw)
            try:
                with TestClient(app) as client:
                    response = client.get('/api/v1/jobs/missing/events')
                    self.assertEqual(response.status_code, 404)
            finally:
                self._close_app(app)


if __name__ == '__main__':
    unittest.main()
