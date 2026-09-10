"""Authenticated execution broker hosted inside the plugin sandbox container."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
from threading import BoundedSemaphore, Event, Lock

try:
    from .job_execution import ExecutionLimits, ProcessToolExecutor
    from .observability import bind_context, configure_logging, log_event
    from .run_context import bind_run_context
except ImportError:
    from job_execution import ExecutionLimits, ProcessToolExecutor
    from observability import bind_context, configure_logging, log_event
    from run_context import bind_run_context


REQUEST_ID_PATTERN = re.compile(r'^[a-f0-9]{32}$')


class SandboxRuntime:
    def __init__(self, executor_factory=None, max_concurrency=2):
        self.executor_factory = executor_factory or (
            lambda: ProcessToolExecutor(ExecutionLimits.from_env())
        )
        self.capacity = BoundedSemaphore(max(int(max_concurrency), 1))
        self._active = {}
        self._lock = Lock()

    def execute(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('sandbox request must be an object')
        request_id = payload.get('request_id')
        tool = payload.get('tool')
        arguments = payload.get('arguments', {})
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise ValueError('sandbox request_id is invalid')
        if not isinstance(tool, str) or not tool or len(tool) > 200:
            raise ValueError('sandbox tool is invalid')
        if not isinstance(arguments, dict):
            raise ValueError('sandbox arguments must be an object')
        if not self.capacity.acquire(timeout=1):
            raise RuntimeError('plugin sandbox capacity is exhausted')
        cancellation = Event()
        with self._lock:
            if request_id in self._active:
                self.capacity.release()
                raise ValueError('duplicate sandbox request_id')
            self._active[request_id] = cancellation
        executor = None
        try:
            executor = self.executor_factory()
            context = (
                bind_run_context(payload['run_context'])
                if payload.get('run_context')
                else bind_context(**(payload.get('observability') or {}))
            )
            with context:
                result = executor.execute(
                    tool,
                    arguments,
                    cancelled=cancellation.is_set,
                )
            return {'ok': True, 'result': result}
        finally:
            if executor is not None:
                executor.shutdown()
            with self._lock:
                self._active.pop(request_id, None)
            self.capacity.release()

    def cancel(self, request_id):
        if not REQUEST_ID_PATTERN.fullmatch(str(request_id)):
            return False
        with self._lock:
            cancellation = self._active.get(request_id)
        if cancellation is None:
            return False
        cancellation.set()
        return True

    @property
    def active_count(self):
        with self._lock:
            return len(self._active)


class SandboxHTTPServer(ThreadingHTTPServer):
    daemon_threads = True


class SandboxHandler(BaseHTTPRequestHandler):
    server_version = 'bio-agent-plugin-sandbox/1'

    def _write(self, status, payload):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self):
        authorization = self.headers.get('Authorization', '')
        scheme, _, supplied = authorization.partition(' ')
        expected = self.server.sandbox_token
        return (
            scheme.lower() == 'bearer'
            and bool(supplied)
            and hmac.compare_digest(supplied, expected)
        )

    def _payload(self):
        raw_length = self.headers.get('Content-Length', '')
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ValueError('invalid Content-Length') from exc
        if length < 0 or length > self.server.max_request_bytes:
            raise ValueError('sandbox request exceeds size limit')
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError('sandbox request contains invalid JSON') from exc

    def do_GET(self):
        if self.path != '/health':
            self._write(404, {'status': 'not_found'})
            return
        self._write(200, {
            'status': 'ok',
            'active': self.server.runtime.active_count,
        })

    def do_POST(self):
        if not self._authorized():
            self._write(401, {'status': 'error', 'error': 'authentication required'})
            return
        try:
            if self.path == '/v1/execute':
                result = self.server.runtime.execute(self._payload())
                self._write(200, result)
                return
            prefix = '/v1/cancel/'
            if self.path.startswith(prefix):
                cancelled = self.server.runtime.cancel(self.path[len(prefix):])
                self._write(202 if cancelled else 404, {
                    'status': 'cancelled' if cancelled else 'not_found'
                })
                return
            self._write(404, {'status': 'not_found'})
        except ValueError as exc:
            self._write(400, {'ok': False, 'error': str(exc)})
        except Exception as exc:
            log_event(
                'plugin.sandbox.execution_failed',
                error_type=type(exc).__name__,
            )
            self._write(500, {
                'ok': False,
                'error': str(exc) or type(exc).__name__,
            })

    def log_message(self, *_args):
        return


def create_server(host, port, token, runtime=None, max_request_bytes=None):
    if not token or len(token) < 32:
        raise ValueError('PLUGIN_SANDBOX_TOKEN must contain at least 32 characters')
    server = SandboxHTTPServer((host, port), SandboxHandler)
    server.sandbox_token = token
    server.runtime = runtime or SandboxRuntime(
        max_concurrency=int(os.environ.get('PLUGIN_SANDBOX_MAX_CONCURRENCY', '2'))
    )
    server.max_request_bytes = int(
        max_request_bytes
        or os.environ.get('PLUGIN_SANDBOX_MAX_REQUEST_BYTES', str(16 * 1024 * 1024))
    )
    return server


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the plugin sandbox broker')
    parser.add_argument('--host', default=os.environ.get('PLUGIN_SANDBOX_HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('PLUGIN_SANDBOX_PORT', '8081')))
    args = parser.parse_args(argv)
    configure_logging('bio-agent-plugin-sandbox')
    token = os.environ.get('PLUGIN_SANDBOX_TOKEN', '')
    token_path = os.environ.get('PLUGIN_SANDBOX_TOKEN_FILE', '').strip()
    if token_path:
        try:
            token = Path(token_path).read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise SystemExit('unable to read PLUGIN_SANDBOX_TOKEN_FILE') from exc
    server = create_server(
        args.host,
        args.port,
        token,
    )
    log_event('plugin.sandbox.started', host=args.host, port=args.port)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
