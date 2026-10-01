import csv
from fractions import Fraction
from itertools import combinations
from math import comb
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd
from scipy.stats import hypergeom

from scripts import benchmark_pathway_batches_baseline as legacy
from src import omics_agent as current
from src.job_execution import ExecutionLimits, JobExecutionError, ProcessToolExecutor, public_execution_failure


def table(path, columns, rows):
    with path.open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target)
        writer.writerow(columns)
        writer.writerows(rows)
    return path


def inputs(root, de_rows, gene_rows):
    return (
        table(root / 'de.csv', ['gene_id', 'padj', 'log2_fc'], de_rows),
        table(root / 'sets.csv', ['pathway_id', 'pathway_name', 'gene_id'], gene_rows),
    )


class PathwayBatchTests(unittest.TestCase):
    def compare(self, paths, output, **parameters):
        expected = legacy.run_pathway_enrichment(*paths, output, **parameters)
        encoded = output.read_bytes()
        observed = current.run_pathway_enrichment(*paths, output, **parameters)
        self.assertEqual(observed, expected)
        self.assertEqual(output.read_bytes(), encoded)
        return observed

    def test_all_small_subsets_match_scalar_and_exact_combinatorial_tails(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for population in range(1, 8):
                genes = [f'G{number}' for number in range(population)]
                subsets = [subset for size in range(1, population + 1) for subset in combinations(genes, size)]
                rows = [(f'P{number:04}', f'通路 {number}', gene) for number, subset in enumerate(subsets) for gene in subset]
                for selected_count in sorted({0, 1, population // 2, population}):
                    with self.subTest(population=population, selected=selected_count):
                        paths = inputs(root, [(gene, 0.05 if number < selected_count else 0.5, -1 if number % 2 else 1) for number, gene in enumerate(genes)], rows)
                        result = self.compare(paths, root / 'result.csv')
                        self.assertEqual(result['n_pathways'], len(subsets))
                        self.assertEqual(result['n_selected_genes'], selected_count)
                        frame = pd.read_csv(root / 'result.csv').set_index('pathway_id')
                        for number, subset in enumerate(subsets):
                            observed = frame.loc[f'P{number:04}']
                            overlap = len(set(subset) & set(genes[:selected_count]))
                            numerator = sum(comb(len(subset), hits) * comb(population - len(subset), selected_count - hits)
                                            for hits in range(overlap, min(len(subset), selected_count) + 1)
                                            if 0 <= selected_count - hits <= population - len(subset))
                            exact = float(Fraction(numerator, comb(population, selected_count)))
                            self.assertAlmostEqual(float(observed['p_value']), exact, places=13)

    def test_thresholds_duplicates_missing_values_and_identifiers_match_complete_outputs(self):
        de_rows = [('G1', 0.05, 1), ('G2', 0.05, -1), ('G3', 0.051, 3), ('G4', 0.01, 0.999),
                   ('G1', 0.001, 5), ('基因,研', 0, 2), ('003', 'nan', 2), ('G5', 0, 'inf')]
        gene_rows = [('Z', 'first name', 'G1'), ('Z', 'later name', 'G1'), ('Z', 'later name', 'G3'),
                     ('A', None, 'G2'), ('A', 'later name', '基因,研'), ('A', 'name', 'outside'),
                     ('only_outside', 'outside', 'missing'), ('B', 'name', None), (None, 'missing id', 'G1')]
        parameters = ({}, {'padj_cutoff': 0}, {'padj_cutoff': -1}, {'abs_log2_fc_cutoff': 0},
                      {'abs_log2_fc_cutoff': 2}, {'padj_cutoff': float('inf')}, {'padj_cutoff': float('nan')})
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for de_values, gene_values in ((de_rows, gene_rows),
                                         ([(1, 0, 2), (2, 0.5, -2), (3, 0, 1)], [(2, 100, 1), (1, 200, 2), (2, 300, 3)]),
                                         ([], [('A', 'outside', 'G1')]),
                                         ([('G1', 0, 2)], []),
                                         ([('G1', 0.5, 2)], [('A', 'name', 'G1')])):
                paths = inputs(root, de_values, gene_values)
                for options in parameters:
                    with self.subTest(de=de_values, sets=gene_values, thresholds=options):
                        self.compare(paths, root / 'result.csv', **options)

    def test_large_pathway_counts_preserve_results_and_bound_scipy_arguments(self):
        original = hypergeom.sf
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for pathways in (0, 1, 1023, 1024, 1025, 4097):
                with self.subTest(pathways=pathways):
                    rows = [(f'P{number:05}', f'path {number}', f'G{number % 4}') for number in range(pathways)]
                    rows += [('outside', 'outside', 'unknown')]
                    paths = inputs(root, [(f'G{number}', 0.01 if number < 2 else 0.5, 2) for number in range(4)], rows)
                    output = root / 'result.csv'
                    expected = legacy.run_pathway_enrichment(*paths, output)
                    encoded = output.read_bytes()
                    sizes = []

                    def bounded(quantiles, population, successes, selected):
                        sizes.append(len(quantiles))
                        self.assertLessEqual(len(quantiles), 1024)
                        self.assertEqual(len(quantiles), len(successes))
                        return original(quantiles, population, successes, selected)

                    with patch.object(hypergeom, 'sf', side_effect=bounded):
                        self.assertEqual(current.run_pathway_enrichment(*paths, output), expected)
                    self.assertEqual(sum(sizes), pathways)
                    self.assertEqual(output.read_bytes(), encoded)

    def test_large_population_extreme_tails_and_full_background_keep_exact_outputs(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            groups = {'zero_tail': range(1000), 'full_background': range(20000),
                      'no_overlap': range(1000, 1100), 'tiny_tail': range(32),
                      'mixed': (*range(200), *range(1000, 1800))}
            paths = inputs(root, [(f'G{number}', 0.01 if number < 1000 else 0.5, 2) for number in range(20000)],
                           [(name, name, f'G{number}') for name, genes in groups.items() for number in genes])
            self.compare(paths, root / 'result.csv')
            frame = pd.read_csv(root / 'result.csv').set_index('pathway_id')
            self.assertEqual(frame.loc['zero_tail', 'p_value'], 0.0)
            self.assertEqual(frame.loc['full_background', 'p_value'], 1.0)
            self.assertEqual(frame.loc['no_overlap', 'p_value'], 1.0)
            self.assertGreater(frame.loc['tiny_tail', 'p_value'], 0)
            self.assertLess(frame.loc['tiny_tail', 'p_value'], 1e-30)

    def test_zero_selected_and_empty_pathway_fast_paths_do_not_call_scipy(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for de_rows, gene_rows in (([], []), ([('G', 0.5, 2)], [('A', 'a', 'G')]),
                                      ([('G', 0.01, 2)], [('A', 'a', 'outside')])):
                with self.subTest(de=de_rows, genes=gene_rows):
                    paths = inputs(root, de_rows, gene_rows)
                    with patch.object(hypergeom, 'sf', side_effect=AssertionError('unnecessary probability calculation')):
                        self.compare(paths, root / 'result.csv')

    def test_input_errors_preserve_previous_csv_and_error_type_and_message(self):
        cases = ('missing_de', 'missing_sets', 'de_columns', 'set_columns', 'bad_padj', 'bad_fc',
                 'padj_cutoff_type', 'fc_cutoff_type', 'empty_de_file', 'empty_set_file')
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for case in cases:
                with self.subTest(case=case):
                    paths = list(inputs(root, [('G', 0.01, 2)], [('A', 'a', 'G')]))
                    parameters = {}
                    if case == 'missing_de':
                        paths[0] = root / 'missing_de.csv'
                    elif case == 'missing_sets':
                        paths[1] = root / 'missing_sets.csv'
                    elif case == 'de_columns':
                        paths[0].write_bytes(b'gene_id\nG\n')
                    elif case == 'set_columns':
                        paths[1].write_bytes(b'pathway_id\nA\n')
                    elif case in ('bad_padj', 'bad_fc'):
                        inputs(root, [('G', 'bad' if case == 'bad_padj' else 0.01, 'bad' if case == 'bad_fc' else 2)], [('A', 'a', 'G')])
                    elif case.endswith('cutoff_type'):
                        parameters = {'padj_cutoff' if case.startswith('padj') else 'abs_log2_fc_cutoff': 'invalid'}
                    elif case == 'empty_de_file':
                        paths[0].write_bytes(b'')
                    else:
                        paths[1].write_bytes(b'')
                    output = root / 'existing.csv'
                    output.write_bytes(b'keep previous results\n')
                    errors = []
                    for module in (legacy, current):
                        try:
                            module.run_pathway_enrichment(*paths, output, **parameters)
                        except Exception as exc:
                            errors.append((type(exc), str(exc)))
                        else:
                            self.fail('invalid input was accepted')
                        self.assertEqual(output.read_bytes(), b'keep previous results\n')
                    self.assertEqual(errors[0], errors[1])

    def test_real_child_preserves_results_private_errors_and_workspace_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            paths = inputs(root, [('G1', 0.01, 2), ('G2', 0.5, -2)], [('A', 'name', 'G1'), ('B', 'name', 'G2')])
            output = root / 'result.csv'
            arguments = {'de_csv': str(paths[0]), 'gene_sets_csv': str(paths[1]), 'output_csv': str(output)}
            expected = legacy.run_pathway_enrichment(**arguments)
            encoded = output.read_bytes()
            runner = root / 'child.py'
            repository = str(Path(__file__).resolve().parents[1])
            runner.write_text('import json\nfrom pathlib import Path\nimport sys\n'
                              f'sys.path.insert(0, {repository!r})\n'
                              'from src.omics_agent import run_pathway_enrichment\n'
                              'from src.job_subprocess import _write_process_response\n'
                              'request=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))\n'
                              'try:\n    result=run_pathway_enrichment(**request["arguments"])\n'
                              '    payload={"ok":True,"result":result}\n    code=0\n'
                              'except Exception as exc:\n    payload={"ok":False,"error":str(exc)}\n    code=1\n'
                              'raise SystemExit(_write_process_response(payload,request,Path(sys.argv[2]),code))\n', encoding='utf-8')
            roots = []

            def spawn(command, **options):
                roots.append(Path(command[-1]).parent)
                return subprocess.Popen(command, **options)

            executor = ProcessToolExecutor(ExecutionLimits(timeout_seconds=20, memory_limit_mb=0, cpu_time_seconds=0),
                                           python_executable=sys.executable, runner_path=runner, popen_factory=spawn)
            try:
                with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                    self.assertEqual(executor.execute('pathway_probe', arguments), expected)
                    self.assertEqual(output.read_bytes(), encoded)
                    paths[0].write_bytes(b'gene_id\nG\n')
                    with self.assertRaises(JobExecutionError) as caught:
                        executor.execute('pathway_probe', arguments)
                    self.assertEqual(public_execution_failure(caught.exception), {'status': 'error', 'error_code': 'execution_failed', 'error': 'job execution failed'})
                    self.assertEqual(output.read_bytes(), encoded)
            finally:
                executor.shutdown()
            self.assertEqual(len(roots), 2)
            self.assertTrue(all(not path.exists() for path in roots))
            self.assertFalse(executor._active_processes)
