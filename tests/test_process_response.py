import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.job_execution import (
    ExecutionLimits, JobExecutionError, ProcessToolExecutor,
    _read_process_response, public_execution_failure,
)


class CountedResponse:
    def __init__(self, source, on_read=None):
        self.source = source
        self.requests = []
        self.actual_reads = []
        self.on_read = on_read

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.source.__exit__(*args)

    def fileno(self):
        return self.source.fileno()

    def read(self, size):
        if self.on_read:
            callback, self.on_read = self.on_read, None
            callback()
        self.requests.append(size)
        value = self.source.read(size)
        self.actual_reads.append(len(value))
        return value


class ProcessResponseTests(unittest.TestCase):
    def test_valid_results_preserve_values_and_exact_utf8_boundaries(self):
        for value in (None, True, 42, ['research', '\u7814\u7a76', '\U0001f9ec'], {'nested': '\u8868\u60c5\U0001f600\n\r\t'}):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / 'response.json'
                payload = {'ok': True, 'result': value, 'telemetry': {'duration_seconds': 0.25}}
                encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                path.write_bytes(encoded)
                self.assertEqual(_read_process_response(path, len(encoded)), payload)
                with self.assertRaisesRegex(JobExecutionError, f'exceeded {len(encoded) - 1} byte limit'):
                    _read_process_response(path, len(encoded) - 1)

    def test_invalid_utf8_json_and_top_level_values_are_execution_errors(self):
        for encoded in (b'', b'{', b'{"ok":\xff}', b'\xef\xbb\xbf{}', b'\xff\xfe{\x00}', b'[]', b'null', b'true', b'42', b'"result"'):
            with self.subTest(encoded=encoded), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / 'response.json'
                path.write_bytes(encoded)
                with self.assertRaisesRegex(JobExecutionError, '^isolated worker returned an invalid response$') as raised:
                    _read_process_response(path, 1024)
                self.assertEqual(raised.exception.error_code, 'execution_failed')

    def test_bom_and_universal_newlines_keep_existing_json_semantics(self):
        for newline in ('\r\n', '\r', '\n'):
            with self.subTest(newline=newline), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / 'response.json'
                payload = ('{' + newline + '"ok":true,' + newline + '"result":"\\r\\n\u7814\u7a76\U0001f600"' + newline + '}').encode('utf-8')
                path.write_bytes(payload)
                expected = json.loads(path.read_text(encoding='utf-8'))
                self.assertEqual(_read_process_response(path, len(payload)), expected)

    def counted_open(self, path, holders, on_read=None):
        original_open = Path.open

        def tracked(selected, *args, **kwargs):
            source = original_open(selected, *args, **kwargs)
            mode = args[0] if args else kwargs.get('mode', 'r')
            if selected == path and mode == 'rb':
                wrapper = CountedResponse(source, on_read=on_read)
                holders.append(wrapper)
                return wrapper
            return source

        return patch.object(Path, 'open', tracked)

    def test_known_oversize_is_rejected_without_reading(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'response.json'
            path.write_bytes(b'x' * 2048)
            holders = []
            with self.counted_open(path, holders):
                with self.assertRaisesRegex(JobExecutionError, 'exceeded 1024 byte limit'):
                    _read_process_response(path, 1024)
            self.assertEqual(holders[0].requests, [])
            self.assertTrue(holders[0].source.closed)

    def test_small_result_does_not_allocate_the_entire_quota(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'response.json'
            encoded = b'{"ok":true,"result":42}'
            path.write_bytes(encoded)
            holders = []
            with self.counted_open(path, holders):
                self.assertEqual(_read_process_response(path, 16 * 1024 * 1024), {'ok': True, 'result': 42})
            self.assertEqual(holders[0].requests, [len(encoded) + 1])
            self.assertEqual(holders[0].actual_reads, [len(encoded)])
            self.assertTrue(holders[0].source.closed)

    def test_growth_after_fstat_keeps_actual_reads_bounded(self):
        for quota, size in ((1024, 1024 * 1024), (65536, 131072)):
            with self.subTest(quota=quota), tempfile.TemporaryDirectory() as raw:
                path = Path(raw) / 'response.json'
                path.write_bytes(b'{"ok":true,"result":{}}')
                grown = b'{"ok":true,"result":"' + b'x' * size + b'"}'
                holders = []
                original_fstat = os.fstat
                changed = False

                def grow(descriptor):
                    nonlocal changed
                    result = original_fstat(descriptor)
                    if holders and descriptor == holders[0].fileno() and not changed:
                        changed = True
                        path.write_bytes(grown)
                    return result

                with self.counted_open(path, holders), patch('src.job_execution.os.fstat', side_effect=grow):
                    with self.assertRaisesRegex(JobExecutionError, f'exceeded {quota} byte limit'):
                        _read_process_response(path, quota)
                self.assertTrue(changed)
                self.assertEqual(sum(holders[0].actual_reads), quota + 1)
                self.assertTrue(all(0 < request <= quota + 1 for request in holders[0].requests))
                self.assertTrue(holders[0].source.closed)

    def test_growth_within_quota_is_fully_decoded(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'response.json'
            original = b'{"ok":true,"result":0}'
            payload = {'ok': True, 'result': {'text': '\u7814\u7a76\U0001f9ec' * 3000}}
            encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            path.write_bytes(original)
            holders = []
            with self.counted_open(path, holders, on_read=lambda: path.write_bytes(encoded)):
                self.assertEqual(_read_process_response(path, len(encoded)), payload)
            self.assertEqual(sum(holders[0].actual_reads), len(encoded))
            self.assertEqual(holders[0].requests[0], len(original) + 1)
            self.assertTrue(holders[0].source.closed)

    def test_file_errors_are_normalized_and_reader_is_closed(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'missing.json'
            with self.assertRaisesRegex(JobExecutionError, 'invalid response') as raised:
                _read_process_response(path, 1024)
            self.assertIsInstance(raised.exception.__cause__, FileNotFoundError)
            path.write_bytes(b'{}')
            holders = []

            def fail():
                raise OSError('read failed')

            with self.counted_open(path, holders, on_read=fail):
                with self.assertRaisesRegex(JobExecutionError, 'invalid response') as raised:
                    _read_process_response(path, 1024)
            self.assertIsInstance(raised.exception.__cause__, OSError)
            self.assertTrue(holders[0].source.closed)

    def test_executor_rejects_growing_result_and_cleans_workspace(self):
        class CompletedProcess:
            returncode = 0

            def poll(self):
                return 0

            def wait(self, timeout=None):
                return 0

        observed = []
        original_fstat = os.fstat
        changed = False

        def spawn(command, **kwargs):
            path = Path(command[-1])
            observed.append(path)
            path.write_bytes(b'{"ok":true,"result":0}')
            return CompletedProcess()

        def grow(descriptor):
            nonlocal changed
            result = original_fstat(descriptor)
            if not changed:
                changed = True
                observed[0].write_bytes(b'{"ok":true,"result":"' + b'x' * 65536 + b'"}')
            return result

        executor = ProcessToolExecutor(
            ExecutionLimits(memory_limit_mb=0, cpu_time_seconds=0, max_result_bytes=1024),
            popen_factory=spawn,
        )
        try:
            with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None), patch('src.job_execution.os.fstat', side_effect=grow):
                with self.assertRaisesRegex(JobExecutionError, 'exceeded 1024 byte limit') as raised:
                    executor.execute('response_probe', {})
            self.assertEqual(public_execution_failure(raised.exception), {
                'status': 'error', 'error_code': 'execution_failed', 'error': 'job execution failed',
            })
        finally:
            executor.shutdown()
        self.assertTrue(changed)
        self.assertEqual(len(observed), 1)
        self.assertFalse(observed[0].parent.exists())
        self.assertFalse(executor._active_processes)

    def test_real_child_utf8_response_and_malformed_response(self):
        for payload in ({'ok': True, 'result': '\u7814\u7a76\U0001f9ec'}, [], None):
            with self.subTest(payload=payload), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                runner = root / 'response_child.py'
                encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                runner.write_text(
                    'from pathlib import Path\nimport sys\n'
                    'Path(sys.argv[2]).write_bytes(bytes.fromhex("' + encoded.hex() + '"))\n',
                    encoding='utf-8',
                )
                executor = ProcessToolExecutor(
                    ExecutionLimits(timeout_seconds=15, memory_limit_mb=0, cpu_time_seconds=0),
                    python_executable=sys.executable, runner_path=runner,
                )
                try:
                    with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                        if isinstance(payload, dict):
                            self.assertEqual(executor.execute('response_probe', {}), payload['result'])
                        else:
                            with self.assertRaisesRegex(JobExecutionError, 'invalid response'):
                                executor.execute('response_probe', {})
                finally:
                    executor.shutdown()
                self.assertFalse(executor._active_processes)


if __name__ == '__main__':
    unittest.main()
