import json
from pathlib import Path
import subprocess
import sys
import tempfile
from types import ModuleType
import unittest
from unittest.mock import patch

from scripts.benchmark_response_write_baseline import legacy_write_process_response
from src import job_subprocess
from src.job_execution import ExecutionLimits, JobExecutionError, ProcessToolExecutor, public_execution_failure


class ResponseWriteTests(unittest.TestCase):
    def compare_writers(self, root, payload, request, exit_code=0):
        outputs = []
        for name, writer in (('legacy', legacy_write_process_response), ('reused', job_subprocess._write_process_response)):
            path = root / (name + '.json')
            path.write_bytes(b'old response' * 1000)
            code = writer(payload, request, path, exit_code)
            outputs.append((code, path.read_bytes()))
        self.assertEqual(outputs[0], outputs[1])
        return outputs[1]

    def test_success_values_default_str_and_telemetry_preserve_bytes(self):
        values = (None, True, 42, -0.0, ['research', '\u7814\u7a76', '\U0001f9ec'],
                  {'nested': '\r\n\t"\\\u8868\u60c5\U0001f600'},
                  Path('research/input.csv'), b'raw', (1, 2), {'count': float('inf')})
        with tempfile.TemporaryDirectory() as raw:
            for value in values:
                with self.subTest(value=value):
                    payload = {'ok': True, 'result': value, 'telemetry': {'duration_seconds': 0.25, 'tool': '\u7814\u7a76'}}
                    code, encoded = self.compare_writers(Path(raw), payload, {'limits': {'max_result_bytes': 4096}})
                    self.assertEqual(code, 0)
                    self.assertEqual(encoded, json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8'))

    def test_exact_utf8_quota_and_oversize_error_preserve_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for value in ('ascii', '\u7814\u7a76\U0001f9ec', '\n\r\t"\\'):
                payload = {'ok': True, 'result': value}
                size = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
                for quota in (size, size - 1):
                    with self.subTest(value=value, quota=quota):
                        code, encoded = self.compare_writers(root, payload, {'limits': {'max_result_bytes': quota}})
                        self.assertEqual(code, int(quota < size))
                        expected = payload if quota == size else {'ok': False, 'error': f'job result exceeded {quota} byte limit'}
                        self.assertEqual(json.loads(encoded), expected)

    def test_disabled_and_coerced_limits_keep_existing_behavior(self):
        requests = ({}, {'limits': {}}, {'limits': {'max_result_bytes': None}},
                    {'limits': {'max_result_bytes': 0}}, {'limits': {'max_result_bytes': ''}},
                    {'limits': {'max_result_bytes': '4096'}}, {'limits': {'max_result_bytes': -1}})
        with tempfile.TemporaryDirectory() as raw:
            for request in requests:
                with self.subTest(request=request):
                    self.compare_writers(Path(raw), {'ok': True, 'result': '\u7814\u7a76\U0001f9ec'}, request)

    def test_failure_payload_and_exit_code_are_preserved(self):
        payload = {'ok': False, 'error': '\u5de5\u5177\u5931\u8d25', 'type': 'RuntimeError', 'traceback': 'line\nnext',
                   'error_code': 'external_retry_deferred', 'retry_after_seconds': 120, 'telemetry': {'status': 'error'}}
        with tempfile.TemporaryDirectory() as raw:
            for quota in (4096, 64):
                with self.subTest(quota=quota):
                    code, encoded = self.compare_writers(Path(raw), payload, {'limits': {'max_result_bytes': quota}}, 1)
                    self.assertEqual(code, 1)
                    self.assertFalse(json.loads(encoded)['ok'])

    def test_encoding_and_configuration_failures_keep_exception_and_file_state(self):
        circular = {}
        circular['cycle'] = circular
        cases = (({'result': circular}, 4096), ({'result': {('tuple',): 1}}, 4096),
                 ({'result': '\ud800'}, 4096), ({'result': '\ud800'}, 0),
                 ({'result': '\ud800'}, 'invalid'), ({'result': 42}, 'invalid'))
        with tempfile.TemporaryDirectory() as raw:
            for index, (payload, quota) in enumerate(cases):
                with self.subTest(index=index):
                    observed = []
                    for name, writer in (('legacy', legacy_write_process_response), ('reused', job_subprocess._write_process_response)):
                        path = Path(raw) / f'{index}-{name}.json'
                        with self.assertRaises((ValueError, TypeError, UnicodeEncodeError)) as raised:
                            writer(payload, {'limits': {'max_result_bytes': quota}}, path, 0)
                        observed.append((type(raised.exception), str(raised.exception), path.read_bytes() if path.exists() else None))
                    self.assertEqual(observed[0], observed[1])

    def test_io_failures_are_not_hidden(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for path in (root / 'missing' / 'response.json', root):
                for quota in (0, 4096):
                    with self.subTest(path=path, quota=quota):
                        errors = []
                        for writer in (legacy_write_process_response, job_subprocess._write_process_response):
                            with self.assertRaises(OSError) as raised:
                                writer({'ok': True, 'result': 42}, {'limits': {'max_result_bytes': quota}}, path, 0)
                            errors.append(type(raised.exception))
                        self.assertEqual(errors[0], errors[1])

    def test_main_keeps_telemetry_status_and_quota_error(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            request_path = root / 'request.json'
            for quota, value in ((0, '\u7814\u7a76\U0001f9ec'), (4096, '\u7814\u7a76\U0001f9ec'), (1024, 'x' * 4096)):
                with self.subTest(quota=quota):
                    request_path.write_text(json.dumps({'tool': 'research_probe', 'arguments': {}, 'limits': {'max_result_bytes': quota}}), encoding='utf-8')
                    outputs = []
                    registry = ModuleType('src.domain_registry')
                    registry.run_tool = lambda *_arguments: {'status': 'ok', 'value': value}
                    for name, writer in (('legacy', legacy_write_process_response), ('reused', job_subprocess._write_process_response)):
                        response_path = root / (name + '.json')
                        with patch.dict(sys.modules, {'src.domain_registry': registry}), patch.object(job_subprocess, '_write_process_response', writer), patch.object(job_subprocess, 'monotonic_ns', side_effect=[100, 300]), patch.object(job_subprocess, '_MODULE_ENTRY_NS', 50), patch.object(job_subprocess, 'perf_counter', side_effect=[1.0, 2.0, 3.0, 5.0]):
                            code = job_subprocess.main([str(request_path), str(response_path)])
                        outputs.append((code, response_path.read_bytes()))
                    self.assertEqual(outputs[0], outputs[1])
                    payload = json.loads(outputs[1][1])
                    if quota != 1024:
                        self.assertEqual(payload['telemetry']['status'], 'success')
                        self.assertEqual(payload['telemetry']['process_clock_ns'], {'module_entry': 50, 'execution_start': 100, 'execution_finished': 300})
                    else:
                        self.assertEqual(payload, {'ok': False, 'error': 'job result exceeded 1024 byte limit'})

    def test_real_child_parent_result_error_and_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            runner = Path(raw) / 'writer_child.py'
            repository = str(Path(__file__).resolve().parents[1])
            runner.write_text(
                'import json\nfrom pathlib import Path\nimport sys\n'
                f'sys.path.insert(0, {repository!r})\n'
                'from src.job_subprocess import _write_process_response\n'
                'request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))\n'
                'payload = {"ok": True, "result": request["arguments"]["value"]}\n'
                'raise SystemExit(_write_process_response(payload, request, Path(sys.argv[2]), 0))\n', encoding='utf-8',
            )
            roots = []

            def spawn(command, **kwargs):
                roots.append(Path(command[-1]).parent)
                return subprocess.Popen(command, **kwargs)

            executor = ProcessToolExecutor(ExecutionLimits(timeout_seconds=15, memory_limit_mb=0, cpu_time_seconds=0, max_result_bytes=1024),
                                           python_executable=sys.executable, runner_path=runner, popen_factory=spawn)
            try:
                with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                    self.assertEqual(executor.execute('response_probe', {'value': '\u7814\u7a76\U0001f9ec'}), '\u7814\u7a76\U0001f9ec')
                    with self.assertRaisesRegex(JobExecutionError, 'job result exceeded 1024 byte limit') as raised:
                        executor.execute('response_probe', {'value': 'x' * 4096})
                    self.assertEqual(public_execution_failure(raised.exception), {'status': 'error', 'error_code': 'execution_failed', 'error': 'job execution failed'})
            finally:
                executor.shutdown()
            self.assertEqual(len(roots), 2)
            self.assertTrue(all(not root.exists() for root in roots))
            self.assertFalse(executor._active_processes)


if __name__ == '__main__':
    unittest.main()
