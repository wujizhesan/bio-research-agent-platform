"""Isolated execution for research tools."""

import json
import math
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from threading import Event, Lock
from time import monotonic, sleep

try:
    from .external_service_policy import (
        ServiceRetryDeferredError,
        retry_deferred_from_payload,
    )
    from .observability import (
        TOOL_DURATION,
        TOOL_EXECUTIONS,
        TOOL_PHASE_DURATION,
        current_context,
        log_event,
    )
    from .run_context import current_run_context
    from .storage_workspace import materialize_storage_references
except ImportError:
    from external_service_policy import (
        ServiceRetryDeferredError,
        retry_deferred_from_payload,
    )
    from observability import (
        TOOL_DURATION,
        TOOL_EXECUTIONS,
        TOOL_PHASE_DURATION,
        current_context,
        log_event,
    )
    from run_context import current_run_context
    from storage_workspace import materialize_storage_references


_PUBLIC_EXECUTION_MESSAGES = {
    'execution_failed': 'job execution failed',
    'execution_indeterminate': 'job outcome requires manual review',
    'external_retry_deferred': 'external service requested retry later',
    'job_execution_cancelled': 'job execution was cancelled',
    'job_execution_timed_out': 'job execution timed out',
    'sandbox_authentication_required': 'plugin sandbox authentication failed',
    'sandbox_execution_failed': 'plugin execution failed',
    'sandbox_invalid_request': 'plugin sandbox rejected the request',
    'sandbox_unavailable': 'plugin sandbox is unavailable',
    'tool_execution_failed': 'tool execution failed',
}
_ERROR_CODE_PATTERN = re.compile(r'^[a-z][a-z0-9_]{2,63}$')


class JobExecutionError(RuntimeError):
    error_code = 'execution_failed'

    def __init__(self, message=None, *, error_code=None):
        super().__init__(message or _PUBLIC_EXECUTION_MESSAGES[self.error_code])
        selected = str(error_code or self.error_code)
        self.error_code = (
            selected
            if _ERROR_CODE_PATTERN.fullmatch(selected)
            and selected in _PUBLIC_EXECUTION_MESSAGES
            else 'execution_failed'
        )


class JobExecutionCancelled(JobExecutionError):
    error_code = 'job_execution_cancelled'


class JobExecutionTimedOut(JobExecutionError):
    error_code = 'job_execution_timed_out'


def public_execution_failure(exc):
    if isinstance(exc, ServiceRetryDeferredError):
        return {
            'status': 'error',
            'error_code': 'external_retry_deferred',
            'error': _PUBLIC_EXECUTION_MESSAGES['external_retry_deferred'],
            'retry_after_seconds': exc.retry_after_seconds,
        }
    code = (
        exc.error_code
        if isinstance(exc, JobExecutionError)
        else 'execution_failed'
    )
    return {
        'status': 'error',
        'error_code': code,
        'error': _PUBLIC_EXECUTION_MESSAGES[code],
    }


def public_tool_failure(_result):
    return {
        'status': 'error',
        'error_code': 'tool_execution_failed',
        'error': _PUBLIC_EXECUTION_MESSAGES['tool_execution_failed'],
    }


def _nonnegative_duration(value):
    try:
        duration = float(value)
    except (TypeError, ValueError):
        return None
    return duration if math.isfinite(duration) and duration >= 0 else None


def _tool_spec(tool):
    try:
        from .domain_registry import active_tool_specs
    except ImportError:
        from domain_registry import active_tool_specs
    return next(
        (item for item in active_tool_specs() if item['name'] == tool),
        None,
    )


def _env_int(name, default, minimum=0):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f'{name} must be an integer') from exc
    if value < minimum:
        raise ValueError(f'{name} must be at least {minimum}')
    return value


def _env_float(name, default, minimum=0.01):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f'{name} must be a number') from exc
    if value < minimum:
        raise ValueError(f'{name} must be at least {minimum}')
    return value


