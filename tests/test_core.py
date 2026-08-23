import asyncio
import importlib.util
import json
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from pathlib import Path

import pandas as pd
import yaml

from src.agent import TOOLS, _completion_url, load_llm_config, run_tool
from src.config_loader import PROJECT_ROOT, latest_run_dir, load_config, resolve_path
from src.dock_vina import dock_one, parse_affinity
from src.fetch_chembl_egfr import aggregate_activity_records, assign_scaffold_split
from src.import_bindingdb import aggregate_bindingdb_records, filter_target_rows, normalize_bindingdb_api_records, normalize_bindingdb_records
from src.audit_external_overlap import audit_overlap, remove_structure_overlap
from src.build_hard_decoy_benchmark import build_hard_decoy_benchmark, build_random_control_benchmark
from src.compare_benchmarks import compare_benchmarks, compare_benchmark_replicates
from src.run_benchmark_replicates import _normalize_id, _validate_control, _validate_hard_benchmark
from src.omics_agent import TOOLS as OMICS_TOOLS, _external_tool_version, _resolve_statistics_backend, annotate_variants, normalize_variants, run_feature_counts, run_genomics_qc, run_metagenomics_qc, run_omics_analysis, run_rnaseq_alignment, run_single_cell_10x_qc, run_single_cell_qc, run_tool as run_omics_tool, run_variant_calling, search_gene_evidence, statistics_backend_status, toolchain_status
from src.domain_registry import run_tool as run_domain_tool, tool_specs, validate_tool_map
from src.workflow_runner import run_workflow
from src.resplit_external import joint_split_indices
from src.plugin_loader import CADD_BACKEND_CONTRACTS, load_contract, load_plugin, plugin_info, require_callable, validate_plugin
from src.pipeline import _file_sha256, _library_sha256, _load_backends, _map_ligand_names, _resolve_run_dir, _runtime_signature, _validate_run_id, check_environment, run_ml
from src.ml_predictor import _calibrate_threshold, load_external_dataset, scaffold_key, scaffold_split_indices, train_model
from src.report import generate_report
import src.qa_verify as qa_verify


