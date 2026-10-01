import json
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

from src.job_execution import (
    ExecutionLimits, JobExecutionError, ProcessToolExecutor,
    _read_stderr_tail, public_execution_failure,
)


class StderrTailTests(unittest.TestCase):
    def check_tail(self, payload):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'stderr.log'
            path.write_bytes(payload)
            expected = path.read_text(encoding='utf-8', errors='replace')[-2000:].strip()
            self.assertEqual(_read_stderr_tail(path), expected)

    def test_empty_short_and_character_boundaries(self):
        for payload in (b'', b'  \r\n\t ', b'  diagnosis  \r\n', b'x' * 1999, b'x' * 2000, b'x' * 2001):
            with self.subTest(size=len(payload)):
                self.check_tail(payload)

    def test_multibyte_and_newline_boundaries(self):
        for text in ('a', '\u7814', '\U0001f600', 'a\u7814\U0001f600', '\r\n', '\r', '\n', '\U0001f600\r\n\u7814\r'):
            for suffix in ('', 'x', '\u5c3e', '\U0001f9ec', '\r\n  '):
                with self.subTest(text=text, suffix=suffix):
                    self.check_tail((text * 10001 + suffix).encode('utf-8'))

    def test_invalid_utf8_and_incomplete_sequences(self):
        for invalid in (b'\xff', b'\x80\x81\x82', b'\xc0\xaf', b'\xed\xa0\x80', b'\xf4\x90\x80\x80', b'\xe7\xa0', b'\xf0\x9f\x98'):
            for suffix in (b'', b'\r\n  diagnostic\r\n', '\u7ec8\U0001f600'.encode()):
                with self.subTest(invalid=invalid, suffix=suffix):
                    self.check_tail((invalid + b'\r\n') * 3001 + suffix + invalid)

    def test_arbitrary_binary_logs_match_existing_tail(self):
        generator = random.Random(20260930)
        for size in (7999, 8000, 8001, 16001, 65536):
            with self.subTest(size=size):
                self.check_tail(generator.randbytes(size))

    def test_large_log_reads_at_most_eight_thousand_bytes(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'stderr.log'
            with path.open('wb') as destination:
                destination.seek(16 * 1024 * 1024)
                destination.write('\u8bca\u65ad\U0001f600\r\n'.encode())
            requests = []
            actual_reads = []
            original_open = Path.open

            class CountedReader:
                def __init__(self, source):
                    self.source = source

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return self.source.__exit__(*args)

                def seek(self, *args):
                    return self.source.seek(*args)

                def read(self, size):
                    requests.append(size)
                    result = self.source.read(size)
                    actual_reads.append(len(result))
                    return result

            def counted_open(selected, *args, **kwargs):
                source = original_open(selected, *args, **kwargs)
                return CountedReader(source) if selected == path else source

            with patch.object(Path, 'open', counted_open):
                result = _read_stderr_tail(path)
            self.assertEqual(requests, [8000])
            self.assertEqual(actual_reads, [8000])
            self.assertTrue(result.endswith('\u8bca\u65ad\U0001f600'))
            self.assertEqual(len(result), 1999)

    def test_read_errors_propagate(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'missing.log'
            with self.assertRaises(FileNotFoundError):
                _read_stderr_tail(path)
            path.write_bytes(b'diagnostic')
            with patch.object(Path, 'open', side_effect=PermissionError('unreadable')):
                with self.assertRaises(PermissionError):
                    _read_stderr_tail(path)

    def test_real_failed_child_preserves_message_code_and_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = root / 'failed_child.py'
            runner.write_text(
                'import sys\n'
                'block = b"progress line\\r\\n" * 4096\n'
                'for index in range(80):\n'
                '    sys.stderr.buffer.write(block)\n'
                'sys.stderr.buffer.write(bytes.fromhex("' + '\u6700\u7ec8\u8bca\u65ad\uff1a\u5de5\u5177\u5f02\u5e38\u9000\u51fa\U0001f600\r\n'.encode().hex() + '"))\n'
                'raise SystemExit(7)\n',
                encoding='utf-8',
            )
            observed = []
            original = _read_stderr_tail

            def verify(path):
                observed.append(path.parent)
                expected = path.read_text(encoding='utf-8', errors='replace')[-2000:].strip()
                result = original(path)
                self.assertEqual(result, expected)
                return result

            executor = ProcessToolExecutor(
                ExecutionLimits(timeout_seconds=15, memory_limit_mb=0, cpu_time_seconds=0),
                python_executable=sys.executable, runner_path=runner,
            )
            try:
                with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None), patch('src.job_execution._read_stderr_tail', side_effect=verify):
                    with self.assertRaises(JobExecutionError) as raised:
                        executor.execute('stderr_probe', {})
            finally:
                executor.shutdown()
            self.assertTrue(str(raised.exception).startswith('isolated worker exited with code 7: '))
            self.assertTrue(str(raised.exception).endswith('\u6700\u7ec8\u8bca\u65ad\uff1a\u5de5\u5177\u5f02\u5e38\u9000\u51fa\U0001f600'))
            self.assertEqual(raised.exception.error_code, 'execution_failed')
            self.assertEqual(public_execution_failure(raised.exception), {
                'status': 'error', 'error_code': 'execution_failed', 'error': 'job execution failed',
            })
            self.assertEqual(len(observed), 1)
            self.assertFalse(observed[0].exists())
            self.assertFalse(executor._active_processes)

    def test_empty_failed_child_omits_suffix_and_success_does_not_read_tail(self):
        class CompletedProcess:
            returncode = 7

            def poll(self):
                return self.returncode

            def wait(self, timeout=None):
                return self.returncode

        for success in (False, True):
            with self.subTest(success=success):
                roots = []

                def spawn(command, **kwargs):
                    response = Path(command[-1])
                    roots.append(response.parent)
                    if success:
                        response.write_text(json.dumps({'ok': True, 'result': {'value': 42}}), encoding='utf-8')
                    return CompletedProcess()

                executor = ProcessToolExecutor(
                    ExecutionLimits(memory_limit_mb=0, cpu_time_seconds=0), popen_factory=spawn,
                )
                try:
                    with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None), patch('src.job_execution._read_stderr_tail', wraps=_read_stderr_tail) as read:
                        if success:
                            self.assertEqual(executor.execute('stderr_probe', {}), {'value': 42})
                            read.assert_not_called()
                        else:
                            with self.assertRaisesRegex(JobExecutionError, '^isolated worker exited with code 7$'):
                                executor.execute('stderr_probe', {})
                            read.assert_called_once()
                finally:
                    executor.shutdown()
                self.assertTrue(all(not path.exists() for path in roots))


if __name__ == '__main__':
    unittest.main()
