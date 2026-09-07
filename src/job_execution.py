"""Isolated execution for research tools."""

from dataclasses import dataclass
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Event, Lock
from time import monotonic, sleep

try:
    from .observability import (
        TOOL_DURATION,
        TOOL_EXECUTIONS,
        current_context,
        log_event,
    )
    from .run_context import current_run_context
except ImportError:
    from observability import (
        TOOL_DURATION,
        TOOL_EXECUTIONS,
        current_context,
        log_event,
    )
    from run_context import current_run_context


class JobExecutionError(RuntimeError):
    pass


class JobExecutionCancelled(JobExecutionError):
    pass


class JobExecutionTimedOut(JobExecutionError):
    pass


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


def _sandbox_environment(tool, temporary_root):
    try:
        from .domain_registry import active_tool_specs
        from .plugin_security import sandbox_environment
    except ImportError:
        from domain_registry import active_tool_specs
        from plugin_security import sandbox_environment
    spec = next(
        (item for item in active_tool_specs() if item['name'] == tool),
        None,
    )
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

    def __init__(self, runner):
        self.runner = runner

    def execute(self, tool, arguments, *, cancelled=None, heartbeat=None):
        if cancelled and cancelled():
            raise JobExecutionCancelled('job cancelled by user')
        if heartbeat:
            heartbeat()
        result = self.runner(tool, arguments)
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

    def __init__(self, limits=None, python_executable=None, runner_path=None, popen_factory=None):
        self.limits = limits or ExecutionLimits.from_env()
        self.python_executable = python_executable or sys.executable
        self.runner_path = Path(runner_path or Path(__file__).with_name('job_subprocess.py'))
        self.popen_factory = popen_factory or subprocess.Popen
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
            child_environment = _sandbox_environment(tool, root)
            request_path.write_text(
                json.dumps(
                    {
                        'tool': tool,
                        'arguments': arguments,
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
                try:
                    duration = max(float(telemetry.get('duration_seconds')), 0)
                except (TypeError, ValueError):
                    duration = None
                if duration is not None:
                    TOOL_DURATION.labels(domain, metric_tool).observe(duration)
                log_event(
                    'tool.execution.completed',
                    domain=domain,
                    tool=metric_tool,
                    status=outcome,
                    duration_seconds=duration,
                    execution_mode='process',
                )
            if not payload.get('ok'):
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
