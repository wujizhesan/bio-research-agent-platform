from pathlib import Path
import tempfile
import unittest

import pandas as pd

from src.omics_results import (
    differential_expression_result,
    pathway_enrichment_result,
    write_omics_report,
)
from src.omics_validation import (
    condition_pair,
    load_expression_matrix,
    normalize_fastq_paths,
    resolve_qc_type,
)
from src.research_results import (
    catalog_result,
    workflow_result,
    write_research_report,
)
from src.research_validation import (
    select_domains,
    validate_planning_request,
    validate_preset,
    validate_workflow,
)


class OmicsBoundaryTests(unittest.TestCase):
    def test_expression_validation_normalizes_sample_order(self):
        with tempfile.TemporaryDirectory(prefix='omics_validation_') as raw:
            root = Path(raw)
            expression_path = root / 'expression.csv'
            metadata_path = root / 'metadata.csv'
            pd.DataFrame({
                'gene_id': ['G1', 'G2'],
                'A1': ['1', '2'],
                'A2': ['2', '3'],
                'B1': ['4', '5'],
                'B2': ['5', '6'],
            }).to_csv(expression_path, index=False)
            pd.DataFrame({
                'sample_id': ['B2', 'A1', 'B1', 'A2'],
                'condition': ['treated', 'control', 'treated', 'control'],
            }).to_csv(metadata_path, index=False)
            expression, metadata = load_expression_matrix(
                expression_path, metadata_path
            )
        self.assertEqual(metadata['sample_id'].tolist(), ['A1', 'A2', 'B1', 'B2'])
        self.assertEqual(expression['A1'].dtype.kind, 'i')
        pair = condition_pair(metadata)
        self.assertEqual(pair[:2], ('control', 'treated'))
        self.assertEqual(pair[2:], (['A1', 'A2'], ['B1', 'B2']))

    def test_file_and_qc_validation_preserve_errors(self):
        with tempfile.TemporaryDirectory(prefix='omics_validation_') as raw:
            invalid = Path(raw) / 'reads.txt'
            invalid.write_text('data', encoding='utf-8')
            with self.assertRaisesRegex(
                ValueError, 'RNA-seq alignment requires FASTQ inputs'
            ):
                normalize_fastq_paths(invalid)
            with self.assertRaisesRegex(ValueError, 'unknown genomics QC input type'):
                resolve_qc_type([invalid], 'unknown')

    def test_result_formatters_keep_public_shape(self):
        differential = pd.DataFrame({'significant': [True, False]})
        backend = {
            'requested': 'auto',
            'backend': 'scipy',
            'fallback_reason': 'DESeq2 unavailable',
        }
        formatted = differential_expression_result(
            'de.csv',
            differential,
            'control',
            'treated',
            ['A1', 'A2'],
            ['B1', 'B2'],
            backend,
        )
        self.assertEqual(formatted['status'], 'completed')
        self.assertEqual(formatted['n_genes'], 2)
        self.assertEqual(formatted['n_significant'], 1)
        self.assertEqual(formatted['backend_requested'], 'auto')

        pathways = pd.DataFrame({'padj': [0.01, 0.2]})
        enrichment = pathway_enrichment_result(
            'pathways.csv', pathways, {'G1', 'G2'}, {'G1'}
        )
        self.assertEqual(enrichment['n_background_genes'], 2)
        self.assertEqual(enrichment['n_significant_pathways'], 1)

    def test_report_formatter_preserves_sources_and_evidence(self):
        de = pd.DataFrame({
            'gene_id': ['G1'],
            'log2_fc': [2.5],
            'padj': [0.001],
            'significant': [True],
        })
        pathways = pd.DataFrame({
            'pathway_name': ['Signal'],
            'overlap_count': [1],
            'padj': [0.01],
        })
        with tempfile.TemporaryDirectory(prefix='omics_results_') as raw:
            report_path = Path(raw) / 'report.md'
            result = write_omics_report(
                de,
                pathways,
                'de.csv',
                'pathways.csv',
                report_path,
                {'n_matches': 1, 'provider': 'local', 'matches': []},
                generated_at='2026-09-07T00:00:00+00:00',
            )
            content = report_path.read_text(encoding='utf-8')
        self.assertEqual(result['n_significant_genes'], 1)
        self.assertIn('Differential-expression result: de.csv', content)
        self.assertIn('Pathway result: pathways.csv', content)
        self.assertIn('Evidence matches: 1', content)


class ResearchBoundaryTests(unittest.TestCase):
    def test_request_validation_does_not_mutate_payload(self):
        inputs = {'expression_csv': 'expression.csv'}
        task, validated = validate_planning_request('  RNA-seq analysis  ', inputs)
        self.assertEqual(task, '  RNA-seq analysis  ')
        self.assertIs(validated, inputs)
        with self.assertRaisesRegex(ValueError, 'task must be a non-empty string'):
            validate_planning_request('   ')
        with self.assertRaisesRegex(ValueError, 'inputs must be an object'):
            validate_planning_request('task', [], require_inputs=True)
        with self.assertRaisesRegex(ValueError, 'workflow must be an object'):
            validate_workflow([])

    def test_domain_and_preset_validation_are_deterministic(self):
        keywords = {'omics': ('rna-seq',), 'literature': ('paper',)}
        selected = select_domains(
            'RNA-seq with a paper', None, {'omics', 'literature', 'research'}, keywords
        )
        self.assertEqual(selected, ['omics', 'literature'])
        self.assertEqual(
            select_domains('task', ['omics', 'omics'], {'omics'}, keywords),
            ['omics'],
        )
        with self.assertRaisesRegex(ValueError, 'unknown research preset'):
            validate_preset('missing', {'known': {}})

    def test_result_formatters_keep_application_contract(self):
        catalog = catalog_result('1.2.3', [{'domain': 'omics'}])
        self.assertEqual(catalog['application'], 'bioinformatics-research-agent')
        self.assertEqual(catalog['application_version'], '1.2.3')
        result = workflow_result(
            '  task  ',
            ['omics'],
            {'ready': True, 'workflow': {'steps': []}},
            {'backend': 'deterministic', 'mode': 'deterministic'},
        )
        self.assertEqual(result['task'], 'task')
        self.assertEqual(result['selected_domains'], ['omics'])
        self.assertEqual(
            result['provenance']['workflow_validation'],
            'delegated to research_execute',
        )

    def test_research_report_formatter_preserves_trace_and_outputs(self):
        manifest = {
            'workflow': 'demo',
            'status': 'completed',
            'completed_steps': 1,
            'failed_steps': 0,
            'resumed_steps': 0,
            'observability': {'trace_id': 'trace-1', 'job_id': 'job-1'},
            'reproducibility': {
                'run_fingerprint': 'fingerprint-1',
                'seed_status': 'declared',
            },
            'steps': [{
                'id': 'evidence',
                'tool': 'literature_search',
                'status': 'completed',
                'result': {
                    'plugin': 'literature',
                    'result': {'n_matches': 2, 'output_csv': 'evidence.csv'},
                },
            }],
        }
        with tempfile.TemporaryDirectory(prefix='research_results_') as raw:
            path = Path(raw) / 'report.md'
            result = write_research_report(manifest, path)
            content = path.read_text(encoding='utf-8')
        self.assertEqual(result, {'status': 'ok', 'path': str(path)})
        self.assertIn('Trace ID: trace-1', content)
        self.assertIn('literature matches=2', content)
        self.assertIn('output_csv = evidence.csv', content)


if __name__ == '__main__':
    unittest.main()
