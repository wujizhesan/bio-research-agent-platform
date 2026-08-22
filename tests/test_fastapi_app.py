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
                    for _ in range(30):
                        record = client.get(f'/api/v1/jobs/{job_id}').json()['job']
                        if record['status'] in {'completed', 'failed'}:
                            break
                        time.sleep(0.05)
                    self.assertEqual(record['status'], 'completed')
                    self.assertEqual(client.get('/api/v1/jobs').status_code, 200)
            finally:
                self._close_app(app)


if __name__ == '__main__':
    unittest.main()
