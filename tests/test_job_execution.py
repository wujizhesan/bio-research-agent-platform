import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
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
    public_execution_failure,
)
from src.external_service_policy import ServiceRetryDeferredError
from src import job_subprocess
from src.job_manager import JobManager
from src.observability import bind_context
from src.run_context import bind_run_context, build_run_context
from src.storage_workspace import S3ObjectReference

HELPER = """import json
from pathlib import Path
import sys
import time
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
arguments = request.get('arguments', {})
time.sleep(float(arguments.get('sleep', 0)))
result = {'status': 'ok', 'value': arguments.get('value'), 'blob': 'x' * int(arguments.get('blob', 0)), 'observability': request.get('observability', {}), 'run_context': request.get('run_context')}
Path(sys.argv[2]).write_text(json.dumps({'ok': True, 'result': result}), encoding='utf-8')
"""

INPUT_HELPER = """import json
from pathlib import Path
import sys
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
path = Path(request['arguments']['input_path'])
result = {'path': str(path), 'content': path.read_text(encoding='utf-8')}
Path(sys.argv[2]).write_text(json.dumps({'ok': True, 'result': result}), encoding='utf-8')
"""

DEFERRED_HELPER = """import json
from pathlib import Path
import sys
Path(sys.argv[2]).write_text(json.dumps({
    'ok': False,
    'error_code': 'external_retry_deferred',
    'service': 'uniprot',
    'retry_after_seconds': 120,
    'status_code': 429,
}), encoding='utf-8')
"""