def _sandbox_environment(tool, temporary_root, spec=None):
    try:
        from .plugin_security import sandbox_environment
    except ImportError:
        from plugin_security import sandbox_environment
    spec = spec if spec is not None else _tool_spec(tool)
    if spec is None:
        return None
    environment = sandbox_environment(
        os.environ,
        spec.get('plugin_security', {}),
        spec.get('permissions', {}),
    )
    if environment is not None:
        plugin_temp = Path(temporary_root) / 'plugin_tmp'
        plugin_temp.mkdir()
        environment.update({
            'TEMP': str(plugin_temp),
            'TMP': str(plugin_temp),
            'TMPDIR': str(plugin_temp),
        })
        environment['PLUGIN_SANDBOX_DOMAIN'] = spec['domain']
        context = current_run_context(as_dict=True) or {}
        execution = context.get('execution') or {}
        environment['BIO_AGENT_JOB_ID'] = str(context.get('job_id') or '')
        environment['BIO_AGENT_EXECUTION_KEY'] = str(
            execution.get('execution_key') or ''
        )
        environment['BIO_AGENT_IDEMPOTENCY_KEY'] = str(
            execution.get('idempotency_key') or ''
        )
        environment['BIO_AGENT_EXECUTION_SEMANTICS'] = str(
            execution.get('semantics') or spec.get('execution_semantics') or 'pure'
        )
    return environment


@dataclass(frozen=True)
class ExecutionLimits:
    timeout_seconds: int = 3600
    memory_limit_mb: int = 8192
    cpu_time_seconds: int = 0
    max_result_bytes: int = 16 * 1024 * 1024
    poll_interval_seconds: float = 0.1
    terminate_grace_seconds: float = 3.0

    @classmethod
    def from_env(cls):
        return cls(
            timeout_seconds=_env_int('JOB_TIMEOUT_SECONDS', 3600),
            memory_limit_mb=_env_int('JOB_MEMORY_LIMIT_MB', 8192),
            cpu_time_seconds=_env_int('JOB_CPU_TIME_SECONDS', 0),
            max_result_bytes=_env_int('JOB_RESULT_MAX_BYTES', 16 * 1024 * 1024, 1024),
            poll_interval_seconds=_env_float('JOB_POLL_INTERVAL_SECONDS', 0.1),
            terminate_grace_seconds=_env_float('JOB_TERMINATE_GRACE_SECONDS', 3.0),
        )

    def as_dict(self):
        return {
            'memory_limit_mb': self.memory_limit_mb,
            'cpu_time_seconds': self.cpu_time_seconds,
            'max_result_bytes': self.max_result_bytes,
        }


class InlineToolExecutor:
    mode = 'inline'

    def __init__(self, runner, storage_client=None):
        self.runner = runner
        self.storage_client = storage_client

    def execute(self, tool, arguments, *, cancelled=None, heartbeat=None):
        if cancelled and cancelled():
            raise JobExecutionCancelled('job cancelled by user')
        if heartbeat:
            heartbeat()
        with tempfile.TemporaryDirectory(
            prefix='bio_agent_job_', ignore_cleanup_errors=True
        ) as raw:
            resolved_arguments = materialize_storage_references(
                arguments,
                Path(raw) / 'inputs',
                client=self.storage_client,
            )
            result = self.runner(tool, resolved_arguments)
        if cancelled and cancelled():
            raise JobExecutionCancelled('job cancelled by user')
        return result


