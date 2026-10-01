import csv
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import benchmark_pathway_grouping_baseline as legacy
from src import omics_agent as current


def table(path, rows, columns=('pathway_id', 'pathway_name', 'gene_id')):
    with path.open('w', encoding='utf-8', newline='') as target:
        writer = csv.writer(target)
        writer.writerow(columns)
        writer.writerows(rows)
    return path


class PathwayGroupingTests(unittest.TestCase):
    def compare_loader(self, path):
        expected = legacy._load_gene_sets(path)
        observed = current._load_gene_sets(path)
        self.assertEqual(observed, expected)
        self.assertEqual(list(observed), list(expected))
        return observed

    def test_csv_types_missing_rows_first_names_and_implicit_indexes_match(self):
        cases = (
            [],
            [('B', 'first', 'G1'), ('A', 'alpha', 'G2'), ('B', 'ignored', 'G1'), ('B', 'ignored', 'G3')],
            [('B', None, 'G1'), ('B', 'ignored', 'G2')],
            [('A', 'dropped', None), ('A', 'retained', 'G1'), (None, 'dropped', 'G2')],
            [('Z', 'quoted," name\nnext', '基因,研'), ('A', 'name', ' G1 '), ('A', 'name', '001A')],
            [(2, 123, 1), (1, 456, 2), (2, 789, 3)],
            [(2.5, 1.2, 1.0), (1.5, None, 2.125), (2.5, 7.8, -3.25)],
            [(True, 'yes', True), (False, 'no', False), (True, 'later', False)],
            [('001', 'first', '0001'), ('010', 'second', '0002')],
            [('A', 'a', '1e-06'), ('A', 'ignored', '2.25e+17')],
            [('A', 'name', 'NA'), ('A', 'name', 'N/A'), ('A', 'name', 'G1')],
            [('index', 'B', 'first', 'G1'), ('index', 'B', 'ignored', 'G2'), ('index', 'A', 'alpha', 'G3')],
            [('same', 'x', 'B', None, 'G1'), ('same', 'x', 'B', 'ignored', 'G2')],
        )
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'sets.csv'
            for number, rows in enumerate(cases):
                with self.subTest(case=number):
                    table(path, rows)
                    encoded = path.read_bytes()
                    self.compare_loader(path)
                    self.assertEqual(path.read_bytes(), encoded)
            table(path, [('B', 'first', 'G1', 'ignored extra'), ('A', 'second', 'G2', None)],
                  ('pathway_id', 'pathway_name', 'gene_id', 'extra'))
            self.compare_loader(path)

    def test_string_storage_numeric_objects_and_nonunique_indexes_keep_source_frames(self):
        columns = [
            pd.Series(['G1', 'G2', None, 'G1', '基因', 'G3', 'G4'], dtype='string[python]'),
            pd.Series([1, 1.0, None, True, '1', '基因', -2], dtype=object),
            pd.Series([1, 2, 3, 4, 5, 6, 7], dtype='int64'),
            pd.Series([1.0, 2.5, None, 1e-20, -3.5, float('inf'), 0.0], dtype='float64'),
            pd.Series([True, False, None, True, False, True, False], dtype='boolean'),
        ]
        if importlib.util.find_spec('pyarrow'):
            columns.append(pd.Series(['G1', 'G2', None, 'G1', '基因', 'G3', 'G4'], dtype='string[pyarrow]'))
        for values in columns:
            with self.subTest(dtype=str(values.dtype), storage=getattr(values.dtype, 'storage', None)):
                frame = pd.DataFrame({'pathway_id': pd.Series([1, '1', None, 1.0, '1.0', 'Z', 'Z'], dtype=object),
                                      'pathway_name': [None, 'second', 'dropped', 'fourth', 'fifth', 'first Z', 'ignored Z'],
                                      'gene_id': values})
                frame.index = [5, 5, 2, 9, 9, 1, 1]
                before = frame.copy(deep=True)
                with patch.object(pd, 'read_csv', return_value=frame):
                    self.compare_loader('mocked.csv')
                pd.testing.assert_frame_equal(frame, before)

    def test_gene_string_materializations_are_bounded_and_all_rows_are_processed(self):
        dtypes = ['string[python]']
        if importlib.util.find_spec('pyarrow'):
            dtypes.append('string[pyarrow]')
        for dtype in dtypes:
            for rows in (0, 1, 16383, 16384, 16385, 65537):
                with self.subTest(dtype=dtype, rows=rows):
                    frame = pd.DataFrame({'pathway_id': ['P'] * rows, 'pathway_name': ['name'] * rows,
                                          'gene_id': pd.Series(['GENE_DUPLICATE'] * rows, dtype=dtype)})
                    with patch.object(pd, 'read_csv', return_value=frame):
                        expected = legacy._load_gene_sets('mocked.csv')
                    array_type = type(frame['gene_id'].array)
                    original = array_type.to_numpy
                    sizes = []

                    def bounded(array, *arguments, **options):
                        if len(array) and array[0] == 'GENE_DUPLICATE':
                            sizes.append(len(array))
                            self.assertLessEqual(len(array), 16384)
                        return original(array, *arguments, **options)

                    with patch.object(pd, 'read_csv', return_value=frame), patch.object(array_type, 'to_numpy', new=bounded):
                        self.assertEqual(current._load_gene_sets('mocked.csv'), expected)
                    self.assertEqual(sum(sizes), rows)

    def test_complete_handler_outputs_match_for_identifiers_thresholds_and_missing_names(self):
        cases = (
            ([('G1', 0.01, 2), ('G2', 0.5, -2), ('基因,研', 0.01, -2)],
             [('B', None, 'G1'), ('B', 'ignored', '基因,研'), ('A', 'alpha', 'G2'), ('B', 'duplicate', 'G1')]),
            ([(1, 0.01, 2), (2, 0.5, -2), (3, 0.01, -2)], [(2, 'two', 1), (1, 'one', 2), (2, 'ignored', 3)]),
            ([(1.5, 0.01, 2), (2.125, 0.5, -2)], [('B', 'name', 1.5), ('A', 'name', 2.125)]),
            ([(True, 0.01, 2), (False, 0.5, -2)], [(True, 'true', True), (False, 'false', False)]),
            ([], [('A', 'outside', 'G')]),
            ([('G', 0.01, 2)], []),
        )
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for de_rows, gene_rows in cases:
                de = table(root / 'de.csv', de_rows, ('gene_id', 'padj', 'log2_fc'))
                sets = table(root / 'sets.csv', gene_rows)
                output = root / 'result.csv'
                for parameters in ({}, {'padj_cutoff': 0}, {'padj_cutoff': float('nan')}, {'abs_log2_fc_cutoff': 0}):
                    with self.subTest(genes=gene_rows, parameters=parameters):
                        expected = legacy.run_pathway_enrichment(de, sets, output, **parameters)
                        encoded = output.read_bytes()
                        self.assertEqual(current.run_pathway_enrichment(de, sets, output, **parameters), expected)
                        self.assertEqual(output.read_bytes(), encoded)

    def test_parser_and_missing_column_errors_preserve_existing_output_and_error_order(self):
        cases = ('missing', 'empty', 'columns', 'bad_quote', 'de_columns', 'bad_padj')
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for case in cases:
                with self.subTest(case=case):
                    de = table(root / 'de.csv', [('G', 0.01, 2)], ('gene_id', 'padj', 'log2_fc'))
                    sets = table(root / 'sets.csv', [('P', 'name', 'G')])
                    if case == 'missing':
                        sets = root / 'missing.csv'
                    elif case == 'empty':
                        sets.write_bytes(b'')
                    elif case == 'columns':
                        table(sets, [('P', 'G')], ('pathway_id', 'gene_id'))
                    elif case == 'bad_quote':
                        sets.write_bytes(b'pathway_id,pathway_name,gene_id\nP,"unterminated,G\n')
                    elif case == 'de_columns':
                        table(de, [('G',)], ('gene_id',))
                    else:
                        table(de, [('G', 'invalid', 2)], ('gene_id', 'padj', 'log2_fc'))
                    output = root / 'previous.csv'
                    output.write_bytes(b'keep previous results\n')
                    errors = []
                    for module in (legacy, current):
                        try:
                            module.run_pathway_enrichment(de, sets, output)
                        except Exception as exc:
                            errors.append((type(exc), str(exc)))
                        else:
                            self.fail('invalid input accepted')
                        self.assertEqual(output.read_bytes(), b'keep previous results\n')
                    self.assertEqual(errors[0], errors[1])