class JobExecutionTests(unittest.TestCase):
    def test_public_execution_failure_hides_internal_details(self):
        failure = public_execution_failure(
            RuntimeError('token=secret-value /srv/private/input.fastq')
        )
        self.assertEqual(failure, {
            'status': 'error',
            'error_code': 'execution_failed',
            'error': 'job execution failed',
        })
        self.assertNotIn('secret-value', str(failure))
        self.assertNotIn('/srv/private', str(failure))

    def test_public_deferred_failure_exposes_only_retry_delay(self):
        failure = public_execution_failure(
            ServiceRetryDeferredError('private-service', 120, 429)
        )
        self.assertEqual(failure, {
            'status': 'error',
            'error_code': 'external_retry_deferred',
            'error': 'external service requested retry later',
            'retry_after_seconds': 120,
        })

    def test_job_subprocess_serializes_deferred_retry(self):
        with tempfile.TemporaryDirectory(prefix='deferred_subprocess_') as raw:
            request_path = Path(raw) / 'request.json'
            response_path = Path(raw) / 'response.json'
            request_path.write_text(json.dumps({
                'tool': 'literature_search',
                'arguments': {},
                'limits': {},
            }), encoding='utf-8')
            with patch(
                'src.domain_registry.run_tool',
                side_effect=ServiceRetryDeferredError('uniprot', 120, 429),
            ):
                code = job_subprocess.main([str(request_path), str(response_path)])
            payload = json.loads(response_path.read_text(encoding='utf-8'))
        self.assertEqual(code, 1)
        self.assertFalse(payload['ok'])
        self.assertEqual(payload['error_code'], 'external_retry_deferred')
        self.assertEqual(payload['retry_after_seconds'], 120)
        self.assertGreaterEqual(payload['telemetry']['registry_import_seconds'], 0)
        self.assertGreaterEqual(payload['telemetry']['tool_run_seconds'], 0)
        self.assertLessEqual(
            payload['telemetry']['process_clock_ns']['module_entry'],
            payload['telemetry']['process_clock_ns']['execution_start'],
        )

    def test_isolated_knowledge_tool_reports_phase_timings(self):
        with tempfile.TemporaryDirectory(prefix='isolated_knowledge_') as raw:
            index = Path(raw) / 'index.json'
            index.write_text(json.dumps({
                'documents': [{'id': 'doc', 'text': 'TP53 tumor suppressor'}],
            }), encoding='utf-8')
            executor = ProcessToolExecutor(ExecutionLimits(
                timeout_seconds=45,
                memory_limit_mb=0,
                cpu_time_seconds=0,
                max_result_bytes=1024 * 1024,
                poll_interval_seconds=0.01,
                terminate_grace_seconds=1,
            ))
            with patch('src.job_execution.log_event') as logged:
                result = executor.execute('knowledge_search', {
                    'query': 'TP53',
                    'index_path': str(index),
                    'top_k': 1,
                })
        self.assertEqual(result['status'], 'ok')
        completed = next(
            call for call in logged.call_args_list
            if call.args[0] == 'tool.execution.completed'
        )
        phases = completed.kwargs['phase_seconds']
        self.assertGreaterEqual(phases['registry_import'], 0)
        self.assertGreaterEqual(phases['tool_run'], 0)
        self.assertGreaterEqual(phases['process_boundary'], 0)
        boundary = completed.kwargs['boundary_seconds']
        self.assertEqual(
            set(boundary),
            {'launch_to_entry', 'module_import', 'result_handoff'},
        )
        self.assertAlmostEqual(
            sum(boundary.values()),
            phases['process_boundary'],
            places=3,
        )

    def test_isolated_knowledge_registry_skips_unrelated_domains(self):
        environment = os.environ.copy()
        environment['BIO_AGENT_EXECUTION_DOMAIN'] = 'knowledge'
        environment['BIO_AGENT_ISOLATED_TOOL_CHILD'] = '1'
        completed = subprocess.run(
            [
                sys.executable,
                '-c',
                'import json, sys; from src import domain_registry; '
                'print(json.dumps({"domains": domain_registry.available_domains(), '
                '"omics_loaded": "src.omics_agent" in sys.modules, '
                '"prometheus_loaded": "prometheus_client" in sys.modules}))',
            ],
            cwd=str(Path(__file__).resolve().parents[1]),
            env=environment,
            capture_output=True,
            text=True,
            check=True,
            timeout=20,
        )
        payload = json.loads(completed.stdout)
        self.assertEqual(payload['domains'], ['knowledge'])
        self.assertFalse(payload['omics_loaded'])
        self.assertFalse(payload['prometheus_loaded'])

    def test_isolated_literature_and_omics_registries_skip_unrelated_domains(self):
        for domain, unrelated in (
            ('literature', 'src.omics_agent'),
            ('omics', 'src.literature_plugin'),
        ):
            with self.subTest(domain=domain):
                environment = os.environ.copy()
                environment['BIO_AGENT_EXECUTION_DOMAIN'] = domain
                environment['BIO_AGENT_ISOLATED_TOOL_CHILD'] = '1'
                completed = subprocess.run(
                    [
                        sys.executable,
                        '-c',
                        'import json, sys; from src import domain_registry; '
                        'print(json.dumps({"domains": domain_registry.available_domains(), '
                        f'"unrelated_loaded": "{unrelated}" in sys.modules, '
                        '"scipy_stats_loaded": "scipy.stats" in sys.modules, '
                        '"pandas_loaded": "pandas" in sys.modules, '
                        '"numpy_loaded": "numpy" in sys.modules}))',
                    ],
                    cwd=str(Path(__file__).resolve().parents[1]),
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=20,
                )
                payload = json.loads(completed.stdout)
                self.assertEqual(payload['domains'], [domain])
                self.assertFalse(payload['unrelated_loaded'])
                if domain == 'omics':
                    self.assertFalse(payload['scipy_stats_loaded'])
                    self.assertFalse(payload['pandas_loaded'])
                    self.assertFalse(payload['numpy_loaded'])

    def test_isolated_literature_and_omics_tools_execute(self):
        executor = ProcessToolExecutor(ExecutionLimits(
            timeout_seconds=45,
            memory_limit_mb=0,
            cpu_time_seconds=0,
            max_result_bytes=1024 * 1024,
            poll_interval_seconds=0.01,
            terminate_grace_seconds=1,
        ))
        literature = executor.execute('literature_summarize', {
            'evidence': {'matches': [{'source': 'fixture'}]},
        })
        omics = executor.execute('omics_inspect_toolchain', {})
        self.assertEqual(literature['status'], 'ok')
        self.assertEqual(literature['result']['n_matches'], 1)
        self.assertIn('fastqc', omics)

    def test_isolated_research_plan_keeps_cross_domain_catalog(self):
        executor = ProcessToolExecutor(ExecutionLimits(
            timeout_seconds=45,
            memory_limit_mb=0,
            cpu_time_seconds=0,
            max_result_bytes=1024 * 1024,
            poll_interval_seconds=0.01,
            terminate_grace_seconds=1,
        ))
        result = executor.execute('research_plan', {
            'task': '分析 RNA-seq 差异表达并设计 mRNA 序列',
        })
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['selected_domains'], ['omics', 'sequence'])

    def test_process_executor_reconstructs_deferred_retry(self):
        with tempfile.TemporaryDirectory(prefix='deferred_executor_') as raw:
            executor = self._executor(raw)
            executor.runner_path.write_text(DEFERRED_HELPER, encoding='utf-8')
            with self.assertRaises(ServiceRetryDeferredError) as raised:
                executor.execute('literature_search', {})
        self.assertEqual(raised.exception.retry_after_seconds, 120)
        self.assertEqual(raised.exception.status_code, 429)

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

    def test_inline_executor_materializes_versioned_input_in_temporary_workspace(self):
        content = b'@read1\nACGT\n'
        digest = hashlib.sha256(content).hexdigest()

        class Client:
            def head_object(self, **_request):
                return {
                    'VersionId': 'version-1',
                    'ContentLength': len(content),
                    'Metadata': {'sha256': digest},
                }

            def download_file(self, _bucket, _key, filename, ExtraArgs=None):
                Path(filename).write_bytes(content)

        client = Client()
        observed = {}

        def run(_tool, arguments):
            path = Path(arguments['input_path'])
            observed['path'] = path
            observed['content'] = path.read_bytes()
            return {'status': 'ok'}

        reference = S3ObjectReference(
            'bio-test',
            'research/file/reads.fastq',
            'version-1',
            digest,
            len(content),
        ).serialize()
        with patch.dict(os.environ, {'S3_BUCKET': 'bio-test', 'S3_PREFIX': 'research'}):
            InlineToolExecutor(run, storage_client=client).execute(
                'tool', {'input_path': reference}
            )
        self.assertEqual(observed['content'], b'@read1\nACGT\n')
        self.assertFalse(observed['path'].exists())

    def test_process_executor_returns_result(self):
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            with bind_context(trace_id='process-trace', job_id='process-job'):
                result = self._executor(raw).execute('tool', {'value': 42})
        self.assertEqual(result['value'], 42)
        self.assertEqual(result['observability']['trace_id'], 'process-trace')
        self.assertEqual(result['observability']['job_id'], 'process-job')

    def test_process_executor_materializes_and_cleans_versioned_input(self):
        content = b'gene,value\nTP53,12\n'
        digest = hashlib.sha256(content).hexdigest()

        class Client:
            def head_object(self, **_request):
                return {
                    'VersionId': 'version-1',
                    'ContentLength': len(content),
                    'Metadata': {'sha256': digest},
                }

            def download_file(self, _bucket, _key, filename, ExtraArgs=None):
                Path(filename).write_bytes(content)

        reference = S3ObjectReference(
            'bio-test',
            'research/file/expression.csv',
            'version-1',
            digest,
            len(content),
        ).serialize()
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            runner = Path(raw) / 'input_helper.py'
            runner.write_text(INPUT_HELPER, encoding='utf-8')
            executor = ProcessToolExecutor(
                ExecutionLimits(
                    timeout_seconds=5,
                    memory_limit_mb=0,
                    cpu_time_seconds=0,
                    max_result_bytes=1024 * 1024,
                    poll_interval_seconds=0.01,
                    terminate_grace_seconds=1,
                ),
                python_executable=sys.executable,
                runner_path=runner,
                storage_client=Client(),
            )
            with patch.dict(os.environ, {'S3_BUCKET': 'bio-test', 'S3_PREFIX': 'research'}):
                result = executor.execute('tool', {'input_path': reference})
        self.assertEqual(result['content'], content.decode('utf-8'))
        self.assertFalse(Path(result['path']).exists())

    def test_process_executor_enforces_timeout(self):
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            executor = self._executor(raw, timeout_seconds=0.1)
            with self.assertRaisesRegex(JobExecutionTimedOut, '0.1 seconds'):
                executor.execute('tool', {'sleep': 5})

    def test_process_executor_propagates_run_context(self):
        with tempfile.TemporaryDirectory(prefix='job_execution_') as raw:
            context = build_run_context(
                'research_catalog',
                {'seed': 9},
                spec={'domain': 'research'},
                job_id='context-job',
            )
            with bind_run_context(context):
                result = self._executor(raw).execute('research_catalog', {})
        self.assertEqual(result['run_context'], context.as_dict())

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