class _WindowsJob:
    def __init__(self, process, limits):
        self.handle = None
        if os.name != 'nt' or not (limits.memory_limit_mb or limits.cpu_time_seconds):
            return
        import ctypes
        from ctypes import wintypes

        class BasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ('PerProcessUserTimeLimit', ctypes.c_longlong),
                ('PerJobUserTimeLimit', ctypes.c_longlong),
                ('LimitFlags', wintypes.DWORD),
                ('MinimumWorkingSetSize', ctypes.c_size_t),
                ('MaximumWorkingSetSize', ctypes.c_size_t),
                ('ActiveProcessLimit', wintypes.DWORD),
                ('Affinity', ctypes.c_size_t),
                ('PriorityClass', wintypes.DWORD),
                ('SchedulingClass', wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ('ReadOperationCount', ctypes.c_ulonglong),
                ('WriteOperationCount', ctypes.c_ulonglong),
                ('OtherOperationCount', ctypes.c_ulonglong),
                ('ReadTransferCount', ctypes.c_ulonglong),
                ('WriteTransferCount', ctypes.c_ulonglong),
                ('OtherTransferCount', ctypes.c_ulonglong),
            ]

        class ExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ('BasicLimitInformation', BasicLimitInformation),
                ('IoInfo', IoCounters),
                ('ProcessMemoryLimit', ctypes.c_size_t),
                ('JobMemoryLimit', ctypes.c_size_t),
                ('PeakProcessMemoryUsed', ctypes.c_size_t),
                ('PeakJobMemoryUsed', ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), 'CreateJobObjectW failed')
        information = ExtendedLimitInformation()
        information.BasicLimitInformation.LimitFlags = 0x2000
        if limits.memory_limit_mb:
            information.BasicLimitInformation.LimitFlags |= 0x100
            information.ProcessMemoryLimit = limits.memory_limit_mb * 1024 * 1024
        if limits.cpu_time_seconds:
            information.BasicLimitInformation.LimitFlags |= 0x2
            information.BasicLimitInformation.PerProcessUserTimeLimit = limits.cpu_time_seconds * 10_000_000
        if not kernel32.SetInformationJobObject(handle, 9, ctypes.byref(information), ctypes.sizeof(information)):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, 'SetInformationJobObject failed')
        process_handle = wintypes.HANDLE(int(process._handle))
        if not kernel32.AssignProcessToJobObject(handle, process_handle):
            error = ctypes.get_last_error()
            kernel32.CloseHandle(handle)
            raise OSError(error, 'AssignProcessToJobObject failed')
        self.handle = handle
        self._close_handle = kernel32.CloseHandle

    def close(self):
        if self.handle is not None:
            self._close_handle(self.handle)
            self.handle = None


