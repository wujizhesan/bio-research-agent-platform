import gzip
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import pandas as pd

from scripts import benchmark_variant_interval_baseline as legacy
from src import omics_variant_annotation as current
from src.job_execution import ExecutionLimits, JobExecutionError, ProcessToolExecutor, public_execution_failure


class VariantIntervalIndexTests(unittest.TestCase):
    def assert_matches(self, frame, index, chrom, position):
        expected = legacy._local_variant_matches(frame, chrom, position)
        observed = current._local_variant_matches(index, chrom, position)
        pd.testing.assert_frame_equal(pd.DataFrame(observed), pd.DataFrame(expected))

    def test_nested_touching_duplicate_intervals_keep_inclusive_bounds_and_row_order(self):
        frame = pd.DataFrame([
            {'chrom': '1', 'start': 20, 'end': 30, 'gene_id': 'later'},
            {'chrom': '1', 'start': -10, 'end': 100, 'gene_id': 'outer'},
            {'chrom': '2', 'start': 0, 'end': 100, 'gene_id': 'other'},
            {'chrom': '1', 'start': 20, 'end': 30, 'gene_id': 'duplicate'},
            {'chrom': '1', 'start': 30, 'end': 30, 'gene_id': 'point'},
            {'chrom': '1', 'start': 50, 'end': 40, 'gene_id': 'reversed'},
            {'chrom': '1', 'start': 2**63 - 2, 'end': 2**63 - 1, 'gene_id': 'large'},
        ], index=[7, 7, 3, 2, 2, 0, 0])
        index = current._VariantIntervalIndex(frame)
        for chrom in ('1', ' CHR1 ', 'chr2', 'missing'):
            for position in (-2**70, -11, -10, 0, 19, 20, 29, 30, 31, 40, 50, 100, 101, 2**63 - 2, 2**63 - 1, 2**70):
                with self.subTest(chrom=chrom, position=position):
                    self.assert_matches(frame, index, chrom, position)

    def test_seeded_unsorted_intervals_and_missing_metadata_match_full_scan(self):
        rng = random.Random(713)
        rows = []
        for number in range(400):
            start = rng.randrange(-200, 500)
            rows.append({'chrom': str(rng.randrange(1, 5)), 'start': start,
                         'end': start + rng.randrange(150), 'gene_id': str(number % 17),
                         'gene_name': None if number % 3 else '\u57fa\u56e0',
                         'extra': float('nan') if number % 5 else 1.25})
        frame = pd.DataFrame(rows, index=[number % 5 for number in range(400)])
        index = current._VariantIntervalIndex(frame)
        for chrom in ('chr1', '2', ' CHR3 ', '4', 'absent'):
            points = [rng.randrange(-250, 700) for _ in range(50)]
            points.extend(value + offset for row in rows[:5] for value in (row['start'], row['end']) for offset in (-1, 0, 1))
            for position in points:
                self.assert_matches(frame, index, chrom, position)

    def test_index_is_lazy_bounded_by_existing_chromosomes_and_returns_fresh_dicts(self):
        frame = pd.DataFrame([{'chrom': '1', 'start': 1, 'end': 10, 'gene_id': 'a'},
                              {'chrom': '2', 'start': 1, 'end': 10, 'gene_id': 'b'}])
        index = current._VariantIntervalIndex(frame)
        self.assertIsNone(index.groups)
        self.assertEqual(index.trees, {})
        first = index.matches('CHR1', 1)
        first[0]['gene_id'] = 'changed'
        tree = index.trees['1']
        self.assertEqual(index.matches('1', 10)[0]['gene_id'], 'a')
        self.assertIs(index.trees['1'], tree)
        for number in range(200):
            self.assertEqual(index.matches(f'absent{number}', 1), [])
        self.assertEqual(set(index.trees), {'1'})
        self.assertEqual(current._local_variant_matches(None, '1', 1), [])
        self.assertEqual(current._local_variant_matches(frame, '1', 1), legacy._local_variant_matches(frame, '1', 1))

    def compare_execution(self, vcf, output, **arguments):
        expected = legacy.execute_variant_annotation(vcf, output, toolchain={'version': 'fixed'}, **arguments)
        encoded = output.read_bytes()
        observed = current.execute_variant_annotation(vcf, output, toolchain={'version': 'fixed'}, **arguments)
        self.assertEqual(observed, expected)
        self.assertEqual(output.read_bytes(), encoded)
        return observed

    def test_backends_multiallelic_ann_priority_gtf_fallback_and_csv_bytes_match(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            annotation = root / 'genes.csv'
            annotation.write_text('chrom,start,end,gene_id,gene_name\nchr1,10,30,G2,\u57fa\u56e02\n1,1,100,G1,\nchr2,1,100,G3,other\nchr1,20,20,G2,duplicate\n', encoding='utf-8')
            gene_gtf = root / 'genes.gtf'
            gene_gtf.write_text('chr1\tsrc\tgene\t10\t30\t.\t+\t.\tgene_id "G2"; gene_name "two";\n'
                                'chr1\tsrc\ttranscript\t1\t100\t.\t+\t.\tgene_id "ignored";\n'
                                'chr1\tsrc\tgene\t1\t100\t.\t+\t.\tgene_id "G1"; gene_type "coding";\n', encoding='utf-8')
            transcript_gtf = root / 'transcripts.gtf.gz'
            transcript_gtf.write_bytes(gzip.compress(b'chr1\tsrc\ttranscript\t1\t100\t.\t+\t.\tgene_id "T1"; transcript_id "tx";\n', mtime=0))
            encoded = (b'##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n'
                       b'chr1\t20\t.\tA\tG,C\t42\tPASS\tANN=G|missense|MODERATE|ann|ANN1,G|other|HIGH|later|ANN2\n'
                       b'2\t1\tid\tA\tG\t.\t.\t.\nchrX\t100\t.\tT\tC\t.\tPASS\tFLAG\n')
            choices = [dict(annotation_csv=annotation, annotation_backend=backend) for backend in ('auto', 'local', 'vcf_ann')]
            choices.extend(dict(annotation_gtf=path, annotation_backend=backend) for path in (gene_gtf, transcript_gtf) for backend in ('auto', 'gencode_gtf'))
            choices.extend(dict(annotation_backend=backend) for backend in ('auto', 'vcf_ann'))
            for compressed in (False, True):
                vcf = root / ('variants.vcf.gz' if compressed else 'variants.vcf')
                vcf.write_bytes(gzip.compress(encoded, mtime=0) if compressed else encoded)
                for arguments in choices:
                    with self.subTest(gzip=compressed, arguments=arguments):
                        self.compare_execution(vcf, root / 'result.csv', **arguments)

    def test_ann_only_records_do_not_build_interval_index(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            annotation = root / 'genes.csv'
            annotation.write_text('chrom,start,end,gene_id\n1,1,10,G1\n', encoding='utf-8')
            vcf = root / 'variants.vcf'
            vcf.write_text('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t5\t.\tA\tG,C\t.\tPASS\tANN=G|e|i|n|G1,C|e|i|n|G2\n', encoding='utf-8')
            with patch.object(current._VariantIntervalIndex, 'matches', side_effect=AssertionError('unexpected interval lookup')):
                self.compare_execution(vcf, root / 'result.csv', annotation_csv=annotation)

    def test_vcf_errors_keep_types_messages_and_existing_output(self):
        cases = [b'', b'1\t2\t.\tA\tG\t.\tPASS\t.\n',
                 b'#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\tbad\t.\tA\tG\t.\tPASS\t.\n',
                 b'#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t2\t.\tA\t.\t.\tPASS\t.\n',
                 b'#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t2\n',
                 b'#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t2\t.\tA\tG\t.\tPASS\t.\n1\tbad\t.\tA\tG\t.\tPASS\t.\n',
                 b'#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n\xff\n']
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            annotation = root / 'genes.csv'
            annotation.write_text('chrom,start,end,gene_id\n1,1,10,G1\n', encoding='utf-8')
            for encoded in cases:
                for compressed in (False, True):
                    with self.subTest(encoded=encoded, gzip=compressed):
                        vcf = root / ('variants.vcf.gz' if compressed else 'variants.vcf')
                        vcf.write_bytes(gzip.compress(encoded, mtime=0) if compressed else encoded)
                        output = root / 'result.csv'
                        errors = []
                        for module in (legacy, current):
                            output.write_bytes(b'previous result')
                            with self.assertRaises((ValueError, UnicodeError)) as caught:
                                module.execute_variant_annotation(vcf, output, annotation_csv=annotation, toolchain={})
                            errors.append((type(caught.exception), str(caught.exception)))
                            self.assertEqual(output.read_bytes(), b'previous result')
                        self.assertEqual(errors[0], errors[1])

    def test_annotation_validation_errors_are_unchanged(self):
        cases = [('csv', text) for text in (
            'gene_id\nG1\n', 'chrom,start,end,gene_id\n',
            'chrom,start,end,gene_id\n1,bad,10,G1\n',
            'chrom,start,end,gene_id\n1,20,10,G1\n',
            'chrom,start,end,gene_id\n1,1,10,\n')]
        cases.extend(('gtf', text) for text in (
            'chr1\tsrc\tgene\n', 'chr1\tsrc\tgene\tbad\t10\t.\t+\t.\tgene_id "G1";\n',
            'chr1\tsrc\texon\t1\t10\t.\t+\t.\tgene_id "G1";\n'))
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            vcf = root / 'variants.vcf'
            vcf.write_text('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n', encoding='utf-8')
            for kind, text in cases:
                with self.subTest(kind=kind, text=text):
                    annotation = root / ('genes.' + kind)
                    annotation.write_text(text, encoding='utf-8')
                    arguments = {'annotation_csv' if kind == 'csv' else 'annotation_gtf': annotation}
                    errors = []
                    for module in (legacy, current):
                        with self.assertRaises(ValueError) as caught:
                            module.execute_variant_annotation(vcf, root / 'result.csv', toolchain={}, **arguments)
                        errors.append((type(caught.exception), str(caught.exception)))
                    self.assertEqual(errors[0], errors[1])

    def test_repeated_execution_reloads_changed_annotation_file(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            annotation = root / 'genes.csv'
            vcf = root / 'variants.vcf'
            vcf.write_text('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t5\t.\tA\tG\t.\tPASS\t.\n', encoding='utf-8')
            for gene in ('first', 'second'):
                annotation.write_text(f'chrom,start,end,gene_id\n1,1,10,{gene}\n', encoding='utf-8')
                result = self.compare_execution(vcf, root / 'result.csv', annotation_csv=annotation)
                self.assertEqual(result['gene_ids'], [gene])

    def test_real_child_result_errors_and_workspace_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            annotation = root / 'genes.csv'
            annotation.write_text('chrom,start,end,gene_id\n1,1,10,G1\n', encoding='utf-8')
            vcf = root / 'variants.vcf'
            vcf.write_text('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\t5\t.\tA\tG\t.\tPASS\t.\n', encoding='utf-8')
            runner = root / 'child.py'
            repository = str(Path(__file__).resolve().parents[1])
            runner.write_text('import json\nfrom pathlib import Path\nimport sys\n'
                              f'sys.path.insert(0, {repository!r})\n'
                              'from src.omics_variant_annotation import execute_variant_annotation\n'
                              'from src.job_subprocess import _write_process_response\n'
                              'request=json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))\n'
                              'try:\n    result=execute_variant_annotation(**request["arguments"],toolchain={})\n'
                              '    payload={"ok":True,"result":result}\n    code=0\n'
                              'except Exception as exc:\n    payload={"ok":False,"error":str(exc)}\n    code=1\n'
                              'raise SystemExit(_write_process_response(payload,request,Path(sys.argv[2]),code))\n', encoding='utf-8')
            roots = []

            def spawn(command, **kwargs):
                roots.append(Path(command[-1]).parent)
                return subprocess.Popen(command, **kwargs)

            executor = ProcessToolExecutor(ExecutionLimits(timeout_seconds=20, memory_limit_mb=0, cpu_time_seconds=0),
                                           python_executable=sys.executable, runner_path=runner, popen_factory=spawn)
            arguments = {'vcf_path': str(vcf), 'output_csv': str(root / 'result.csv'), 'annotation_csv': str(annotation)}
            try:
                with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                    expected = current.execute_variant_annotation(**arguments, toolchain={})
                    self.assertEqual(executor.execute('variant_probe', arguments), expected)
                    vcf.write_text('#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n1\tbad\t.\tA\tG\t.\tPASS\t.\n', encoding='utf-8')
                    with self.assertRaisesRegex(JobExecutionError, 'invalid POS') as caught:
                        executor.execute('variant_probe', arguments)
                    self.assertEqual(public_execution_failure(caught.exception), {'status': 'error', 'error_code': 'execution_failed', 'error': 'job execution failed'})
            finally:
                executor.shutdown()
            self.assertEqual(len(roots), 2)
            self.assertTrue(all(not path.exists() for path in roots))
            self.assertFalse(executor._active_processes)
