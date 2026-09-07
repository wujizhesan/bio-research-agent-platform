import os
from pathlib import Path
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
import time
import unittest
from unittest.mock import patch

from src.job_execution import (
    ExecutionLimits,
    InlineToolExecutor,
    JobExecutionCancelled,
    JobExecutionError,
    JobExecutionTimedOut,
    ProcessToolExecutor,
    build_tool_executor_from_env,
    job_max_workers_from_env,
)
from src.job_manager import JobManager
from src.observability import bind_context


HELPER = """import json
from pathlib import Path
import sys
import time
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
arguments = request.get('arguments', {})
time.sleep(float(arguments.get('sleep', 0)))
result = {'status': 'ok', 'value': arguments.get('value'), 'blob': 'x' * int(arguments.get('blob', 0)), 'observability': request.get('observability', {})}
Path(sys.argv[2]).write_text(json.dumps({'ok': True, 'result': result}), encoding='utf-8')
"""


class JobExecutionTests(unittest.TestCase):
    def _executor(self, root, **overrides):
        runner = Path(root) / 'helper.py'
        runner.write_text(HELPER, encoding='utf-8')
        values = {
            'timeout_seconds': 5,
            'memory_limit_mb': 0,
            'cpu_time_seconds': 0,
            'max_result_bytes': 1024 * 1024,
            'poll_interval_seconds': 0.01,
            'terminate_grace_seconds': 1,
        }
        values.update(overrides)
        return ProcessToolExecutor(
            ExecutionLimits(**values),
            python_executable=sys.executable,
            runner_path=runner,
        )

    def test_inline_executor_checks_cancellation(self):
        executor = InlineToolExecutor(lambda _tool, _arguments: {'status': 'ok'})
        with self.assertRaises(JobExecutionCancelled):
            executor.execute('tool', {}, cancelled=lambda: True)

    def test_process_executor_returns_result(self):
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            with bind_context(trace_id='process-trace', job_id='process-job'):
                result = self._executor(raw).execute('tool', {'value': 42})
        self.assertEqual(result['value'], 42)
        self.assertEqual(result['observability']['trace_id'], 'process-trace')
        self.assertEqual(result['observability']['job_id'], 'process-job')

    def test_process_executor_enforces_timeout(self):
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            executor = self._executor(raw, timeout_seconds=0.1)
            with self.assertRaisesRegex(JobExecutionTimedOut, '0.1 seconds'):
                executor.execute('tool', {'sleep': 5})

    def test_process_executor_terminates_cancelled_job(self):
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            executor = self._executor(raw)
            started = time.monotonic()
            with self.assertRaises(JobExecutionCancelled):
                executor.execute(
                    'tool',
                    {'sleep': 5},
                    cancelled=lambda: time.monotonic() - started > 0.1,
                )

    def test_job_manager_cancels_running_process(self):
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            manager = JobManager(max_workers=1, tool_executor=self._executor(raw))
            try:
                submitted = manager.submit('research_catalog', {'sleep': 5})
                for _ in range(100):
                    current = manager.get(submitted['job_id'])
                    if current['status'] == 'running':
                        break
                    time.sleep(0.01)
                manager.cancel(submitted['job_id'])
                for _ in range(200):
                    current = manager.get(submitted['job_id'])
                    if current['status'] == 'cancelled':
                        break
                    time.sleep(0.01)
                self.assertEqual(current['status'], 'cancelled')
            finally:
                manager.shutdown()

    def test_process_executor_rejects_oversized_result(self):
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            executor = self._executor(raw, max_result_bytes=1024)
            with self.assertRaisesRegex(JobExecutionError, 'exceeded 1024 byte limit'):
                executor.execute('tool', {'blob': 4096})

    def test_process_executor_shutdown_terminates_active_child(self):
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            executor = self._executor(raw)
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(
                    executor.execute, 'tool', {'sleep': 30}
                )
                time.sleep(0.2)
                executor.shutdown()
                with self.assertRaises(JobExecutionCancelled):
                    future.result(timeout=5)

    def test_environment_builds_process_limits(self):
        values = {
            'JOB_EXECUTION_MODE': 'process',
            'JOB_TIMEOUT_SECONDS': '90',
            'JOB_MEMORY_LIMIT_MB': '2048',
            'JOB_CPU_TIME_SECONDS': '60',
            'JOB_RESULT_MAX_BYTES': '4096',
            'JOB_MAX_WORKERS': '3',
        }
        with patch.dict(os.environ, values, clear=False):
            executor = build_tool_executor_from_env()
            workers = job_max_workers_from_env()
        self.assertEqual(executor.mode, 'process')
        self.assertEqual(executor.limits.timeout_seconds, 90)
        self.assertEqual(executor.limits.memory_limit_mb, 2048)
        self.assertEqual(executor.limits.cpu_time_seconds, 60)
        self.assertEqual(executor.limits.max_result_bytes, 4096)
        self.assertEqual(workers, 3)

    def test_invalid_execution_mode_is_rejected(self):
        with patch.dict(os.environ, {'JOB_EXECUTION_MODE': 'unknown'}, clear=False):
            with self.assertRaisesRegex(ValueError, 'unsupported JOB_EXECUTION_MODE'):
                build_tool_executor_from_env()


if __name__ == '__main__':
    unittest.main()