class CoreTests(unittest.TestCase):
    def test_config_and_paths(self):
        config = load_config()
        self.assertEqual(config['receptor']['pdb_id'], '4hjo')
        self.assertAlmostEqual(config['ml']['test_fraction'], 0.25)
        self.assertEqual(resolve_path('data/4hjo.pdb'), PROJECT_ROOT / 'data' / '4hjo.pdb')

    def test_run_directory_isolated_and_latest_pointer_resolves(self):
        with tempfile.TemporaryDirectory(prefix='cadd_runs_') as raw:
            root = Path(raw)
            run_dir, run_id = _resolve_run_dir(
                root,
                {'runs_dir': 'runs', 'isolate_runs': True},
                {},
                None,
                'abcdef0123456789',
            )
            self.assertEqual(run_id, 'abcdef0123456789')
            run_dir.mkdir(parents=True)
            (root / 'latest_run.json').write_text(json.dumps({
                'run_id': run_id,
                'path': 'runs/abcdef0123456789',
            }), encoding='utf-8')
            self.assertEqual(latest_run_dir(root), run_dir)
            self.assertEqual(_validate_run_id('trial-01_v2'), 'trial-01_v2')
            flat_dir, flat_id = _resolve_run_dir(
                root,
                {'runs_dir': 'runs', 'isolate_runs': False},
                {},
                None,
                'abcdef0123456789',
            )
            self.assertEqual(flat_dir, root)
            self.assertIsNone(flat_id)
            with self.assertRaises(ValueError):
                _validate_run_id('../escape')

    def test_benchmark_identifier_normalization(self):
        self.assertEqual(_normalize_id(199967.0), '199967')
        self.assertEqual(_normalize_id('199967'), '199967')
        self.assertEqual(_normalize_id(None), '')

    def test_hard_decoy_benchmark_balances_test_pairs(self):
        rows = [
            {'name': 'train_active', 'smiles': 'CCO', 'tag': 'active', 'split': 'train'},
            {'name': 'train_inactive', 'smiles': 'CCCl', 'tag': 'inactive', 'split': 'train'},
            {'name': 'test_active_1', 'smiles': 'c1ccccc1O', 'tag': 'active', 'split': 'test'},
            {'name': 'test_active_2', 'smiles': 'c1ccncc1', 'tag': 'active', 'split': 'test'},
            {'name': 'test_inactive_1', 'smiles': 'C1CCCCC1', 'tag': 'inactive', 'split': 'test'},
            {'name': 'test_inactive_2', 'smiles': 'CC(=O)O', 'tag': 'inactive', 'split': 'test'},
        ]
        with tempfile.TemporaryDirectory(prefix='cadd_hard_decoy_') as raw:
            root = Path(raw)
            input_csv = root / 'input.csv'
            output_csv = root / 'hard_decoy.csv'
            provenance = root / 'hard_decoy.provenance.json'
            pd.DataFrame(rows).to_csv(input_csv, index=False)
            output, metadata = build_hard_decoy_benchmark(
                input_csv, output_csv, provenance, max_tanimoto=0.9
            )
            test = output[output['split'] == 'test']
            self.assertGreater(metadata['test_hard_decoy_rows'], 0)
            self.assertEqual(int((test['benchmark_role'] == 'active').sum()), metadata['test_active_rows'])
            self.assertEqual(int((test['benchmark_role'] == 'hard_decoy').sum()), metadata['test_hard_decoy_rows'])
            self.assertEqual(set(test['tag']), {'active', 'inactive'})
            self.assertTrue(output_csv.exists())
            self.assertTrue(provenance.exists())
            control_csv = root / 'random_control.csv'
            control_provenance = root / 'random_control.provenance.json'
            active_names = test.loc[test['benchmark_role'] == 'active', 'name'].tolist()
            control, control_meta = build_random_control_benchmark(
                input_csv, active_names, control_csv, control_provenance, random_state=7
            )
            control_test = control[control['split'] == 'test']
            self.assertEqual(control_meta['test_active_rows'], len(active_names))
            self.assertEqual(int((control_test['benchmark_role'] == 'random_control').sum()), len(active_names))
            self.assertEqual(len(control_test), len(active_names) * 2)
            input_frame = load_external_dataset(input_csv).reset_index(drop=True)
            metadata = json.loads(provenance.read_text(encoding='utf-8'))
            validated_names = _validate_hard_benchmark(output, input_frame, metadata)
            self.assertEqual(set(validated_names), set(active_names))
            validation = _validate_control(control, validated_names, output)
            self.assertTrue(validation['active_names_match'])

    @patch('src.compare_benchmarks.train_model')
    def test_benchmark_comparison_reports_deltas(self, mocked_train):
        metrics = {
            'balanced_accuracy': 0.7,
            'roc_auc': 0.8,
            'pr_auc': 0.75,
            'pr_auc_baseline': 0.5,
            'pr_auc_lift': 1.5,
            'top30_enrichment': 1.2,
            'n_train': 10,
            'n_test': 4,
            'test_active_fraction': 0.5,
            'scaffold_overlap_count': 0,
            'document_overlap_count': 0,
        }
        control_metrics = dict(metrics, balanced_accuracy=0.6, roc_auc=0.7, pr_auc=0.65, pr_auc_lift=1.3)
        mocked_train.side_effect = [(None, metrics), (None, control_metrics)]
        with tempfile.TemporaryDirectory(prefix='cadd_compare_') as raw:
            result = compare_benchmarks('hard.csv', 'control.csv', Path(raw) / 'comparison.json')
        self.assertAlmostEqual(result['hard_minus_random_delta']['roc_auc'], 0.1)
        self.assertAlmostEqual(result['hard_minus_random_delta']['pr_auc_lift'], 0.2)

    @patch('src.compare_benchmarks.train_model')
    def test_repeated_benchmark_comparison_reports_bootstrap_summary(self, mocked_train):
        base = {
            'balanced_accuracy': 0.7,
            'roc_auc': 0.8,
            'pr_auc': 0.75,
            'pr_auc_baseline': 0.5,
            'pr_auc_lift': 1.5,
            'top30_enrichment': 1.2,
            'n_train': 10,
            'n_test': 4,
            'test_active_fraction': 0.5,
            'scaffold_overlap_count': 0,
            'document_overlap_count': 0,
        }
        mocked_train.side_effect = [
            (None, base),
            (None, dict(base, roc_auc=0.7)),
            (None, dict(base, roc_auc=0.72)),
            (None, dict(base, roc_auc=0.74)),
        ]
        with tempfile.TemporaryDirectory(prefix='cadd_compare_replicates_') as raw:
            result = compare_benchmark_replicates(
                'hard.csv',
                ['control_1.csv', 'control_2.csv', 'control_3.csv'],
                Path(raw) / 'comparison.json',
                bootstrap_iterations=100,
            )
        self.assertEqual(result['n_replicates'], 3)
        summary = result['hard_minus_random_summary']['roc_auc']
        self.assertEqual(summary['n'], 3)
        self.assertAlmostEqual(summary['mean'], 0.08)
        self.assertLessEqual(summary['ci95_low'], summary['mean'])
        self.assertGreaterEqual(summary['ci95_high'], summary['mean'])

    @patch('src.compare_benchmarks.train_model')
    def test_benchmark_comparison_rejects_shape_mismatch(self, mocked_train):
        metrics = {
            'balanced_accuracy': 0.7,
            'roc_auc': 0.8,
            'pr_auc': 0.75,
            'pr_auc_baseline': 0.5,
            'pr_auc_lift': 1.5,
            'top30_enrichment': 1.2,
            'n_train': 10,
            'n_test': 4,
            'test_active_fraction': 0.5,
            'scaffold_overlap_count': 0,
            'document_overlap_count': 0,
        }
        mocked_train.side_effect = [(None, metrics), (None, dict(metrics, n_test=6))]
        with tempfile.TemporaryDirectory(prefix='cadd_compare_shape_') as raw:
            with self.assertRaises(ValueError):
                compare_benchmarks('hard.csv', 'control.csv', Path(raw) / 'comparison.json')

    @patch('src.evidence_providers.requests.get')
    def test_uniprot_evidence_provider_normalizes_results(self, mocked_get):
        response = Mock()
        response.json.return_value = {
            'results': [{
                'primaryAccession': 'P04637',
                'uniProtkbId': 'P53_HUMAN',
                'genes': [{'geneName': {'value': 'TP53'}}],
                'proteinDescription': {
                    'recommendedName': {'fullName': {'value': 'Cellular tumor antigen p53'}}
                },
                'comments': [{
                    'commentType': 'FUNCTION',
                    'texts': [{'value': 'Regulates cell cycle.'}],
                }],
                'organism': {'scientificName': 'Homo sapiens'},
            }]
        }
        mocked_get.return_value = response
        result = search_gene_evidence(['TP53'], provider='uniprot', timeout=3)
        self.assertEqual(result['provider'], 'uniprot')
        self.assertEqual(result['n_matches'], 1)
        self.assertEqual(result['matches'][0]['accession'], 'P04637')
        self.assertIn('Regulates cell cycle', result['matches'][0]['evidence'])
        mocked_get.assert_called_once()
        self.assertEqual(mocked_get.call_args.kwargs['params']['format'], 'json')

    @patch('src.evidence_providers.requests.get')
    def test_pubmed_evidence_provider_normalizes_results(self, mocked_get):
        search_response = Mock()
        search_response.json.return_value = {
            'esearchresult': {'idlist': ['12345']}
        }
        summary_response = Mock()
        summary_response.json.return_value = {
            'result': {
                'uids': ['12345'],
                '12345': {
                    'uid': '12345',
                    'title': 'TP53 study',
                    'fulljournalname': 'Nature Genetics',
                    'pubdate': '2024',
                    'elocationid': 'doi:10.1000/example',
                    'authors': [{'name': 'Doe J'}],
                },
            }
        }
        mocked_get.side_effect = [search_response, summary_response]
        with tempfile.TemporaryDirectory(prefix='cadd_pubmed_cache_') as raw:
            result = search_gene_evidence(['TP53'], provider='pubmed', cache_dir=raw, timeout=3)
            self.assertEqual(result['provider'], 'pubmed')
            self.assertEqual(result['n_matches'], 1)
            self.assertEqual(result['matches'][0]['pmid'], '12345')
            self.assertEqual(result['matches'][0]['doi'], '10.1000/example')
            self.assertIn('TP53 study', result['matches'][0]['title'])
            mocked_get.reset_mock()
            cached = search_gene_evidence(['TP53'], provider='pubmed', cache_dir=raw, timeout=3)
            self.assertEqual(cached['n_matches'], 1)
            mocked_get.assert_not_called()

    @patch('src.evidence_providers.requests.get')
    def test_ncbi_gene_evidence_provider_normalizes_results(self, mocked_get):
        search_response = Mock()
        search_response.json.return_value = {
            'esearchresult': {'idlist': ['7157']}
        }
        summary_response = Mock()
        summary_response.json.return_value = {
            'result': {
                'uids': ['7157'],
                '7157': {
                    'uid': '7157',
                    'name': 'TP53',
                    'description': 'tumor protein p53',
                    'organism': {'scientificname': 'Homo sapiens'},
                    'chromosome': '17',
                    'maplocation': '17p13.1',
                },
            }
        }
        mocked_get.side_effect = [search_response, summary_response]
        with tempfile.TemporaryDirectory(prefix='cadd_ncbi_gene_cache_') as raw:
            result = search_gene_evidence(['TP53'], provider='ncbi_gene', cache_dir=raw, timeout=3)
            self.assertEqual(result['provider'], 'ncbi_gene')
            self.assertEqual(result['n_matches'], 1)
            self.assertEqual(result['matches'][0]['ncbi_gene_id'], '7157')
            self.assertEqual(result['matches'][0]['organism'], 'Homo sapiens')
            self.assertIn('/gene/7157', result['matches'][0]['url'])
            mocked_get.reset_mock()
            cached = search_gene_evidence(['TP53'], provider='ncbi_gene', cache_dir=raw, timeout=3)
            self.assertEqual(cached['n_matches'], 1)
            mocked_get.assert_not_called()

    @patch('src.evidence_providers.requests.get')
    def test_kegg_evidence_provider_normalizes_results(self, mocked_get):
        find_response = Mock()
        find_response.text = 'hsa:7157\tTP53; tumor protein p53\n'
        pathway_response = Mock()
        pathway_response.text = 'hsa:7157\tpath:hsa04115\n'
        mocked_get.side_effect = [find_response, pathway_response]
        with tempfile.TemporaryDirectory(prefix='cadd_kegg_cache_') as raw:
            result = search_gene_evidence(['TP53'], provider='kegg', cache_dir=raw, timeout=3)
            self.assertEqual(result['provider'], 'kegg')
            self.assertEqual(result['n_matches'], 1)
            self.assertEqual(result['matches'][0]['kegg_id'], 'hsa:7157')
            self.assertEqual(result['matches'][0]['pathways'], ['path:hsa04115'])
            self.assertIn('/entry/hsa:7157', result['matches'][0]['url'])
            mocked_get.reset_mock()
            cached = search_gene_evidence(['TP53'], provider='kegg', cache_dir=raw, timeout=3)
            self.assertEqual(cached['n_matches'], 1)
            mocked_get.assert_not_called()

    @patch('src.evidence_providers.requests.get')
    def test_ucsc_evidence_provider_normalizes_positions_and_caches(self, mocked_get):
        response = Mock()
        response.json.return_value = {
            'genome': 'hg38',
            'positionMatches': [{
                'trackName': 'knownGene',
                'description': 'Gencode Genes',
                'matches': [{
                    'position': 'chr17:7661779-7687550',
                    'hgFindMatches': 'ENST00000269305.9',
                    'posName': 'TP53 (ENST00000269305.9)',
                    'canonical': True,
                    'description': 'tumor protein p53',
                }],
            }],
        }
        mocked_get.return_value = response
        with tempfile.TemporaryDirectory(prefix='cadd_ucsc_cache_') as raw:
            result = search_gene_evidence(
                ['TP53'], provider='ucsc', cache_dir=raw, genome='hg38', timeout=3
            )
            self.assertEqual(result['provider'], 'ucsc')
            self.assertEqual(result['n_matches'], 1)
            self.assertEqual(result['matches'][0]['chromosome'], 'chr17')
            self.assertEqual(result['matches'][0]['start'], 7661779)
            self.assertTrue(result['matches'][0]['canonical'])
            mocked_get.reset_mock()
            cached = search_gene_evidence(
                ['TP53'], provider='ucsc', cache_dir=raw, genome='hg38', timeout=3
            )
            self.assertEqual(cached['n_matches'], 1)
            mocked_get.assert_not_called()

    def test_gencode_evidence_provider_reads_versioned_gene_ids(self):
        with tempfile.TemporaryDirectory(prefix='cadd_gencode_') as raw:
            gtf_path = Path(raw) / 'gencode.test.gtf'
            gtf_path.write_text(
                '##gff-version 2\n'
                'chr17\tHAVANA\tgene\t7661779\t7687550\t.\t-\t.\t'
                'gene_id "ENSG00000141510.17"; gene_type "protein_coding"; '
                'gene_name "TP53"; level 2;\n',
                encoding='utf-8',
            )
            result = search_gene_evidence(
                ['ENSG00000141510'], provider='gencode', gencode_gtf=gtf_path
            )
        self.assertEqual(result['provider'], 'gencode')
        self.assertEqual(result['n_matches'], 1)
        self.assertEqual(result['matches'][0]['gene_id'], 'ENSG00000141510.17')
        self.assertEqual(result['matches'][0]['gene_name'], 'TP53')
        self.assertEqual(result['matches'][0]['gene_type'], 'protein_coding')

    def test_domain_registry_exposes_cadd_and_omics_tools(self):
        specs = tool_specs()
        names = {spec['name'] for spec in specs}
        self.assertIn('cadd_run_screening', names)
        self.assertIn('omics_run_differential_expression', names)
        self.assertIn('omics_run_analysis', names)
        self.assertEqual(len(names), len(specs))
        custom_tools = {'ping': {'description': 'Ping', 'parameters': {'type': 'object'}, 'function': lambda: {'status': 'ok'}}}
        self.assertIs(validate_tool_map('custom', custom_tools), custom_tools)
        with self.assertRaises(ValueError):
            validate_tool_map('broken', {'ping': {'description': 'Ping'}})
        with tempfile.TemporaryDirectory(prefix='cadd_domain_registry_') as raw:
            evidence_path = Path(raw) / 'evidence.csv'
            pd.DataFrame([{
                'gene_id': 'GeneA',
                'source': 'fixture',
                'title': 'Evidence',
                'evidence': 'Record',
            }]).to_csv(evidence_path, index=False)
            result = run_domain_tool('omics_search_gene_evidence', {
                'gene_ids': ['GeneA'],
                'evidence_csv': str(evidence_path),
            })
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['n_matches'], 1)
        self.assertEqual(run_domain_tool('missing_tool', {})['status'], 'error')

    def test_omics_agent_runs_rnaseq_workflow(self):
        expression = pd.DataFrame([
            {'gene_id': 'GeneA', 'A1': 1, 'A2': 2, 'A3': 1, 'B1': 100, 'B2': 110, 'B3': 120},
            {'gene_id': 'GeneB', 'A1': 20, 'A2': 21, 'A3': 19, 'B1': 20, 'B2': 22, 'B3': 19},
            {'gene_id': 'GeneC', 'A1': 5, 'A2': 6, 'A3': 5, 'B1': 5, 'B2': 7, 'B3': 5},
        ])
        metadata = pd.DataFrame([
            {'sample_id': 'A1', 'condition': 'control'},
            {'sample_id': 'A2', 'condition': 'control'},
            {'sample_id': 'A3', 'condition': 'control'},
            {'sample_id': 'B1', 'condition': 'treatment'},
            {'sample_id': 'B2', 'condition': 'treatment'},
            {'sample_id': 'B3', 'condition': 'treatment'},
        ])
        gene_sets = pd.DataFrame([
            {'pathway_id': 'P1', 'pathway_name': 'Example pathway', 'gene_id': 'GeneA'},
            {'pathway_id': 'P1', 'pathway_name': 'Example pathway', 'gene_id': 'GeneB'},
            {'pathway_id': 'P2', 'pathway_name': 'Background pathway', 'gene_id': 'GeneC'},
        ])
        evidence = pd.DataFrame([{
            'gene_id': 'GeneA',
            'source': 'local_fixture',
            'title': 'Example evidence',
            'evidence': 'Synthetic test record',
        }])
        with tempfile.TemporaryDirectory(prefix='cadd_omics_') as raw:
            root = Path(raw)
            expression_path = root / 'expression.csv'
            metadata_path = root / 'metadata.csv'
            gene_sets_path = root / 'gene_sets.csv'
            evidence_path = root / 'evidence.csv'
            out_dir = root / 'out'
            expression.to_csv(expression_path, index=False)
            metadata.to_csv(metadata_path, index=False)
            gene_sets.to_csv(gene_sets_path, index=False)
            evidence.to_csv(evidence_path, index=False)
            result = run_omics_analysis(
                expression_path, metadata_path, gene_sets_path, out_dir, evidence_path
            )
            de = pd.read_csv(out_dir / 'differential_expression.csv')
            self.assertEqual(result['status'], 'completed')
            self.assertGreater(result['differential_expression']['n_significant'], 0)
            self.assertIn(result['differential_expression']['backend'], {'scipy', 'deseq2'})
            self.assertEqual(result['differential_expression']['backend_requested'], 'auto')
            self.assertIn('GeneA', set(de.loc[de['significant'], 'gene_id']))
            self.assertGreater(result['pathway_enrichment']['n_pathways'], 0)
            self.assertEqual(result['report']['n_evidence_matches'], 1)
            self.assertTrue((out_dir / 'omics_manifest.json').exists())
            self.assertIn('run_differential_expression', OMICS_TOOLS)
            agent_result = run_omics_tool('run_analysis', {
                'expression_csv': str(expression_path),
                'metadata_csv': str(metadata_path),
                'gene_sets_csv': str(gene_sets_path),
                'output_dir': str(root / 'agent_out'),
                'evidence_csv': str(evidence_path),
            })
            self.assertEqual(agent_result['status'], 'completed')
            self.assertEqual(agent_result['report']['n_evidence_matches'], 1)
            self.assertEqual(agent_result['differential_expression']['backend_requested'], 'auto')
            self.assertIn(agent_result['differential_expression']['backend'], {'scipy', 'deseq2'})
            tool_result = run_omics_tool('unknown', {})
            self.assertEqual(tool_result['status'], 'error')

    def test_omics_statistics_backend_catalog_is_explicit(self):
        status = statistics_backend_status()
        self.assertTrue(status['scipy']['available'])
        self.assertIn('available', status['deseq2'])

    def test_explicit_deseq2_does_not_silently_fallback(self):
        unavailable = {'available': False, 'backend': 'deseq2', 'reason': 'test unavailable'}
        with patch('src.omics_agent._deseq2_runtime', return_value=unavailable):
            resolved = _resolve_statistics_backend('auto')
            self.assertEqual(resolved['backend'], 'scipy')
            self.assertEqual(resolved['fallback_reason'], 'test unavailable')
            with self.assertRaisesRegex(RuntimeError, 'test unavailable'):
                _resolve_statistics_backend('deseq2')

    def test_variant_annotation_maps_vcf_to_local_genes(self):
        with tempfile.TemporaryDirectory(prefix='cadd_variant_') as raw:
            output_csv = Path(raw) / 'variant_annotation.csv'
            result = annotate_variants(
                'examples/variants/variants.vcf',
                output_csv,
                'examples/variants/gene_annotations.csv',
            )
            annotated = pd.read_csv(output_csv)
            self.assertEqual(result['status'], 'completed')
            self.assertEqual(result['n_variants'], 3)
            self.assertEqual(result['n_alleles'], 3)
            self.assertEqual(result['n_annotated'], 3)
            self.assertEqual(result['backend'], 'mixed')
            self.assertEqual(set(annotated['gene_id'].dropna()), {'GeneA', 'GeneB', 'GeneC'})
            self.assertIn('gatk', toolchain_status())
            self.assertIn('annotate_variants', OMICS_TOOLS)

    def test_variant_annotation_reads_gencode_gtf_coordinates(self):
        with tempfile.TemporaryDirectory(prefix='cadd_gencode_variant_') as raw:
            root = Path(raw)
            gtf_path = root / 'genes.gtf'
            output_csv = root / 'variant_annotation.csv'
            gtf_path.write_text(
                '##gff-version 3\n'
                '1\tsource\tgene\t90\t150\t.\t+\t.\tgene_id "GeneA"; gene_name "Gene A"; gene_type "protein_coding";\n'
                '1\tsource\tgene\t200\t300\t.\t-\t.\tgene_id "GeneB"; gene_name "Gene B"; gene_type "lncRNA";\n',
                encoding='utf-8',
            )
            result = annotate_variants(
                'examples/variants/variants.vcf',
                output_csv,
                annotation_backend='gencode_gtf',
                annotation_gtf=gtf_path,
            )
            annotated = pd.read_csv(output_csv)
        self.assertEqual(result['backend'], 'gencode_gtf')
        self.assertEqual(result['n_annotated'], 2)
        self.assertEqual(set(annotated['gene_id'].dropna()), {'GeneA', 'GeneB'})
        self.assertEqual(annotated.loc[annotated['gene_id'] == 'GeneA', 'gene_type'].iloc[0], 'protein_coding')
        self.assertIn('transcript_id', annotated.columns)

    def test_fastq_genomics_qc_is_reproducible(self):
        with tempfile.TemporaryDirectory(prefix='cadd_fastq_qc_') as raw:
            root = Path(raw)
            fastq_path = root / 'reads.fastq'
            fastq_path.write_text(
                '@read1\nACGT\n+\nIIII\n'
                '@read2\nAC\n+\n!!\n',
                encoding='utf-8',
            )
            result = run_genomics_qc(fastq_path, root / 'qc')
            manifest = json.loads((root / 'qc' / 'genomics_qc.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['tool'], 'python-fastq-parser')
        self.assertEqual(result['metrics']['reads'], 2)
        self.assertEqual(result['metrics']['bases'], 6)
        self.assertAlmostEqual(result['metrics']['mean_quality'], 26.667, places=3)
        self.assertEqual(manifest['manifest_path'], result['manifest_path'])
        self.assertIn('run_genomics_qc', OMICS_TOOLS)

    @patch('src.omics_agent.subprocess.run')
    @patch('src.omics_agent.shutil.which')
    def test_bam_genomics_qc_uses_samtools(self, mocked_which, mocked_run):
        mocked_which.return_value = 'samtools'
        mocked_run.side_effect = [
            SimpleNamespace(returncode=0, stdout='', stderr=''),
            SimpleNamespace(returncode=0, stdout='5 + 1 in total (QC-passed reads + QC-failed reads)\n', stderr=''),
        ]
        with tempfile.TemporaryDirectory(prefix='cadd_bam_qc_') as raw:
            root = Path(raw)
            bam_path = root / 'aligned.bam'
            bam_path.write_bytes(b'placeholder')
            result = run_genomics_qc(bam_path, root / 'qc', input_type='bam')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['tool'], 'samtools')
        self.assertEqual(result['total_reads'], 6)
        self.assertEqual(mocked_run.call_args_list[0].args[0][1:3], ['quickcheck', '-v'])
        self.assertEqual(mocked_run.call_args_list[1].args[0][1], 'flagstat')

    @patch('src.omics_agent.subprocess.run')
    @patch('src.omics_agent.shutil.which')
    def test_vcf_genomics_qc_uses_bcftools(self, mocked_which, mocked_run):
        mocked_which.return_value = 'bcftools'
        mocked_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='SN\t0\tnumber of records:\t4\n',
            stderr='',
        )
        with tempfile.TemporaryDirectory(prefix='cadd_vcf_qc_') as raw:
            root = Path(raw)
            vcf_path = root / 'calls.vcf'
            vcf_path.write_text('##fileformat=VCFv4.3\n', encoding='utf-8')
            result = run_genomics_qc(vcf_path, root / 'qc', input_type='vcf')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['tool'], 'bcftools')
        self.assertEqual(result['number_of_records'], '4')
        self.assertEqual(mocked_run.call_args.args[0][1], 'stats')

    @patch('src.omics_agent.shutil.which', return_value=None)
    def test_variant_calling_reports_missing_native_tools(self, mocked_which):
        with tempfile.TemporaryDirectory(prefix='cadd_variant_calling_') as raw:
            root = Path(raw)
            bam_path = root / 'aligned.bam'
            reference_path = root / 'reference.fa'
            bam_path.write_bytes(b'placeholder')
            reference_path.write_text('>chr1\nACGT\n', encoding='utf-8')
            result = run_variant_calling(bam_path, reference_path, root / 'calling')
            manifest = json.loads((root / 'calling' / 'genomics_qc.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['missing_tools'], ['samtools', 'bcftools'])
        self.assertEqual(manifest['provenance']['inputs']['bam_or_cram']['sha256'], result['provenance']['inputs']['bam_or_cram']['sha256'])
        mocked_which.assert_called()

    @patch('src.omics_agent._external_tool_version', return_value={'available': True, 'version': 'test'})
    @patch('src.omics_agent.shutil.which')
    @patch('src.omics_agent.subprocess.run')
    def test_variant_calling_runs_index_mpileup_call_and_stats(self, mocked_run, mocked_which, mocked_version):
        mocked_which.side_effect = lambda name: f'/usr/bin/{name}'

        def fake_run(command, **kwargs):
            if command[1] == 'call':
                output_vcf = Path(command[command.index('-o') + 1])
                output_vcf.write_text('##fileformat=VCFv4.3\n', encoding='utf-8')
            stdout = 'SN\t0\tnumber of records:\t2\n' if command[1] == 'stats' else ''
            return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

        mocked_run.side_effect = fake_run
        with tempfile.TemporaryDirectory(prefix='cadd_variant_calling_') as raw:
            root = Path(raw)
            bam_path = root / 'aligned.bam'
            reference_path = root / 'reference.fa'
            bam_path.write_bytes(b'placeholder')
            reference_path.write_text('>chr1\nACGT\n', encoding='utf-8')
            result = run_variant_calling(
                bam_path, reference_path, root / 'calling', region='chr1:1-4',
                min_mapping_quality=20, min_base_quality=10,
            )
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['number_of_records'], '2')
        self.assertEqual([step['id'] for step in result['steps']], [
            'reference_index', 'alignment_index', 'mpileup', 'variant_call', 'variant_stats',
        ])
        self.assertIn('bcftools', result['provenance']['tools'])
        mpileup_command = result['steps'][2]['command']
        self.assertIn('-r', mpileup_command)
        self.assertIn('chr1:1-4', mpileup_command)
        self.assertIn('run_variant_calling', OMICS_TOOLS)

    @patch('src.omics_agent._external_tool_version', return_value={'available': True, 'version': 'test'})
    @patch('src.omics_agent.shutil.which')
    @patch('src.omics_agent.subprocess.run')
    def test_variant_normalization_runs_reference_aware_bcftools(self, mocked_run, mocked_which, mocked_version):
        mocked_which.side_effect = lambda name: f'/usr/bin/{name}'

        def fake_run(command, **kwargs):
            if command[1] == 'norm':
                output_vcf = Path(command[command.index('-o') + 1])
                output_vcf.write_text('##fileformat=VCFv4.3\n', encoding='utf-8')
            stdout = 'SN\t0\tnumber of records:\t3\n' if command[1] == 'stats' else ''
            return SimpleNamespace(returncode=0, stdout=stdout, stderr='')

        mocked_run.side_effect = fake_run
        with tempfile.TemporaryDirectory(prefix='cadd_variant_norm_') as raw:
            root = Path(raw)
            vcf_path = root / 'input.vcf'
            reference_path = root / 'reference.fa'
            vcf_path.write_text('##fileformat=VCFv4.3\n', encoding='utf-8')
            reference_path.write_text('>chr1\nACGT\n', encoding='utf-8')
            result = normalize_variants(
                vcf_path, reference_path, root / 'normalization', region='chr1:1-4'
            )
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['number_of_records'], '3')
        self.assertEqual([step['id'] for step in result['steps']], [
            'reference_index', 'normalize', 'stats',
        ])
        self.assertIn('-m', result['steps'][1]['command'])
        self.assertIn('-any', result['steps'][1]['command'])
        self.assertIn('normalize_variants', OMICS_TOOLS)

    @patch('src.omics_agent._external_tool_version', return_value={'available': True, 'version': 'test'})
    @patch('src.omics_agent.shutil.which')
    @patch('src.omics_agent.subprocess.run')
    def test_feature_counts_builds_gene_by_sample_matrix(self, mocked_run, mocked_which, mocked_version):
        mocked_which.return_value = '/usr/bin/featureCounts'

        def fake_run(command, **kwargs):
            raw_counts = Path(command[command.index('-o') + 1])
            raw_counts.write_text(
                '# Program:featureCounts\n'
                'Geneid\tChr\tStart\tEnd\tStrand\tLength\tdata/sample_a.bam\tdata/sample_b.bam\n'
                'GeneA\t1\t1\t100\t+\t100\t10\t20\n'
                'GeneB\t1\t200\t300\t-\t101\t0\t5\n',
                encoding='utf-8',
            )
            return SimpleNamespace(returncode=0, stdout='', stderr='')

        mocked_run.side_effect = fake_run
        with tempfile.TemporaryDirectory(prefix='cadd_feature_counts_') as raw:
            root = Path(raw)
            bam_a = root / 'sample_a.bam'
            bam_b = root / 'sample_b.bam'
            annotation = root / 'genes.gtf'
            bam_a.write_bytes(b'placeholder-a')
            bam_b.write_bytes(b'placeholder-b')
            annotation.write_text('chr1\tsrc\texon\t1\t100\t.\t+\t.\tgene_id "GeneA";\n', encoding='utf-8')
            result = run_feature_counts(
                [bam_a, bam_b], annotation, root / 'counts',
                paired_end=True, threads=4, strand=1,
            )
            counts = pd.read_csv(root / 'counts' / 'expression_counts.csv')
            manifest = json.loads((root / 'counts' / 'feature_counts.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['n_genes'], 2)
        self.assertEqual(result['n_samples'], 2)
        self.assertEqual(result['sample_names'], ['sample_a', 'sample_b'])
        self.assertEqual(counts.columns.tolist(), ['gene_id', 'sample_a', 'sample_b'])
        self.assertEqual(counts['sample_b'].tolist(), [20, 5])
        self.assertEqual(manifest['manifest_path'], result['manifest_path'])
        command = mocked_run.call_args.args[0]
        self.assertIn('-p', command)
        self.assertIn('run_feature_counts', OMICS_TOOLS)

    @patch('src.omics_agent.shutil.which', return_value=None)
    def test_feature_counts_reports_missing_subread_without_fallback(self, mocked_which):
        with tempfile.TemporaryDirectory(prefix='cadd_feature_counts_missing_') as raw:
            root = Path(raw)
            bam_path = root / 'sample.bam'
            annotation = root / 'genes.gtf'
            bam_path.write_bytes(b'placeholder')
            annotation.write_text('chr1\tsrc\texon\t1\t100\t.\t+\t.\tgene_id "GeneA";\n', encoding='utf-8')
            result = run_feature_counts(bam_path, annotation, root / 'counts')
            manifest = json.loads((root / 'counts' / 'feature_counts.json').read_text(encoding='utf-8'))
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['missing_tools'], ['featureCounts'])
        self.assertEqual(manifest['workflow'], 'rnaseq_feature_counts')
        mocked_which.assert_called_once_with('featureCounts')

    @patch('src.omics_agent.subprocess.run')
    def test_feature_counts_version_probe_uses_v_flag(self, mocked_run):
        mocked_run.return_value = SimpleNamespace(
            returncode=0,
            stdout='featureCounts v2.0.8\n',
            stderr='',
        )
        result = _external_tool_version('/usr/bin/featureCounts')
        self.assertTrue(result['available'])
        self.assertEqual(result['version'], 'featureCounts v2.0.8')
        self.assertEqual(mocked_run.call_args.args[0], ['/usr/bin/featureCounts', '-v'])
        self.assertEqual(mocked_run.call_args.kwargs['errors'], 'replace')

    @patch('src.omics_agent.shutil.which', return_value=None)
    def test_rnaseq_alignment_reports_missing_native_tools(self, mocked_which):
        with tempfile.TemporaryDirectory(prefix='cadd_rnaseq_alignment_') as raw:
            root = Path(raw)
            fastq_path = root / 'A1.fastq'
            reference_path = root / 'reference.fa'
            fastq_path.write_text('@r1\nACGT\n+\nIIII\n', encoding='utf-8')
            reference_path.write_text('>chr1\nACGT\n', encoding='utf-8')
            result = run_rnaseq_alignment([fastq_path], reference_path, root / 'output')
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['missing_tools'], ['hisat2', 'hisat2-build', 'samtools'])
        self.assertEqual(mocked_which.call_count, 3)

    def test_single_cell_qc_calculates_metrics_and_filters_cells(self):
        with tempfile.TemporaryDirectory(prefix='cadd_single_cell_qc_') as raw:
            root = Path(raw)
            result = run_single_cell_qc(
                'examples/omics/single_cell_counts.csv',
                root / 'qc',
                min_genes=2,
                max_mito_percent=20,
            )
            metrics = pd.read_csv(root / 'qc' / 'single_cell_cell_metrics.csv')
            filtered = pd.read_csv(root / 'qc' / 'single_cell_filtered_matrix.csv')
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['metrics']['n_cells_input'], 3)
        self.assertEqual(result['metrics']['n_cells_passed'], 1)
        self.assertEqual(result['metrics']['mitochondrial_genes'], ['MT-CO1'])
        self.assertEqual(filtered['cell_id'].tolist(), ['cell-1'])
        self.assertAlmostEqual(float(metrics.loc[0, 'pct_counts_mito']), 11.111, places=3)
        self.assertIn('run_single_cell_qc', OMICS_TOOLS)

    def test_single_cell_10x_qc_preserves_sparse_filtered_artifacts(self):
        from scipy.io import mmread
        result = run_single_cell_10x_qc(
            'examples/omics/tenx/matrix.mtx',
            'examples/omics/tenx/barcodes.tsv',
            'examples/omics/tenx/features.tsv',
            Path('output/test_single_cell_10x_qc'),
            min_genes=2,
            max_mito_percent=20,
        )
        filtered_matrix = mmread(result['outputs']['filtered_matrix'])
        metrics = pd.read_csv(result['outputs']['cell_metrics'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['input_format'], '10x_matrix_market')
        self.assertEqual(result['metrics']['n_cells_input'], 3)
        self.assertEqual(result['metrics']['n_cells_passed'], 1)
        self.assertEqual(filtered_matrix.shape, (3, 1))
        self.assertEqual(metrics.loc[0, 'cell_id'], 'cell-1')
        self.assertIn('run_single_cell_10x_qc', OMICS_TOOLS)

    def test_single_cell_10x_qc_reads_gzip_inputs(self):
        import gzip

        with tempfile.TemporaryDirectory(prefix='cadd_single_cell_10x_gz_') as raw:
            root = Path(raw)
            compressed = []
            for source_name in ('matrix.mtx', 'barcodes.tsv', 'features.tsv'):
                source = Path('examples/omics/tenx') / source_name
                target = root / f'{source_name}.gz'
                with gzip.open(target, 'wb') as handle:
                    handle.write(source.read_bytes())
                compressed.append(target)
            result = run_single_cell_10x_qc(
                *compressed,
                root / 'qc',
                min_genes=2,
                max_mito_percent=20,
            )
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['metrics']['n_cells_passed'], 1)

    def test_metagenomics_qc_normalizes_abundance_and_calculates_alpha_metrics(self):
        with tempfile.TemporaryDirectory(prefix='cadd_metagenomics_qc_') as raw:
            result = run_metagenomics_qc(
                'examples/omics/metagenome_abundance.csv',
                Path(raw) / 'qc',
                min_prevalence=2,
            )
            relative = pd.read_csv(result['outputs']['relative_abundance'])
            sample_metrics = pd.read_csv(result['outputs']['sample_metrics'])
        self.assertEqual(result['status'], 'completed')
        self.assertEqual(result['metrics']['n_taxa_input'], 3)
        self.assertEqual(result['metrics']['n_taxa_retained'], 1)
        self.assertEqual(len(sample_metrics), 2)
        self.assertTrue((sample_metrics['shannon_index'] > 0).all())
        self.assertAlmostEqual(float(relative['sample_a'].sum()), 1.0, places=6)
        self.assertAlmostEqual(float(relative['sample_b'].sum()), 1.0, places=6)
        self.assertIn('run_metagenomics_qc', OMICS_TOOLS)

    def test_agent_uses_project_llm_config(self):
        base_url, model, _ = load_llm_config()
        self.assertTrue(base_url.endswith('/v1/chat/completions'))
        self.assertEqual(model, 'deepseek-v4-flash')
        self.assertEqual(_completion_url('https://example.test/v1/chat/completions'), 'https://example.test/v1/chat/completions')

    def test_agent_tools_have_json_schemas(self):
        self.assertEqual(set(TOOLS), {'run_screening', 'read_results', 'analyze_hit'})
        for spec in TOOLS.values():
            self.assertIn('parameters', spec)
            self.assertEqual(spec['parameters']['type'], 'object')
            self.assertTrue(callable(spec['function']))
        self.assertEqual(TOOLS['analyze_hit']['parameters']['required'], ['name'])
        self.assertEqual(run_tool('missing', '{}')['status'], 'error')

    def test_agent_tools_return_structured_results(self):
        with tempfile.TemporaryDirectory(prefix='cadd_agent_results_') as raw:
            path = Path(raw) / 'top_hits.csv'
            pd.DataFrame([
                {'mol_name': 'a', 'tag': 'active', 'affinity': -7.2},
                {'mol_name': 'b', 'tag': 'inactive', 'affinity': -5.0},
            ]).to_csv(path, index=False)
            result = run_tool('read_results', json.dumps({'path': str(path)}))
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(len(result['rows']), 2)
            analysis = run_tool('analyze_hit', json.dumps({'name': 'a', 'path': str(path)}))
            self.assertEqual(analysis['status'], 'ok')
            self.assertEqual(analysis['strength'], 'strong')

    def test_parse_affinity_returns_best_pose(self):
        text = '1 -7.10 0 0\n2 -8.35 0 0\n3 -6.90 0 0\n'
        self.assertEqual(parse_affinity(text), -8.35)
        self.assertIsNone(parse_affinity('no score'))

    def test_sparse_ligand_mapping(self):
        paths = [Path('ligand_0.pdbqt'), Path('ligand_2.pdbqt')]
        self.assertEqual(_map_ligand_names(paths, ['first', 'invalid', 'third']), {0: 'first', 2: 'third'})

    def test_environment_check_rejects_invalid_plugin(self):
        with tempfile.TemporaryDirectory(prefix='cadd_plugin_config_') as raw:
            config = load_config()
            config['plugins']['ml_backend'] = 'src.missing_backend'
            path = Path(raw) / 'config.yaml'
            path.write_text(yaml.safe_dump(config), encoding='utf-8')
            self.assertEqual(check_environment(path), 1)
    def test_default_backend_factory_contracts(self):
        receptor, library, docking, ml, report = _load_backends({})
        self.assertTrue(callable(receptor))
        self.assertTrue(callable(library))
        self.assertTrue(callable(docking))
        self.assertTrue(callable(getattr(ml, 'train_model')))
        self.assertTrue(callable(report))
    def test_plugin_loader_and_contract(self):
        backend = load_plugin('src.dock_vina', 'unused')
        self.assertTrue(callable(require_callable(backend, 'dock_batch')))
        runtime = load_plugin('src.pipeline:_runtime_signature', 'unused')
        self.assertTrue(callable(runtime))
        docking = load_contract(CADD_BACKEND_CONTRACTS[2], {'target': 'src.dock_vina'})
        self.assertTrue(callable(docking.dock_batch))
        info = plugin_info(docking, CADD_BACKEND_CONTRACTS[2], {'target': 'src.dock_vina'})
        self.assertEqual(info['api_version'], 1)
        self.assertIn('dock_batch', info['capabilities'])
        custom = SimpleNamespace(
            PLUGIN_NAME='custom-docking',
            PLUGIN_VERSION='2.1',
            PLUGIN_API_VERSION=1,
            PLUGIN_CAPABILITIES=('gpu',),
            dock_batch=lambda *args, **kwargs: None,
        )
        custom_info = plugin_info(custom, CADD_BACKEND_CONTRACTS[2], 'custom')
        self.assertEqual(custom_info['name'], 'custom-docking')
        self.assertIn('gpu', custom_info['capabilities'])
        with self.assertRaises(ValueError):
            validate_plugin(SimpleNamespace(PLUGIN_API_VERSION=2), CADD_BACKEND_CONTRACTS[2])
        with self.assertRaises(TypeError):
            require_callable(object(), 'missing')
    def test_environment_check_passes(self):
        self.assertEqual(check_environment(), 0)
    def test_runtime_signature_contains_python_and_packages(self):
        signature = _runtime_signature()
        self.assertTrue(signature['python'])
        self.assertIn('packages', signature)
        self.assertIn('rdkit', signature['packages'])
    def test_resume_fingerprints_change_with_inputs(self):
        first = _library_sha256(['mol'], ['CC'])
        changed = _library_sha256(['mol'], ['CCC'])
        self.assertNotEqual(first, changed)
        with tempfile.TemporaryDirectory() as raw:
            digest_path = Path(raw) / 'receptor.pdb'
            digest_path.write_bytes(b'receptor')
            digest = _file_sha256(digest_path)
        self.assertEqual(len(digest), 64)

    def test_scaffold_split_has_no_scaffold_overlap(self):
        from src.library_data import build_screening_library, is_active
        lib = build_screening_library()
        smiles = list(lib.values())
        labels = [int(is_active(name)) for name in lib]
        train, test = scaffold_split_indices(smiles, labels, test_fraction=0.25)
        train_scaffolds = {scaffold_key(smiles[index]) for index in train}
        test_scaffolds = {scaffold_key(smiles[index]) for index in test}
        self.assertTrue(test)
        self.assertFalse(train_scaffolds & test_scaffolds)
        self.assertEqual(set(labels[index] for index in test), {0, 1})
    def test_chembl_aggregation_and_scaffold_split(self):
        activities = [
            {'molecule_chembl_id': 'CHEMBL1', 'pchembl_value': '7.0', 'assay_type': 'B'},
            {'molecule_chembl_id': 'CHEMBL1', 'pchembl_value': '6.8', 'assay_type': 'B'},
            {'molecule_chembl_id': 'CHEMBL2', 'pchembl_value': '4.0', 'assay_type': 'B'},
            {'molecule_chembl_id': 'CHEMBL2', 'pchembl_value': '4.2', 'assay_type': 'B'},
            {'molecule_chembl_id': 'CHEMBL3', 'pchembl_value': '5.5', 'assay_type': 'B'},
        ]
        molecules = [
            {'molecule_chembl_id': 'CHEMBL1', 'molecule_structures': {'canonical_smiles': 'CCO'}},
            {'molecule_chembl_id': 'CHEMBL2', 'molecule_structures': {'canonical_smiles': 'CCC'}},
            {'molecule_chembl_id': 'CHEMBL3', 'molecule_structures': {'canonical_smiles': 'CCCC'}},
        ]
        rows = aggregate_activity_records(activities, molecules)
        self.assertEqual([row['tag'] for row in rows], ['active', 'inactive'])
        self.assertEqual(rows[0]['evidence_count'], 2)
        self.assertAlmostEqual(rows[0]['pchembl_value'], 6.9)
        rows.extend([
            {'name': 'CHEMBL4', 'smiles': 'CCN', 'tag': 'active'},
            {'name': 'CHEMBL5', 'smiles': 'CCCl', 'tag': 'inactive'},
        ])
        split_rows = assign_scaffold_split(rows, test_fraction=0.25)
        self.assertEqual({row['split'] for row in split_rows}, {'train', 'test'})

    def test_chembl_aggregation_requires_repeated_evidence(self):
        activities = [
            {'molecule_chembl_id': 'CHEMBL1', 'pchembl_value': '7.0', 'assay_type': 'B'},
            {'molecule_chembl_id': 'CHEMBL2', 'pchembl_value': '4.0', 'assay_type': 'B'},
        ]
        molecules = [
            {'molecule_chembl_id': 'CHEMBL1', 'molecule_structures': {'canonical_smiles': 'CCO'}},
            {'molecule_chembl_id': 'CHEMBL2', 'molecule_structures': {'canonical_smiles': 'CCC'}},
        ]
        self.assertEqual(aggregate_activity_records(activities, molecules), [])
    def test_bindingdb_adapter_normalizes_and_aggregates(self):
        frame = pd.DataFrame([
            {'Target UniProt ID': 'P00533', 'BindingDB Ligand Name': 'active_a', 'BindingDB Ligand SMILES': 'CCO', 'Ki (nM)': '10', 'Article DOI': 'doi:a'},
            {'Target UniProt ID': 'P00533', 'BindingDB Ligand Name': 'active_a', 'BindingDB Ligand SMILES': 'CCO', 'Ki (nM)': '12', 'Article DOI': 'doi:b'},
            {'Target UniProt ID': 'P00533', 'BindingDB Ligand Name': 'inactive_a', 'BindingDB Ligand SMILES': 'CCC', 'IC50 (nM)': '10000', 'Article DOI': 'doi:c'},
            {'Target UniProt ID': 'P00533', 'BindingDB Ligand Name': 'inactive_a', 'BindingDB Ligand SMILES': 'CCC', 'IC50 (nM)': '12000', 'Article DOI': 'doi:d'},
            {'Target UniProt ID': 'OTHER', 'BindingDB Ligand Name': 'ignored', 'BindingDB Ligand SMILES': 'CCCC', 'Ki (nM)': '1', 'Article DOI': 'doi:e'},
        ])
        filtered = filter_target_rows(frame, target_uniprot='P00533')
        records = normalize_bindingdb_records(filtered)
        rows = aggregate_bindingdb_records(records, min_observations=2)
        self.assertEqual({row['tag'] for row in rows}, {'active', 'inactive'})
        active_row = next(row for row in rows if row['tag'] == 'active')
        self.assertEqual(active_row['evidence_count'], 2)
        self.assertEqual(active_row['document_count'], 2)
    def test_bindingdb_api_normalizes_public_response(self):
        records = normalize_bindingdb_api_records([
            {'monomerid': 'BDBM1', 'smile': 'CCO', 'affinity_type': 'Ki', 'affinity': '10', 'doi': '10.1/a', 'pmid': '1'},
            {'monomerid': 'BDBM2', 'smile': 'CCC', 'affinity_type': 'IC50', 'affinity': '10000', 'doi': '10.1/b'},
        ])
        self.assertEqual(len(records), 2)
        self.assertAlmostEqual(records[0]['pactivity'], 8.0)
        self.assertEqual(records[0]['document_ids'], '1|10.1/a')
    def test_external_structure_overlap_audit_and_filter(self):
        with tempfile.TemporaryDirectory(prefix='cadd_overlap_test_') as raw:
            root = Path(raw)
            reference = root / 'reference.csv'
            candidate = root / 'candidate.csv'
            pd.DataFrame([
                {'name': 'ref_a', 'smiles': 'CCO'},
                {'name': 'ref_b', 'smiles': 'c1ccccc1'},
            ]).to_csv(reference, index=False)
            pd.DataFrame([
                {'name': 'same_a', 'smiles': 'OCC', 'tag': 'active', 'split': 'train'},
                {'name': 'same_b', 'smiles': 'C1=CC=CC=C1', 'tag': 'inactive', 'split': 'test'},
                {'name': 'new', 'smiles': 'CCC', 'tag': 'active', 'split': 'train'},
            ]).to_csv(candidate, index=False)
            audit = audit_overlap(reference, candidate)
            self.assertEqual(audit['overlap_unique_structures'], 2)
            self.assertEqual(audit['candidate_overlap_rows'], 2)
            self.assertEqual(audit['candidate_overlap_tag_counts'], {'active': 1, 'inactive': 1})
            filtered, _ = remove_structure_overlap(reference, candidate)
            self.assertEqual(filtered['name'].tolist(), ['new'])
            self.assertNotIn('_canonical_smiles', filtered.columns)

    def test_joint_group_split_has_no_document_or_scaffold_overlap(self):
        documents = [{'D1'}, {'D2'}, {'D3'}, {'D4'}, {'D5'}, {'D6'}]
        scaffolds = ['S1', 'S1', 'S2', 'S2', 'S3', 'S4']
        labels = [1, 1, 0, 0, 1, 0]
        train, test = joint_split_indices(documents, scaffolds, labels, test_fraction=0.3)
        self.assertTrue(test)
        self.assertEqual(set(labels[index] for index in train), {0, 1})
        self.assertEqual(set(labels[index] for index in test), {0, 1})
        self.assertFalse(set().union(*(documents[index] for index in train)) & set().union(*(documents[index] for index in test)))
        self.assertFalse({scaffolds[index] for index in train} & {scaffolds[index] for index in test})
    @patch('sklearn.model_selection.cross_val_predict')
    def test_large_training_threshold_uses_grouped_oof(self, mocked_predict):
        import numpy as np
        y = np.array([0, 1] * 501)
        X = np.zeros((len(y), 2))
        groups = np.arange(len(y))
        mocked_predict.return_value = np.column_stack([1 - y, y])
        threshold, score, strategy = _calibrate_threshold(X, y, groups)
        self.assertEqual(strategy, 'training_only_grouped_oof_3fold')
        self.assertEqual(score, 1.0)
        self.assertAlmostEqual(threshold, 0.5)
    def test_external_ml_failure_is_visible(self):
        with tempfile.TemporaryDirectory(prefix='cadd_ml_failure_') as raw:
            output = Path(raw)
            with self.assertRaises(RuntimeError):
                run_ml({}, output, external_dataset=output / 'missing.csv')
            metrics = json.loads((output / 'ml_metrics.json').read_text(encoding='utf-8'))
            self.assertEqual(metrics['evaluation'], 'failed')
            self.assertEqual(metrics['data_source'], str(output / 'missing.csv'))
    def test_external_dataset_holdout(self):
        rows = [
            {'name': 'train_active_1', 'smiles': 'CCO', 'tag': 'active', 'split': 'train', 'document_chembl_ids': 'DOC_A'},
            {'name': 'train_active_2', 'smiles': 'CCN', 'tag': 'active', 'split': 'train', 'document_chembl_ids': 'DOC_B'},
            {'name': 'train_inactive_1', 'smiles': 'CCC', 'tag': 'inactive', 'split': 'train', 'document_chembl_ids': 'DOC_C'},
            {'name': 'train_inactive_2', 'smiles': 'CCCC', 'tag': 'inactive', 'split': 'train', 'document_chembl_ids': 'DOC_D'},
            {'name': 'test_active', 'smiles': 'CCCl', 'tag': 'active', 'split': 'test', 'document_chembl_ids': 'DOC_B'},
            {'name': 'test_inactive', 'smiles': 'CCCCC', 'tag': 'inactive', 'split': 'test', 'document_chembl_ids': 'DOC_E'},
        ]
        with tempfile.TemporaryDirectory(prefix='cadd_external_ml_') as raw:
            path = Path(raw) / 'external.csv'
            pd.DataFrame(rows).to_csv(path, index=False)
            _, metrics = train_model({}, external_dataset=path)
            self.assertEqual(metrics['evaluation'], 'external_holdout')
            self.assertEqual(metrics['n_train'], 4)
            self.assertEqual(metrics['n_test'], 2)
            self.assertEqual(metrics['test_n_active'], 1)
            self.assertEqual(Path(metrics['data_source']).name, 'external.csv')
            self.assertEqual(metrics['scaffold_overlap_count'], 0)
            self.assertEqual(metrics['document_overlap_count'], 1)
            self.assertEqual(metrics['class_imbalance_ratio'], 1.0)
            self.assertEqual(metrics['test_class_imbalance_ratio'], 1.0)
            self.assertEqual(metrics['pr_auc_baseline'], 0.5)
            self.assertIsNotNone(metrics['pr_auc_lift'])
            self.assertEqual(metrics['decision_threshold'], 0.5)
            self.assertEqual(metrics['threshold_strategy'], 'fixed_0.5_insufficient_scaffolds')
            bad_path = Path(raw) / 'bad_external.csv'
            bad_rows = rows + [dict(rows[0], name='duplicate_row')]
            pd.DataFrame(bad_rows).to_csv(bad_path, index=False)
            with self.assertRaises(ValueError):
                load_external_dataset(bad_path)
    def test_vina_seed_is_forwarded(self):
        with tempfile.TemporaryDirectory(prefix='cadd_vina_test_') as raw:
            root = Path(raw)
            response = SimpleNamespace(returncode=0, stdout='1 -7.50 0 0\n', stderr='')
            with patch('src.dock_vina.subprocess.run', return_value=response) as mocked_run:
                result = dock_one(
                    root / 'ligand.pdbqt',
                    root / 'receptor.pdb',
                    root / 'dock',
                    exhaustiveness=6,
                    center=(1.0, 2.0, 3.0),
                    size=(18.0, 18.0, 18.0),
                    vina_exe='vina.exe',
                    seed=42,
                )
            command = mocked_run.call_args.args[0]
            self.assertEqual(command[command.index('--seed') + 1], '42')
            self.assertEqual(result['affinity'], -7.5)

    def test_report_includes_manifest_and_ml_metadata(self):
        with tempfile.TemporaryDirectory(prefix='cadd_report_test_') as raw:
            root = Path(raw)
            csv_path = root / 'top_hits.csv'
            report_path = root / 'report.md'
            manifest_path = root / 'run_manifest.json'
            metrics_path = root / 'ml_metrics.json'
            pd.DataFrame([
                {'mol_name': 'active_a', 'tag': 'active', 'affinity': -8.0},
                {'mol_name': 'inactive_a', 'tag': 'inactive', 'affinity': -5.0},
            ]).to_csv(csv_path, index=False)
            manifest_path.write_text(json.dumps({
                'status': 'completed_with_failures',
                'ligand_count': 3,
                'successful_ligands': 2,
                'failed_ligands': ['invalid'],
            }), encoding='utf-8')
            metrics_path.write_text(json.dumps({
                'evaluation': 'leave_one_out',
                'n_samples': 2,
                'balanced_accuracy': 0.5,
                'roc_auc': 0.5,
                'pr_auc': 0.5,
                'top30_enrichment': 1.0,
                'evaluation': 'scaffold_holdout',
                'split_strategy': 'Bemis-Murcko scaffold split',
                'n_train': 1,
                'n_test': 1,
                'loo_balanced_accuracy': 1.0,
                'data_source': 'external.csv',
                'scaffold_overlap_count': 0,
                'warning': 'demo',
            }), encoding='utf-8')
            text = generate_report(csv_path, report_path, manifest_path=manifest_path, ml_metrics_path=metrics_path)
            self.assertIn('completed_with_failures', text)
            self.assertIn('ML 评估摘要', text)
            self.assertIn('Vina 随机种子', text)
            self.assertIn('scaffold split', text)
            self.assertIn('Class imbalance ratio', text)
            self.assertIn('PR-AUC baseline', text)
            self.assertIn('PR-AUC lift', text)
            self.assertIn('Decision threshold', text)
            self.assertIn('Repeated scaffold ROC-AUC', text)
            self.assertIn('Document overlap count', text)
            self.assertIn('评估数据源', text)
            self.assertIn('Scaffold 重叠数', text)
            self.assertTrue(report_path.exists())

    def test_qa_accepts_consistent_partial_manifest(self):
        with tempfile.TemporaryDirectory(prefix='cadd_qa_test_') as raw:
            root = Path(raw)
            csv_path = root / 'top_hits.csv'
            report_path = root / 'report.md'
            manifest_path = root / 'run_manifest.json'
            pd.DataFrame([
                {'mol_name': 'active_a', 'tag': 'active', 'affinity': -8.0},
                {'mol_name': 'inactive_a', 'tag': 'inactive', 'affinity': -5.0},
            ]).to_csv(csv_path, index=False)
            report_path.write_text('report', encoding='utf-8')
            manifest_path.write_text(json.dumps({
                'status': 'completed_with_failures',
                'ligand_count': 3,
                'successful_ligands': 2,
                'failed_ligands': ['invalid'],
            }), encoding='utf-8')
            self.assertEqual(qa_verify.main(csv_path, report_path, manifest_path), 0)

    @unittest.skipUnless(
        importlib.util.find_spec('rdkit') is not None and importlib.util.find_spec('meeko') is not None,
        '需要RDKit和Meeko',
    )
    def test_build_library_preserves_input_index(self):
        from src.build_library import build_library
        with tempfile.TemporaryDirectory(prefix='cadd_build_test_') as raw:
            root = Path(raw)
            _, pdbqts = build_library(
                ['CC', 'not-a-smiles', 'CCO'],
                root / 'library.sdf',
                root / 'pdbqt',
                ['first', 'invalid', 'third'],
            )
            self.assertEqual(sorted(path.name for path in pdbqts), ['ligand_0.pdbqt', 'ligand_2.pdbqt'])




    def test_workflow_runner_resolves_references(self):
        with tempfile.TemporaryDirectory(prefix='cadd_workflow_') as raw:
            root = Path(raw)
            workflow = {
                'name': 'test-rnaseq',
                'steps': [
                    {
                        'id': 'de',
                        'tool': 'omics_run_differential_expression',
                        'args': {
                            'expression_csv': 'examples/rnaseq/expression.csv',
                            'metadata_csv': 'examples/rnaseq/metadata.csv',
                            'output_csv': str(root / 'de.csv'),
                        },
                    },
                    {
                        'id': 'pathways',
                        'tool': 'omics_run_pathway_enrichment',
                        'depends_on': ['de'],
                        'args': {
                            'de_csv': '${de.output_csv}',
                            'gene_sets_csv': 'examples/rnaseq/gene_sets.csv',
                            'output_csv': str(root / 'pathways.csv'),
                        },
                    },
                ],
            }
            manifest = run_workflow(workflow, output_path=root / 'manifest.json')
            self.assertEqual(manifest['status'], 'completed')
            self.assertEqual(manifest['completed_steps'], 2)
            self.assertTrue((root / 'pathways.csv').exists())
            self.assertTrue((root / 'manifest.json').exists())
            dry_run = run_workflow(workflow, dry_run=True)
            self.assertEqual(dry_run['status'], 'planned')
            invalid = run_workflow({'steps': [{
                'id': 'bad',
                'tool': 'omics_run_differential_expression',
                'args': {'unexpected': 1},
            }]})
            self.assertEqual(invalid['status'], 'failed')

    @unittest.skipUnless(importlib.util.find_spec('mcp') is not None, '需要 MCP SDK')
    def test_mcp_server_reuses_domain_registry(self):
        from src.mcp_server import BioMCPServer
        server = BioMCPServer()
        tools = asyncio.run(server.list_tools())
        self.assertEqual({tool.name for tool in tools}, {spec['name'] for spec in tool_specs()})
        result = asyncio.run(server.call_tool(
            'omics_search_gene_evidence',
            {'gene_ids': ['TP53'], 'evidence_csv': 'examples/rnaseq/evidence.csv'},
        ))
        self.assertFalse(result.is_error)
        self.assertEqual(result.structured_content['status'], 'ok')
if __name__ == '__main__':
    unittest.main()
