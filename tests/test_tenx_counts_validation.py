import gzip
from pathlib import Path
import subprocess
import sys
import tempfile
import tracemalloc
import unittest
from unittest.mock import patch

import numpy as np
from scipy.sparse import csr_matrix

from scripts import benchmark_tenx_counts_baseline as legacy
from src import omics_qc_executors as current
from src.job_execution import ExecutionLimits, JobExecutionError, ProcessToolExecutor, public_execution_failure


INTEGER_MATRIX = b'%%MatrixMarket matrix coordinate integer general\n4 3 8\n4 3 0\n1 1 1\n1 1 2\n2 1 5\n3 2 7\n4 2 9\n2 3 -2\n2 3 4\n'


def fixture(root, data=INTEGER_MATRIX, compressed=False, mixed_features=False):
    feature_types = ('Gene Expression', 'Antibody Capture', '', 'Gene Expression') if mixed_features else ('',) * 4
    texts = {
        'matrix.mtx': data,
        'barcodes.tsv': b'cell-1\textra\ncell-2\ncell-3\n',
        'features.tsv': ''.join(f'G{number}\t{"MT-" if number % 2 == 0 else "Gene"}{number}\t{kind}\n' for number, kind in enumerate(feature_types)).encode('utf-8'),
    }
    paths = []
    for name, encoded in texts.items():
        path = root / (name + '.gz' if compressed else name)
        path.write_bytes(gzip.compress(encoded, mtime=0) if compressed else encoded)
        paths.append(path)
    return paths


def files(root):
    return {path.name: path.read_bytes() for path in root.iterdir()} if root.exists() else {}


