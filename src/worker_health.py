"""Worker liveness, readiness and metrics HTTP endpoint."""

import json
import os
from pathlib import Path
import shutil
from threading import Thread
from wsgiref.simple_server import make_server

from prometheus_client import make_wsgi_app

try:
    from .settings import PlatformSettings
except ImportError:
    from settings import PlatformSettings


class WorkerHealthState:
    def __init__(self, manager, state_writer, output_root=None, settings=None):
        self.manager = manager
        self.state_writer = state_writer
        self.settings = settings or PlatformSettings.from_env()
        self.output_root = Path(
            output_root or 'output'
        ).resolve()
        self.output_root.mkdir(parents=True, exist_ok=True)
        self.draining = False
        self.active_jobs = 0

    def update(self, *, draining=None, active_jobs=None):
        if draining is not None:
            self.draining = bool(draining)
        if active_jobs is not None:
            self.active_jobs = max(int(active_jobs), 0)

    def _storage(self):
        backend = self.settings.storage_backend
        if backend == 'local':
            return os.access(self.output_root, os.W_OK)
        if backend != 's3' or not self.settings.s3_bucket:
            return False
        try:
            import boto3
            from botocore.config import Config
            client = boto3.client(
                's3',
                endpoint_url=self.settings.s3_endpoint_url or None,
                region_name=self.settings.s3_region or None,
                config=Config(
                    connect_timeout=1,
                    read_timeout=1,
                    retries={'max_attempts': 0},
                ),
            )
            request = {'Bucket': self.settings.s3_bucket}
            owner = self.settings.s3_expected_bucket_owner
            if owner:
                request['ExpectedBucketOwner'] = owner
            client.head_bucket(**request)
            return True
        except Exception:
            return False

    def snapshot(self):
        checks = {}
        try:
            self.manager.ping()
            checks['redis'] = 'ok'
        except Exception:
            checks['redis'] = 'unavailable'
        try:
            writer = self.state_writer.health()
            if not writer['healthy']:
                checks['database_state_writer'] = 'unavailable'
            elif not writer.get('accepting_work', True):
                checks['database_state_writer'] = 'backpressure'
            else:
                checks['database_state_writer'] = 'ok'
        except Exception:
            writer = {'healthy': False}
            checks['database_state_writer'] = 'unavailable'
        try:
            catalog = self.manager.execution_catalog()
            checks['plugin_executor'] = 'ok' if catalog else 'unavailable'
        except Exception:
            checks['plugin_executor'] = 'unavailable'
        checks['object_storage'] = 'ok' if self._storage() else 'unavailable'
        try:
            free_bytes = shutil.disk_usage(self.output_root).free
            minimum = self.settings.worker_min_free_disk_bytes
            checks['disk'] = 'ok' if free_bytes >= minimum else 'exhausted'
        except Exception:
            free_bytes = None
            checks['disk'] = 'unavailable'
        ready = not self.draining and all(
            value == 'ok' for value in checks.values()
        )
        return {
            'status': 'ok' if ready else 'degraded',
            'ready': ready,
            'worker_id': self.manager.worker_id,
            'draining': self.draining,
            'active_jobs': self.active_jobs,
            'checks': checks,
            'state_writer': writer,
            'disk_free_bytes': free_bytes,
            'configuration': self.settings.public_snapshot(),
        }


class WorkerHttpServer:
    def __init__(self, host, port, health_state):
        metrics = make_wsgi_app()

        def application(environ, start_response):
            path = environ.get('PATH_INFO', '/')
            if path == '/metrics':
                return metrics(environ, start_response)
            if path == '/live':
                payload = {'status': 'ok', 'worker_id': health_state.manager.worker_id}
                status = '200 OK'
            elif path in {'/health', '/ready'}:
                payload = health_state.snapshot()
                status = '200 OK' if payload['ready'] else '503 Service Unavailable'
            else:
                payload = {'status': 'not_found'}
                status = '404 Not Found'
            encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            start_response(status, [
                ('Content-Type', 'application/json; charset=utf-8'),
                ('Content-Length', str(len(encoded))),
            ])
            return [encoded]

        self.server = make_server(host, int(port), application)
        self.thread = Thread(
            target=self.server.serve_forever,
            name='worker-health',
            daemon=True,
        )

    def start(self):
        self.thread.start()
        return self

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
