"""Remote container executor for isolated scientific tool execution."""

import json
import os
import re
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from uuid import uuid4

try:
    from .external_service_policy import retry_deferred_from_payload
    from .job_execution import (
        ExecutionLimits,
        JobExecutionCancelled,
        JobExecutionError,
        JobExecutionTimedOut,
    )
    from .observability import current_context, log_event
    from .run_context import current_run_context
    from .storage_workspace import materialize_storage_references
except ImportError:
    from external_service_policy import retry_deferred_from_payload
    from job_execution import (
        ExecutionLimits,
        JobExecutionCancelled,
        JobExecutionError,
        JobExecutionTimedOut,
    )
    from observability import current_context, log_event
    from run_context import current_run_context
    from storage_workspace import materialize_storage_references


_SAFE_SEGMENT = re.compile(r'[^A-Za-z0-9._-]+')


def _within(path, roots):
    resolved = Path(path).expanduser().resolve(strict=False)
    return any(
        resolved == root or root in resolved.parents
        for root in roots
    )


def _safe_segment(value, fallback):
    selected = _SAFE_SEGMENT.sub('_', str(value)).strip(' ._')
    return (selected or fallback)[:120]


def _iter_paths(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                yield item


def _map_paths(value, mapper):
    if isinstance(value, str):
        return mapper(value)
    if isinstance(value, list):
        return [mapper(item) if isinstance(item, str) else item for item in value]
    if isinstance(value, tuple):
        return tuple(mapper(item) if isinstance(item, str) else item for item in value)
    return value


def _reject_symlinks(path):
    path = Path(path)
    candidates = [path]
    if path.is_dir():
        candidates.extend(path.rglob('*'))
    if any(candidate.is_symlink() for candidate in candidates):
        raise JobExecutionError('plugin workspace transfer rejects symbolic links')


def _tree_size(path):
    path = Path(path)
    if path.is_file():
        return path.stat().st_size
    return sum(item.stat().st_size for item in path.rglob('*') if item.is_file())


def _copy_path(source, target):
    source = Path(source)
    target = Path(target)
    _reject_symlinks(source)
    if source.is_dir():
        shutil.copytree(source, target)
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    else:
        raise JobExecutionError(f'plugin input path is unavailable: {source.name}')


def _replace_result_paths(value, replacements):
    if isinstance(value, dict):
        return {
            key: _replace_result_paths(item, replacements)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_replace_result_paths(item, replacements) for item in value]
    if isinstance(value, tuple):
        return tuple(_replace_result_paths(item, replacements) for item in value)
    if not isinstance(value, str):
        return value
    for staged, target in replacements:
        if value == staged:
            return target
        prefix = staged + os.sep
        if value.startswith(prefix):
            return target + value[len(staged):]
    return value


def _tool_filesystem_contract(tool):
    try:
        from .domain_registry import active_tool_specs
        from .execution_semantics import workspace_path_contract
    except ImportError:
        try:
            from domain_registry import active_tool_specs
            from execution_semantics import workspace_path_contract
        except ImportError:
            return set(), {}, set()
    spec = next(
        (item for item in active_tool_specs() if item.get('name') == tool),
        None,
    )
    if spec is None:
        return set(), {}, set()
    return workspace_path_contract(spec)


class ContainerToolExecutor:
    mode = 'container'

    def __init__(
        self,
        base_url,
        token,
        limits=None,
        transport=None,
        max_concurrency=8,
        input_workspace_root=None,
        storage_client=None,
        artifact_root=None,
        input_roots=None,
        workspace_max_bytes=None,
    ):
        if not base_url or not str(base_url).strip():
            raise ValueError('PLUGIN_SANDBOX_URL is required')
        if not token or len(str(token)) < 32:
            raise ValueError('PLUGIN_SANDBOX_TOKEN must contain at least 32 characters')
        self.base_url = str(base_url).rstrip('/')
        self.token = str(token)
        self.limits = limits or ExecutionLimits.from_env()
        self.transport = transport or self._http_transport
        self.input_workspace_root = Path(
            input_workspace_root
            or os.environ.get('JOB_INPUT_WORKSPACE_ROOT', 'output/.job-inputs')
        ).resolve()
        self.storage_client = storage_client
        self.artifact_root = Path(
            artifact_root
            or os.environ.get('PLUGIN_ARTIFACT_ROOT', 'output')
        ).resolve()
        configured_input_roots = input_roots
        if configured_input_roots is None:
            configured_input_roots = os.environ.get(
                'PLUGIN_INPUT_ROOTS', 'output,data'
            ).split(',')
        self.input_roots = tuple(
            Path(value).resolve()
            for value in configured_input_roots
            if str(value).strip()
        )
        self.workspace_max_bytes = max(int(
            workspace_max_bytes
            or os.environ.get(
                'PLUGIN_SANDBOX_WORKSPACE_MAX_BYTES', str(10 * 1024 ** 3)
            )
        ), 1)
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
            try:
                failure = json.loads(body.decode('utf-8'))
            except (UnicodeDecodeError, json.JSONDecodeError):
                failure = {}
            deferred = retry_deferred_from_payload(failure)
            if deferred is not None:
                raise deferred from exc
            raise JobExecutionError(
                f'plugin sandbox rejected request with HTTP {exc.code}',
                error_code=failure.get('error_code'),
            ) from exc
        except (OSError, TimeoutError, URLError) as exc:
            raise JobExecutionError(
                'plugin sandbox is unavailable',
                error_code='sandbox_unavailable',
            ) from exc
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
        self.input_workspace_root.mkdir(parents=True, exist_ok=True)
        request_id = uuid4().hex
        workspace = self.input_workspace_root / request_id
        workspace.mkdir(mode=0o700)
        try:
            resolved_arguments = materialize_storage_references(
                arguments,
                workspace / 'materialized',
                client=self.storage_client,
            )
            if _tree_size(workspace) > self.workspace_max_bytes:
                raise JobExecutionError('plugin workspace input quota exceeded')
            resolved_arguments, outputs = self._stage_workspace(
                tool, resolved_arguments, workspace
            )
            return self._execute_resolved(
                tool,
                resolved_arguments,
                request_id=request_id,
                workspace=workspace,
                outputs=outputs,
                cancelled=cancelled,
                heartbeat=heartbeat,
            )
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _stage_workspace(self, tool, arguments, workspace):
        reads, artifact_kinds, writes = _tool_filesystem_contract(tool)
        selected = dict(arguments)
        outputs = []
        transferred = _tree_size(workspace)
        for parameter in sorted(reads | writes):
            if parameter not in selected:
                continue
            index = 0

            def stage(raw):
                nonlocal index, transferred
                source = Path(raw).expanduser().resolve(strict=False)
                name = _safe_segment(source.name, 'path')
                parameter_dir = _safe_segment(parameter, 'argument')
                if parameter in writes:
                    if not _within(source, (self.artifact_root,)):
                        raise JobExecutionError(
                            f'plugin output path is outside the artifact root: {parameter}'
                        )
                    staged = workspace / 'outputs' / parameter_dir / str(index) / name
                    directory = artifact_kinds.get(parameter) == 'directory' or (
                        parameter.lower().endswith(('_dir', '_directory'))
                    )
                    if parameter in reads and source.exists():
                        transferred += _tree_size(source)
                        if transferred > self.workspace_max_bytes:
                            raise JobExecutionError(
                                'plugin workspace input quota exceeded'
                            )
                        _copy_path(source, staged)
                    elif directory:
                        staged.mkdir(parents=True)
                    else:
                        staged.parent.mkdir(parents=True, exist_ok=True)
                    outputs.append((str(staged), str(source), directory))
                else:
                    if _within(source, (workspace,)):
                        staged = source
                    else:
                        if not _within(source, self.input_roots):
                            raise JobExecutionError(
                                f'plugin input path is outside allowed roots: {parameter}'
                            )
                        transferred += _tree_size(source)
                        if transferred > self.workspace_max_bytes:
                            raise JobExecutionError(
                                'plugin workspace input quota exceeded'
                            )
                        staged = workspace / 'inputs' / parameter_dir / str(index) / name
                        _copy_path(source, staged)
                index += 1
                return str(staged)

            selected[parameter] = _map_paths(selected[parameter], stage)
        return selected, tuple(outputs)

    def _publish_outputs(self, outputs):
        total = 0
        replacements = []
        published = []
        try:
            for staged_raw, target_raw, directory in outputs:
                staged = Path(staged_raw)
                target = Path(target_raw)
                if not staged.exists():
                    continue
                _reject_symlinks(staged)
                total += _tree_size(staged)
                if total > self.workspace_max_bytes:
                    raise JobExecutionError('plugin workspace output quota exceeded')
                if target.exists():
                    if target.is_dir() and not any(target.iterdir()):
                        target.rmdir()
                    else:
                        raise JobExecutionError(
                            f'plugin artifact target already exists: {target.name}'
                        )
                target.parent.mkdir(parents=True, exist_ok=True)
                if directory or staged.is_dir():
                    shutil.copytree(staged, target)
                else:
                    shutil.copy2(staged, target)
                published.append(target)
                replacements.append((str(staged), str(target)))
        except Exception:
            for target in reversed(published):
                if target.is_dir():
                    shutil.rmtree(target, ignore_errors=True)
                else:
                    target.unlink(missing_ok=True)
            raise
        return tuple(replacements)

    def _execute_resolved(
        self,
        tool,
        arguments,
        *,
        request_id=None,
        workspace=None,
        outputs=(),
        cancelled=None,
        heartbeat=None,
    ):
        if self._shutdown.is_set():
            raise JobExecutionCancelled('tool executor is shutting down')
        request_id = request_id or uuid4().hex
        payload = {
            'request_id': request_id,
            'tool': tool,
            'arguments': arguments,
            'limits': self.limits.as_dict(),
            'observability': current_context(),
            'run_context': current_run_context(as_dict=True),
        }
        if workspace is not None:
            payload['workspace'] = {'root': str(workspace)}
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
            deferred = retry_deferred_from_payload(response)
            if deferred is not None:
                raise deferred
            raise JobExecutionError(
                'plugin sandbox execution failed',
                error_code=response.get('error_code'),
            )
        log_event(
            'tool.execution.remote_completed',
            tool=tool,
            execution_mode='container',
        )
        replacements = self._publish_outputs(outputs)
        return _replace_result_paths(response.get('result'), replacements)

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
        input_workspace_root=os.environ.get('JOB_INPUT_WORKSPACE_ROOT') or None,
        artifact_root=os.environ.get('PLUGIN_ARTIFACT_ROOT') or None,
    )
