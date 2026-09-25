import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

from src.worker_health import WorkerHealthState, WorkerHttpServer


class HealthyManager:
    worker_id = 'worker-health-test'

    def ping(self):
        return None

    def execution_catalog(self):
        return {'research_catalog': {'fingerprint': 'implementation'}}


class HealthyWriter:
    def health(self):
        return {'healthy': True, 'pending': 0, 'capacity': 1000}


class WorkerHealthTests(unittest.TestCase):
    def test_readiness_covers_dependencies_and_drain_state(self):
        with tempfile.TemporaryDirectory(prefix='worker_health_') as raw, patch.dict(
            'os.environ',
            {
                'STORAGE_BACKEND': 'local',
                'WORKER_MIN_FREE_DISK_BYTES': '0',
            },
        ):
            state = WorkerHealthState(HealthyManager(), HealthyWriter(), raw)
            ready = state.snapshot()
            self.assertTrue(ready['ready'])
            self.assertEqual(ready['configuration']['schema_version'], 1)
            self.assertEqual(len(ready['configuration']['fingerprint']), 64)
            self.assertEqual(set(ready['checks']), {
                'redis',
                'database_state_writer',
                'plugin_executor',
                'object_storage',
                'disk',
            })
            state.update(draining=True, active_jobs=2)
            draining = state.snapshot()
            self.assertFalse(draining['ready'])
            self.assertTrue(draining['draining'])
            self.assertEqual(draining['active_jobs'], 2)

    def test_http_server_exposes_readiness_and_metrics(self):
        with tempfile.TemporaryDirectory(prefix='worker_health_http_') as raw, patch.dict(
            'os.environ',
            {
                'STORAGE_BACKEND': 'local',
                'WORKER_MIN_FREE_DISK_BYTES': '0',
            },
        ):
            state = WorkerHealthState(HealthyManager(), HealthyWriter(), raw)
            server = WorkerHttpServer('127.0.0.1', 0, state).start()
            port = server.server.server_port
            try:
                ready = urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/ready',
                    timeout=2,
                )
                metrics = urllib.request.urlopen(
                    f'http://127.0.0.1:{port}/metrics',
                    timeout=2,
                )
                self.assertEqual(ready.status, 200)
                self.assertIn(b'python_info', metrics.read())
                state.update(draining=True)
                with self.assertRaises(urllib.error.HTTPError) as failure:
                    urllib.request.urlopen(
                        f'http://127.0.0.1:{port}/ready',
                        timeout=2,
                    )
                self.assertEqual(failure.exception.code, 503)
            finally:
                server.close()


if __name__ == '__main__':
    unittest.main()
