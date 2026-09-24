"""Authenticated execution broker hosted inside the plugin sandbox container."""

import argparse
from dataclasses import replace
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
from pathlib import Path
import re
from threading import Condition, Event

try:
    from .external_service_policy import ServiceRetryDeferredError
    from .job_execution import ExecutionLimits, ProcessToolExecutor
    from .observability import bind_context, configure_logging, log_event
    from .run_context import bind_run_context
except ImportError:
    from external_service_policy import ServiceRetryDeferredError
    from job_execution import ExecutionLimits, ProcessToolExecutor
    from observability import bind_context, configure_logging, log_event
    from run_context import bind_run_context


REQUEST_ID_PATTERN = re.compile(r'^[a-f0-9]{32}$')
LIGHTWEIGHT_ARGUMENT_LIMIT = 1024 * 1024
LIGHTWEIGHT_INDEX_LIMIT = 8 * 1024 * 1024


class SandboxRequestError(ValueError):
    pass


def _sandbox_path_values(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                yield item


class SandboxRuntime:
    def __init__(
        self,
        executor_factory=None,
        max_concurrency=2,
        workspace_root=None,
    ):
        self.executor_factory = executor_factory
        memory_budget_mb = max(int(os.environ.get(
            'PLUGIN_SANDBOX_MEMORY_BUDGET_MB', '4096'
        )), 1)
        self.light_memory_limit_mb = min(max(int(os.environ.get(
            'PLUGIN_SANDBOX_LIGHT_MEMORY_LIMIT_MB', '2048'
        )), 1), memory_budget_mb)
        self.max_concurrency = min(
            max(int(max_concurrency), 1),
            max(memory_budget_mb // self.light_memory_limit_mb, 1),
        )
        self._active = {}
        self._capacity = Condition()
        self._running = 0
        self._exclusive = False
        self._waiting_exclusive = 0
        self.workspace_root = Path(
            workspace_root
            or os.environ.get(
                'PLUGIN_SANDBOX_WORKSPACE_ROOT',
                '/run/bioagent/plugin-exchange',
            )
        ).resolve()

    def _validate_workspace(self, request_id, payload):
        workspace = payload.get('workspace')
        if workspace is None:
            return
        if not isinstance(workspace, dict) or set(workspace) != {'root'}:
            raise SandboxRequestError('sandbox workspace metadata is invalid')
        expected = (self.workspace_root / request_id).resolve(strict=False)
        supplied = Path(str(workspace.get('root') or '')).resolve(strict=False)
        if supplied != expected or not expected.is_dir():
            raise SandboxRequestError(
                'sandbox workspace is outside the request boundary'
            )
        for value in self._filesystem_paths(payload.get('tool'), payload.get('arguments')):
            candidate = Path(value).resolve(strict=False)
            if candidate != expected and expected not in candidate.parents:
                raise SandboxRequestError(
                    'sandbox argument path is outside the request workspace'
                )

    @staticmethod
    def _filesystem_paths(tool, arguments):
        try:
            from .domain_registry import active_tool_specs
            from .execution_semantics import workspace_path_contract
        except ImportError:
            from domain_registry import active_tool_specs
            from execution_semantics import workspace_path_contract
        spec = next(
            (item for item in active_tool_specs() if item.get('name') == tool),
            None,
        )
        if spec is None:
            return ()
        reads, _artifacts, writes = workspace_path_contract(spec)
        names = reads | writes
        return tuple(
            path
            for name in names
            for path in _sandbox_path_values((arguments or {}).get(name))
        )

    @staticmethod
    def _lightweight(tool, arguments):
        if tool == 'omics_inspect_toolchain':
            return not arguments
        if tool == 'literature_summarize':
            return len(json.dumps(arguments, default=str).encode('utf-8')) <= LIGHTWEIGHT_ARGUMENT_LIMIT
        if tool == 'knowledge_search':
            index_path = arguments.get('index_path')
            if not isinstance(index_path, str):
                return False
            try:
                return Path(index_path).stat().st_size <= LIGHTWEIGHT_INDEX_LIMIT
            except OSError:
                return False
        return False

    def execute(self, payload):
        if not isinstance(payload, dict):
            raise SandboxRequestError('sandbox request must be an object')
        request_id = payload.get('request_id')
        tool = payload.get('tool')
        arguments = payload.get('arguments', {})
        if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
            raise SandboxRequestError('sandbox request_id is invalid')
        if not isinstance(tool, str) or not tool or len(tool) > 200:
            raise SandboxRequestError('sandbox tool is invalid')
        if not isinstance(arguments, dict):
            raise SandboxRequestError('sandbox arguments must be an object')
        self._validate_workspace(request_id, payload)
        cancellation = Event()
        lightweight = self._lightweight(tool, arguments)
        with self._capacity:
            if request_id in self._active:
                raise SandboxRequestError('duplicate sandbox request_id')
            self._active[request_id] = cancellation
            if not lightweight:
                self._waiting_exclusive += 1
            try:
                while True:
                    if cancellation.is_set():
                        raise RuntimeError('plugin sandbox request was cancelled')
                    if lightweight:
                        admitted = (
                            not self._exclusive
                            and not self._waiting_exclusive
                            and self._running < self.max_concurrency
                        )
                    else:
                        admitted = self._running == 0
                    if admitted:
                        self._running += 1
                        self._exclusive = not lightweight
                        break
                    self._capacity.wait()
            except BaseException:
                self._active.pop(request_id, None)
                raise
            finally:
                if not lightweight:
                    self._waiting_exclusive -= 1
                    self._capacity.notify_all()
        executor = None
        try:
            if self.executor_factory is None:
                limits = ExecutionLimits.from_env()
                if lightweight:
                    limits = replace(
                        limits,
                        memory_limit_mb=min(
                            limits.memory_limit_mb or self.light_memory_limit_mb,
                            self.light_memory_limit_mb,
                        ),
                    )
                executor = ProcessToolExecutor(limits)
            else:
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
            try:
                if executor is not None:
                    executor.shutdown()
            finally:
                with self._capacity:
                    self._active.pop(request_id, None)
                    self._running -= 1
                    if not lightweight:
                        self._exclusive = False
                    self._capacity.notify_all()

    def cancel(self, request_id):
        if not REQUEST_ID_PATTERN.fullmatch(str(request_id)):
            return False
        with self._capacity:
            cancellation = self._active.get(request_id)
            if cancellation is None:
                return False
            cancellation.set()
            self._capacity.notify_all()
            return True

    @property
    def active_count(self):
        with self._capacity:
            return self._running


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
            raise SandboxRequestError('invalid Content-Length') from exc
        if length < 0 or length > self.server.max_request_bytes:
            raise SandboxRequestError('sandbox request exceeds size limit')
        try:
            return json.loads(self.rfile.read(length).decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SandboxRequestError(
                'sandbox request contains invalid JSON'
            ) from exc

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
            self._write(401, {
                'ok': False,
                'error_code': 'sandbox_authentication_required',
                'error': 'plugin sandbox authentication failed',
            })
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
        except SandboxRequestError as exc:
            log_event(
                'plugin.sandbox.request_rejected',
                level=logging.WARNING,
                error_code='sandbox_invalid_request',
                error_type=type(exc).__name__,
                error_detail=str(exc),
            )
            self._write(400, {
                'ok': False,
                'error_code': 'sandbox_invalid_request',
                'error': 'plugin sandbox rejected the request',
            })
        except ServiceRetryDeferredError as exc:
            self._write(503, {
                'ok': False,
                'error': 'external service requested retry later',
                **exc.as_payload(),
            })
        except Exception as exc:
            log_event(
                'plugin.sandbox.execution_failed',
                level=logging.ERROR,
                error_code='sandbox_execution_failed',
                error_type=type(exc).__name__,
                error_detail=str(exc),
            )
            self._write(500, {
                'ok': False,
                'error_code': 'sandbox_execution_failed',
                'error': 'plugin execution failed',
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