class ProcessToolExecutor:
    mode = 'process'

    def __init__(
        self,
        limits=None,
        python_executable=None,
        runner_path=None,
        popen_factory=None,
        storage_client=None,
    ):
        self.limits = limits or ExecutionLimits.from_env()
        self.python_executable = python_executable or sys.executable
        self.runner_path = Path(runner_path or Path(__file__).with_name('job_subprocess.py'))
        self.popen_factory = popen_factory or subprocess.Popen
        self.storage_client = storage_client
        self._shutdown = Event()
        self._process_lock = Lock()
        self._active_processes = set()

    def _stop(self, process):
        if process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=self.limits.terminate_grace_seconds)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=self.limits.terminate_grace_seconds)

    def execute(self, tool, arguments, *, cancelled=None, heartbeat=None):
        if self._shutdown.is_set():
            raise JobExecutionCancelled('tool executor is shutting down')
        with tempfile.TemporaryDirectory(
            prefix='bio_agent_job_', ignore_cleanup_errors=True
        ) as raw:
            root = Path(raw)
            request_path = root / 'request.json'
            response_path = root / 'response.json'
            error_path = root / 'stderr.log'
            spec = _tool_spec(tool)
            child_environment = _sandbox_environment(tool, root, spec)
            resolved_arguments = materialize_storage_references(
                arguments,
                root / 'inputs',
                client=self.storage_client,
            )
            request_path.write_text(
                json.dumps(
                    {
                        'tool': tool,
                        'execution_domain': (
                            'knowledge' if spec and spec['domain'] == 'knowledge' else None
                        ),
                        'arguments': resolved_arguments,
                        'limits': self.limits.as_dict(),
                        'observability': current_context(),
                        'run_context': current_run_context(as_dict=True),
                    },
                    ensure_ascii=False,
                    default=str,
                ),
                encoding='utf-8',
            )
            started = monotonic()
            windows_job = None
            with error_path.open('wb') as error_stream:
                process = self.popen_factory(
                    [self.python_executable, str(self.runner_path), str(request_path), str(response_path)],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=error_stream,
                    env=child_environment,
                    cwd=str(root) if child_environment is not None else None,
                )
                with self._process_lock:
                    if self._shutdown.is_set():
                        self._stop(process)
                        raise JobExecutionCancelled('tool executor is shutting down')
                    self._active_processes.add(process)
                try:
                    if process.poll() is None:
                        windows_job = _WindowsJob(process, self.limits)
                    while process.poll() is None:
                        now = monotonic()
                        if cancelled and cancelled():
                            self._stop(process)
                            raise JobExecutionCancelled('job cancelled by user')
                        if self._shutdown.is_set():
                            self._stop(process)
                            raise JobExecutionCancelled('tool executor is shutting down')
                        if self.limits.timeout_seconds and now - started >= self.limits.timeout_seconds:
                            self._stop(process)
                            raise JobExecutionTimedOut(
                                f'job exceeded execution timeout of {self.limits.timeout_seconds} seconds'
                            )
                        if heartbeat:
                            heartbeat()
                        sleep(self.limits.poll_interval_seconds)
                    if self._shutdown.is_set():
                        raise JobExecutionCancelled('tool executor is shutting down')
                except Exception:
                    self._stop(process)
                    raise
                finally:
                    with self._process_lock:
                        self._active_processes.discard(process)
                    if windows_job is not None:
                        windows_job.close()
            process_elapsed_seconds = monotonic() - started
            if not response_path.exists():
                detail = error_path.read_text(encoding='utf-8', errors='replace')[-2000:].strip()
                suffix = f': {detail}' if detail else ''
                raise JobExecutionError(f'isolated worker exited with code {process.returncode}{suffix}')
            if response_path.stat().st_size > self.limits.max_result_bytes:
                raise JobExecutionError(
                    f'job result exceeded {self.limits.max_result_bytes} byte limit'
                )
            try:
                payload = json.loads(response_path.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError) as exc:
                raise JobExecutionError('isolated worker returned an invalid response') from exc
            telemetry = payload.get('telemetry')
            if isinstance(telemetry, dict):
                domain = str(telemetry.get('domain') or 'unknown')[:128]
                metric_tool = str(telemetry.get('tool') or tool)[:200]
                outcome = str(telemetry.get('status') or 'unknown')[:32]
                TOOL_EXECUTIONS.labels(domain, metric_tool, outcome).inc()
                duration = _nonnegative_duration(telemetry.get('duration_seconds'))
                if duration is not None:
                    TOOL_DURATION.labels(domain, metric_tool).observe(duration)
                phases = {
                    'registry_import': _nonnegative_duration(
                        telemetry.get('registry_import_seconds')
                    ),
                    'tool_run': _nonnegative_duration(
                        telemetry.get('tool_run_seconds')
                    ),
                }
                if duration is not None:
                    measured = sum(value for value in phases.values() if value is not None)
                    phases['child_other'] = max(duration - measured, 0)
                    phases['process_boundary'] = max(
                        process_elapsed_seconds - duration, 0
                    )
                phases = {
                    phase: seconds for phase, seconds in phases.items()
                    if seconds is not None
                }
                for phase, seconds in phases.items():
                    TOOL_PHASE_DURATION.labels(domain, metric_tool, phase).observe(seconds)
                log_event(
                    'tool.execution.completed',
                    domain=domain,
                    tool=metric_tool,
                    status=outcome,
                    duration_seconds=duration,
                    phase_seconds=phases,
                    execution_mode='process',
                )
            if not payload.get('ok'):
                deferred = retry_deferred_from_payload(payload)
                if deferred is not None:
                    raise deferred
                raise JobExecutionError(payload.get('error') or 'isolated worker failed')
            return payload.get('result')

    def shutdown(self):
        self._shutdown.set()
        with self._process_lock:
            processes = tuple(self._active_processes)
        for process in processes:
            try:
                self._stop(process)
            except (OSError, subprocess.SubprocessError):
                continue


def build_tool_executor_from_env(default_mode='process', runner=None):
    mode = os.environ.get('JOB_EXECUTION_MODE', default_mode).strip().lower()
    if mode == 'process':
        return ProcessToolExecutor(ExecutionLimits.from_env())
    if mode == 'inline':
        if runner is None:
            try:
                from .domain_registry import run_tool
            except ImportError:
                from domain_registry import run_tool
            runner = run_tool
        return InlineToolExecutor(runner)
    if mode == 'container':
        try:
            from .plugin_container import container_tool_executor_from_env
        except ImportError:
            from plugin_container import container_tool_executor_from_env
        return container_tool_executor_from_env()
    raise ValueError(f'unsupported JOB_EXECUTION_MODE: {mode}')


def job_max_workers_from_env(default=2):
    return _env_int('JOB_MAX_WORKERS', default, 1)
