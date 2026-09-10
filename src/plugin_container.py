"""Remote container executor for isolated scientific tool execution."""

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

try:
    from .job_execution import (
        ExecutionLimits,
        JobExecutionCancelled,
        JobExecutionError,
        JobExecutionTimedOut,
    )
    from .observability import current_context, log_event
    from .run_context import current_run_context
except ImportError:
    from job_execution import (
        ExecutionLimits,
        JobExecutionCancelled,
        JobExecutionError,
        JobExecutionTimedOut,
    )
    from observability import current_context, log_event
    from run_context import current_run_context


class ContainerToolExecutor:
    mode = 'container'

    def __init__(
        self,
        base_url,
        token,
        limits=None,
        transport=None,
        max_concurrency=8,
    ):
        if not base_url or not str(base_url).strip():
            raise ValueError('PLUGIN_SANDBOX_URL is required')
        if not token or len(str(token)) < 32:
            raise ValueError('PLUGIN_SANDBOX_TOKEN must contain at least 32 characters')
        self.base_url = str(base_url).rstrip('/')
        self.token = str(token)
        self.limits = limits or ExecutionLimits.from_env()
        self.transport = transport or self._http_transport
        self._pool = ThreadPoolExecutor(max_workers=max(int(max_concurrency), 1))
        self._shutdown = Event()
        self._active = set()
        self._active_lock = Lock()

    def _http_transport(self, path, payload, timeout_seconds):
        encoded = json.dumps(
            payload, ensure_ascii=False, default=str
        ).encode('utf-8')
        request = Request(
            f'{self.base_url}{path}',
            data=encoded,
            headers={
                'Authorization': f'Bearer {self.token}',
                'Content-Type': 'application/json',
            },
            method='POST',
        )
        limit = self.limits.max_result_bytes + 64 * 1024
        try:
            with urlopen(request, timeout=timeout_seconds) as response:
                body = response.read(limit + 1)
        except HTTPError as exc:
            body = exc.read(64 * 1024)
            detail = body.decode('utf-8', errors='replace')
            raise JobExecutionError(
                f'plugin sandbox rejected request with HTTP {exc.code}: {detail}'
            ) from exc
        except (OSError, TimeoutError, URLError) as exc:
            raise JobExecutionError('plugin sandbox is unavailable') from exc
        if len(body) > limit:
            raise JobExecutionError('plugin sandbox response exceeded size limit')
        try:
            return json.loads(body.decode('utf-8'))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JobExecutionError('plugin sandbox returned invalid JSON') from exc

    def _cancel(self, request_id):
        try:
            self.transport(
                f'/v1/cancel/{request_id}',
                {},
                min(self.limits.terminate_grace_seconds, 3.0),
            )
        except Exception:
            return

    def execute(self, tool, arguments, *, cancelled=None, heartbeat=None):
        if self._shutdown.is_set():
            raise JobExecutionCancelled('tool executor is shutting down')
        request_id = uuid4().hex
        payload = {
            'request_id': request_id,
            'tool': tool,
            'arguments': arguments,
            'limits': self.limits.as_dict(),
            'observability': current_context(),
            'run_context': current_run_context(as_dict=True),
        }
        timeout = self.limits.timeout_seconds or 24 * 60 * 60
        future = self._pool.submit(
            self.transport,
            '/v1/execute',
            payload,
            timeout + self.limits.terminate_grace_seconds + 5,
        )
        with self._active_lock:
            self._active.add(request_id)
        started = monotonic()
        try:
            while not future.done():
                if cancelled and cancelled():
                    self._cancel(request_id)
                    raise JobExecutionCancelled('job cancelled by user')
                if self._shutdown.is_set():
                    self._cancel(request_id)
                    raise JobExecutionCancelled('tool executor is shutting down')
                if self.limits.timeout_seconds and (
                    monotonic() - started >= self.limits.timeout_seconds
                ):
                    self._cancel(request_id)
                    raise JobExecutionTimedOut(
                        'job exceeded container execution timeout of '
                        f'{self.limits.timeout_seconds} seconds'
                    )
                if heartbeat:
                    heartbeat()
                sleep(self.limits.poll_interval_seconds)
            response = future.result()
        finally:
            with self._active_lock:
                self._active.discard(request_id)
        if not isinstance(response, dict):
            raise JobExecutionError('plugin sandbox returned an invalid response')
        if not response.get('ok'):
            raise JobExecutionError(
                response.get('error') or 'plugin sandbox execution failed'
            )
        log_event(
            'tool.execution.remote_completed',
            tool=tool,
            execution_mode='container',
        )
        return response.get('result')

    def shutdown(self):
        self._shutdown.set()
        with self._active_lock:
            active = tuple(self._active)
        for request_id in active:
            self._cancel(request_id)
        self._pool.shutdown(wait=False, cancel_futures=True)


def container_tool_executor_from_env():
    token = os.environ.get('PLUGIN_SANDBOX_TOKEN', '')
    token_path = os.environ.get('PLUGIN_SANDBOX_TOKEN_FILE', '').strip()
    if token_path:
        try:
            token = Path(token_path).read_text(encoding='utf-8').strip()
        except OSError as exc:
            raise ValueError('unable to read PLUGIN_SANDBOX_TOKEN_FILE') from exc
    return ContainerToolExecutor(
        os.environ.get('PLUGIN_SANDBOX_URL', ''),
        token,
        limits=ExecutionLimits.from_env(),
        max_concurrency=int(os.environ.get('PLUGIN_SANDBOX_CLIENT_CONCURRENCY', '8')),
    )