class TenxCountsValidationTests(unittest.TestCase):
    def test_finite_numeric_dtypes_zero_extremes_and_strides_are_accepted_without_changes(self):
        for dtype in (np.bool_, np.int8, np.int64, np.uint64, np.float32, np.float64, np.complex128):
            source = np.array([0, 1, 2, 0, 3, 1], dtype=dtype)
            for data in (source[:0], source, source[::-1], source[::2]):
                with self.subTest(dtype=np.dtype(dtype).name, size=data.size, stride=data.strides):
                    original = data.copy()
                    self.assertIsNone(legacy.validate_counts(data))
                    self.assertIsNone(current._validate_10x_counts(data))
                    np.testing.assert_array_equal(data, original)
        for data in (np.array([0, 2**63 - 1], dtype=np.int64), np.array([0, 2**64 - 1], dtype=np.uint64),
                     np.array([-0.0, np.finfo(np.float64).tiny, np.finfo(np.float64).max])):
            with self.subTest(values=data.tolist()):
                self.assertIsNone(legacy.validate_counts(data))
                self.assertIsNone(current._validate_10x_counts(data))

    def test_negative_nan_and_infinite_counts_at_block_boundaries_keep_errors(self):
        for dtype in (np.float32, np.float64, np.complex128):
            for invalid in (-1, np.nan, np.inf, -np.inf):
                for position in (0, 65535, 65536, 65538):
                    with self.subTest(dtype=np.dtype(dtype).name, invalid=invalid, position=position):
                        data = np.ones(65539, dtype=dtype)
                        data[position] = invalid
                        original = data.copy()
                        for validator in (legacy.validate_counts, current._validate_10x_counts):
                            with np.errstate(all='raise'), self.assertRaisesRegex(ValueError, '^10x counts must be finite and non-negative$'):
                                validator(data)
                        np.testing.assert_array_equal(data, original)
        for invalid in (complex(1, np.nan), complex(1, np.inf)):
            with self.subTest(imaginary=invalid):
                for validator in (legacy.validate_counts, current._validate_10x_counts):
                    with np.errstate(all='raise'), self.assertRaises(ValueError):
                        validator(np.array([invalid]))

    def test_large_validation_keeps_temporary_python_allocations_bounded(self):
        for length in (65535, 65536, 65537, 4 * 1024 * 1024):
            with self.subTest(length=length):
                data = np.ones(length, dtype=np.float64)
                tracemalloc.start()
                try:
                    self.assertIsNone(current._validate_10x_counts(data))
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
                self.assertLess(peak, 150000)
                self.assertTrue((data == 1).all())

    def test_complete_qc_keeps_metrics_and_every_output_byte_for_plain_and_gzip_formats(self):
        matrices = (
            INTEGER_MATRIX,
            b'%%MatrixMarket matrix coordinate real general\n4 3 4\n1 1 -0.0\n2 1 1.25\n3 2 2.5\n4 3 4.75\n',
            b'%%MatrixMarket matrix coordinate pattern general\n4 3 2\n1 1\n3 2\n',
            b'%%MatrixMarket matrix array integer general\n4 3\n1\n0\n2\n0\n0\n3\n0\n4\n0\n0\n0\n0\n',
            b'%%MatrixMarket matrix coordinate integer general\n4 3 0\n',
            INTEGER_MATRIX.replace(b'\n', b'\r\n'),
        )
        thresholds = ({}, {'min_genes': 1, 'max_mito_percent': 20},
                      {'min_counts': 5, 'max_genes': 1}, {'min_genes': 100})
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for number, encoded in enumerate(matrices):
                for compressed in (False, True):
                    paths = fixture(root, encoded, compressed, mixed_features=bool(number % 2))
                    output = root / 'qc'
                    for parameters in thresholds:
                        with self.subTest(matrix=number, gzip=compressed, thresholds=parameters):
                            expected = legacy.run_single_cell_10x_qc(*paths, output, **parameters)
                            before = files(output)
                            observed = current.run_single_cell_10x_qc(*paths, output, **parameters)
                            self.assertEqual(observed, expected)
                            self.assertEqual(files(output), before)

    def test_validation_and_input_errors_preserve_existing_outputs_and_error_order(self):
        cases = ('negative', 'nan', 'inf', 'negative_in_excluded_feature', 'barcode_count',
                 'feature_count', 'duplicate_barcodes', 'missing_matrix', 'bad_matrix',
                 'truncated_gzip', 'min_genes', 'max_genes', 'min_counts', 'mito_negative', 'mito_high')
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for case in cases:
                with self.subTest(case=case):
                    paths = fixture(root, compressed=case == 'truncated_gzip', mixed_features=True)
                    parameters = {}
                    if case in ('negative', 'nan', 'inf', 'negative_in_excluded_feature'):
                        value = {'negative': '-1', 'nan': 'nan', 'inf': 'inf', 'negative_in_excluded_feature': '-1'}[case]
                        row = 2 if case == 'negative_in_excluded_feature' else 1
                        paths[0].write_text(f'%%MatrixMarket matrix coordinate real general\n4 3 1\n{row} 1 {value}\n', encoding='utf-8')
                    elif case == 'barcode_count':
                        paths[1].write_bytes(b'cell-1\n')
                    elif case == 'feature_count':
                        paths[2].write_bytes(b'G1\tGene1\n')
                    elif case == 'duplicate_barcodes':
                        paths[1].write_bytes(b'cell-1\ncell-1\ncell-3\n')
                    elif case == 'missing_matrix':
                        paths[0] = root / 'missing.mtx'
                    elif case == 'bad_matrix':
                        paths[0].write_bytes(b'not MatrixMarket\n')
                    elif case == 'truncated_gzip':
                        paths[0].write_bytes(paths[0].read_bytes()[:-5])
                    else:
                        parameters = {'max_mito_percent': -1 if case == 'mito_negative' else 101} if case.startswith('mito_') else {case: 'invalid'}
                    output = root / 'existing'
                    output.mkdir(exist_ok=True)
                    marker = output / 'single_cell_10x_qc.json'
                    marker.write_bytes(b'keep previous outputs\n')
                    before = files(output)
                    errors = []
                    for module in (legacy, current):
                        try:
                            module.run_single_cell_10x_qc(*paths, output, **parameters)
                        except Exception as exc:
                            errors.append((type(exc), str(exc)))
                        else:
                            self.fail('invalid 10x input was accepted')
                        self.assertEqual(files(output), before)
                    self.assertEqual(errors[0], errors[1])

    def test_sparse_qc_does_not_densify_the_matrix(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = fixture(root)
            with (
                patch.object(csr_matrix, 'toarray', side_effect=AssertionError('dense matrix allocation')),
                patch.object(csr_matrix, 'todense', side_effect=AssertionError('dense matrix allocation')),
            ):
                result = current.run_single_cell_10x_qc(*paths, root / 'qc')
            self.assertEqual(result['metrics']['n_cells_input'], 3)
            self.assertEqual(result['metrics']['n_cells_passed'], 3)

    def test_real_child_results_private_errors_and_execution_workspace_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = fixture(root)
            runner = root / 'child.py'
            repository = str(Path(__file__).resolve().parents[1])
            runner.write_text('import json\nfrom pathlib import Path\nimport sys\n'
                              f'sys.path.insert(0, {repository!r})\n'
                              'from src.omics_qc_executors import run_single_cell_10x_qc\n'
                              'from src.job_subprocess import _write_process_response\n'
                              'request=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))\n'
                              'try:\n    result=run_single_cell_10x_qc(**request["arguments"])\n'
                              '    payload={"ok":True,"result":result}\n    code=0\n'
                              'except Exception as exc:\n    payload={"ok":False,"error":str(exc)}\n    code=1\n'
                              'raise SystemExit(_write_process_response(payload,request,Path(sys.argv[2]),code))\n', encoding='utf-8')
            roots = []

            def spawn(command, **kwargs):
                roots.append(Path(command[-1]).parent)
                return subprocess.Popen(command, **kwargs)

            executor = ProcessToolExecutor(ExecutionLimits(timeout_seconds=20, memory_limit_mb=0, cpu_time_seconds=0),
                                           python_executable=sys.executable, runner_path=runner, popen_factory=spawn)
            arguments = dict(zip(('matrix_mtx', 'barcodes_tsv', 'features_tsv'), map(str, paths)))
            arguments['output_dir'] = str(root / 'qc')
            try:
                with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                    expected = legacy.run_single_cell_10x_qc(**arguments)
                    before = files(root / 'qc')
                    self.assertEqual(executor.execute('tenx_probe', arguments), expected)
                    self.assertEqual(files(root / 'qc'), before)
                    paths[0].write_bytes(b'%%MatrixMarket matrix coordinate integer general\n4 3 1\n1 1 -1\n')
                    with self.assertRaisesRegex(JobExecutionError, 'finite and non-negative') as caught:
                        executor.execute('tenx_probe', arguments)
                    self.assertEqual(public_execution_failure(caught.exception), {'status': 'error', 'error_code': 'execution_failed', 'error': 'job execution failed'})
            finally:
                executor.shutdown()
            self.assertEqual(len(roots), 2)
            self.assertTrue(all(not path.exists() for path in roots))
            self.assertFalse(executor._active_processes)
