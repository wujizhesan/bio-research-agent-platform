import gzip
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from scripts.benchmark_fastq_quality_baseline import legacy_fastq_file_stats
from src import omics_fastq_qc
from src.job_execution import ExecutionLimits, JobExecutionError, ProcessToolExecutor, public_execution_failure


class FastqQualityTests(unittest.TestCase):
    def test_all_ascii_values_and_combinations_keep_clamped_scores(self):
        self.assertEqual([omics_fastq_qc._phred33_sum(chr(value)) for value in range(128)],
                         [max(0, value - 33) for value in range(128)])
        for quality in ('', '!', '\x00 \t!', '~\x7f', 'I' * 150, ''.join(map(chr, range(128))) * 100):
            with self.subTest(length=len(quality)):
                self.assertEqual(omics_fastq_qc._phred33_sum(quality), sum(max(0, ord(char) - 33) for char in quality))

    def test_non_ascii_and_surrogate_values_keep_existing_scores(self):
        for quality in ('\u7814', '\U0001f9ec', '\u03a9', '\ufffd', '\ud800', '!I\u7814\U0001f9ec\x00'):
            with self.subTest(quality=quality):
                self.assertEqual(omics_fastq_qc._phred33_sum(quality), sum(max(0, ord(char) - 33) for char in quality))

    def test_long_quality_chunks_preserve_scores_and_bound_temporary_allocations(self):
        for length in (65535, 65536, 65537, 1024 * 1024):
            with self.subTest(length=length):
                quality = ('\x00 !IJ~\x7f' * ((length + 6) // 7))[:length]
                expected = sum(max(0, ord(char) - 33) for char in quality)
                tracemalloc.start()
                try:
                    observed = omics_fastq_qc._phred33_sum(quality)
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertEqual(observed, expected)
                self.assertLess(peak, 300000)

    def test_long_read_plain_and_gzip_stats_preserve_values_and_memory(self):
        with tempfile.TemporaryDirectory() as raw:
            length = 1024 * 1024
            encoded = b'@long\n' + b'A' * length + b'\n+\n' + b'I' * length + b'\n'
            for compressed in (False, True):
                with self.subTest(gzip=compressed):
                    path = Path(raw) / ('long.fastq.gz' if compressed else 'long.fastq')
                    path.write_bytes(gzip.compress(encoded, mtime=0) if compressed else encoded)
                    outputs, peaks = [], []
                    for parser in (legacy_fastq_file_stats, omics_fastq_qc._fastq_file_stats):
                        tracemalloc.start()
                        try:
                            outputs.append(parser(path))
                            peaks.append(tracemalloc.get_traced_memory()[1])
                        finally:
                            tracemalloc.stop()
                    self.assertEqual(outputs[0], outputs[1])
                    self.assertLessEqual(peaks[1], peaks[0] + 65536)

    def test_plain_and_gzip_stats_match_for_newlines_encoding_and_empty_files(self):
        cases = (b'', b'@a\nACGT\n+\n!"#I\n@b\nG\n+\n~\n',
                 b'@control\nACGT\n+\n\x00 \t\x7f\n',
                 '@unicode\nAC\n+\n\u7814\U0001f9ec\n'.encode('utf-8'),
                 b'@invalid_utf8\nAC\n+\n\xff\xfe\n', b'@final\nAC\n+\nII')
        with tempfile.TemporaryDirectory() as raw:
            for index, encoded in enumerate(cases):
                for newline in (b'\n', b'\r\n', b'\r'):
                    for compressed in (False, True):
                        with self.subTest(case=index, newline=newline, gzip=compressed):
                            path = Path(raw) / ('reads.FASTQ.GZ' if compressed else 'reads.fastq')
                            data = encoded.replace(b'\n', newline)
                            path.write_bytes(gzip.compress(data, mtime=0) if compressed else data)
                            self.assertEqual(omics_fastq_qc._fastq_file_stats(path), legacy_fastq_file_stats(path))

    def test_record_validation_errors_keep_type_and_message(self):
        cases = (b'#read\nAC\n+\nII\n', b'@read\n\n+\nI\n', b'@read\nAC\n-\nII\n',
                 b'@read\nAC\n+\n', b'@read\nAC\n+\nI\n', b'@ok\nA\n+\nI\n@truncated\n')
        with tempfile.TemporaryDirectory() as raw:
            for encoded in cases:
                for compressed in (False, True):
                    with self.subTest(encoded=encoded, gzip=compressed):
                        path = Path(raw) / ('reads.fastq.gz' if compressed else 'reads.fastq')
                        path.write_bytes(gzip.compress(encoded, mtime=0) if compressed else encoded)
                        errors = []
                        for parser in (legacy_fastq_file_stats, omics_fastq_qc._fastq_file_stats):
                            with self.assertRaises(ValueError) as raised:
                                parser(path)
                            errors.append((type(raised.exception), str(raised.exception)))
                        self.assertEqual(errors[0], errors[1])

    def test_corrupt_and_truncated_gzip_keep_errors(self):
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'reads.fastq.gz'
            for encoded in (b'not gzip', gzip.compress(b'@read\nAC\n+\nII\n', mtime=0)[:-8]):
                with self.subTest(encoded=encoded):
                    path.write_bytes(encoded)
                    errors = []
                    for parser in (legacy_fastq_file_stats, omics_fastq_qc._fastq_file_stats):
                        with self.assertRaises((OSError, EOFError)) as raised:
                            parser(path)
                        errors.append((type(raised.exception), str(raised.exception)))
                    self.assertEqual(errors[0], errors[1])

    def test_multiple_file_qc_metrics_and_manifest_bytes_match(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            first, second = root / 'first.fastq', root / 'second.fastq.gz'
            first.write_bytes(b'@a\nACGT\n+\n!"#I\n')
            second.write_bytes(gzip.compress('@b\nAC\n+\n\u7814\u03a9\n'.encode('utf-8'), mtime=0))
            output = root / 'qc'
            with patch.object(omics_fastq_qc, '_fastq_file_stats', legacy_fastq_file_stats):
                expected = omics_fastq_qc.execute_genomics_qc([first, second], output, dependencies=SimpleNamespace())
            expected_bytes = (output / 'genomics_qc.json').read_bytes()
            actual = omics_fastq_qc.execute_genomics_qc([first, second], output, dependencies=SimpleNamespace())
            self.assertEqual(actual, expected)
            self.assertEqual((output / 'genomics_qc.json').read_bytes(), expected_bytes)

    def test_real_child_qc_result_error_and_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            runner = root / 'qc_child.py'
            repository = str(Path(__file__).resolve().parents[1])
            runner.write_text(
                'import json\nfrom pathlib import Path\nimport sys\nfrom types import SimpleNamespace\n'
                f'sys.path.insert(0, {repository!r})\n'
                'from src.omics_fastq_qc import execute_genomics_qc\n'
                'from src.job_subprocess import _write_process_response\n'
                'request=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))\n'
                'try:\n'
                '    result=execute_genomics_qc(**request["arguments"], dependencies=SimpleNamespace())\n'
                '    payload={"ok":True,"result":result}\n    code=0\n'
                'except Exception as exc:\n'
                '    payload={"ok":False,"error":str(exc)}\n    code=1\n'
                'raise SystemExit(_write_process_response(payload, request, Path(sys.argv[2]), code))\n', encoding='utf-8',
            )
            roots = []

            def spawn(command, **kwargs):
                roots.append(Path(command[-1]).parent)
                return subprocess.Popen(command, **kwargs)

            executor = ProcessToolExecutor(ExecutionLimits(timeout_seconds=15, memory_limit_mb=0, cpu_time_seconds=0),
                                           python_executable=sys.executable, runner_path=runner, popen_factory=spawn)
            try:
                with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                    for compressed in (False, True):
                        with self.subTest(gzip=compressed):
                            path = root / ('reads.fastq.gz' if compressed else 'reads.fastq')
                            encoded = b'@a\nACGT\n+\nIIII\n'
                            path.write_bytes(gzip.compress(encoded, mtime=0) if compressed else encoded)
                            arguments = {'input_path': str(path), 'output_dir': str(root / 'qc')}
                            expected = omics_fastq_qc.execute_genomics_qc(**arguments, dependencies=SimpleNamespace())
                            self.assertEqual(executor.execute('qc_probe', arguments), expected)
                    path = root / 'invalid.fastq'
                    path.write_bytes(b'@a\nAC\n+\nI\n')
                    with self.assertRaisesRegex(JobExecutionError, 'sequence/quality length mismatch') as raised:
                        executor.execute('qc_probe', {'input_path': str(path), 'output_dir': str(root / 'qc')})
                    self.assertEqual(public_execution_failure(raised.exception), {'status': 'error', 'error_code': 'execution_failed', 'error': 'job execution failed'})
            finally:
                executor.shutdown()
            self.assertEqual(len(roots), 3)
            self.assertTrue(all(not root.exists() for root in roots))
            self.assertFalse(executor._active_processes)


if __name__ == '__main__':
    unittest.main()
