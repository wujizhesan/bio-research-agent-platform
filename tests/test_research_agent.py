import json
import os
import unittest
from unittest.mock import patch

from src import research_planner
from src.domain_registry import available_domains, run_tool
from src.workflow_runner import load_workflow


class ResearchAgentTests(unittest.TestCase):
    def test_research_application_domain_is_registered(self):
        self.assertIn('research', available_domains())

    def test_research_plan_selects_domains_from_task(self):
        result = run_tool('research_plan', {
            'task': '分析 RNA-seq 差异表达并设计 mRNA 序列',
        })
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['application'], 'bioinformatics-research-agent')
        self.assertEqual(result['selected_domains'], ['omics', 'sequence'])
        self.assertIn('omics_run_differential_expression', result['capabilities'])
        self.assertIn('sequence_pipeline', result['capabilities'])
        self.assertFalse(result['policy']['llm_may_invent_measurements'])
        self.assertFalse(result['execution']['ready'])
        self.assertIn('expression_csv', result['execution']['missing_inputs'])

    def test_research_plan_selects_omics_for_metagenomics_task(self):
        result = run_tool('research_plan', {
            'task': '分析宏基因组物种丰度',
        })
        self.assertEqual(result['selected_domains'], ['omics'])

    def test_llm_planner_selects_domains_before_deterministic_workflow_build(self):
        response = {
            'choices': [{
                'message': {
                    'content': json.dumps({
                        'domains': ['omics', 'mRNA'],
                        'rationale': ['The task combines expression analysis and mRNA design.'],
                    }),
                },
            }],
        }

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, traceback):
                return False

            def read(self):
                return json.dumps(response).encode('utf-8')

        environment = {
            'RESEARCH_PLANNER_API_KEY': 'test-key',
            'RESEARCH_PLANNER_BASE_URL': 'https://planner.test/v1',
            'RESEARCH_PLANNER_MODEL': 'planner-test',
            'CADD_API_KEY': '',
            'OPENAI_API_KEY': '',
        }
        with patch.dict(os.environ, environment, clear=False):
            with patch.object(research_planner.urllib.request, 'urlopen', return_value=FakeResponse()) as request:
                result = run_tool('research_plan', {
                    'task': '分析 RNA-seq 差异表达并设计 mRNA 序列',
                    'planner_mode': 'llm',
                })
        self.assertEqual(result['planner']['backend'], 'llm')
        self.assertEqual(result['planner']['model'], 'planner-test')
        self.assertEqual(result['selected_domains'], ['omics', 'sequence'])
        self.assertFalse(result['policy']['llm_may_select_tools'])
        self.assertTrue(result['policy']['llm_may_select_domains'])
        self.assertEqual(request.call_args.args[0].full_url, 'https://planner.test/v1/chat/completions')

    def test_research_planner_builds_kegg_rnaseq_sequence_workflow(self):
        inputs = {
            'expression_csv': 'examples/rnaseq/expression.csv',
            'metadata_csv': 'examples/rnaseq/metadata.csv',
            'gene_sets_csv': 'examples/rnaseq/gene_sets.csv',
            'protein': 'MKT',
            'output_dir': 'output/test_auto_research',
        }
        result = run_tool('research_plan', {
            'task': '分析 RNA-seq 差异表达，使用 KEGG 解释通路并设计 mRNA',
            'inputs': inputs,
        })
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['evidence_provider'], 'kegg')
        self.assertTrue(result['execution']['ready'])
        workflow = result['execution']['workflow']
        self.assertEqual(workflow['steps'][0]['tool'], 'omics_run_analysis')
        self.assertEqual(workflow['steps'][0]['args']['evidence_provider'], 'kegg')
        self.assertIn('sequence_pipeline', [step['tool'] for step in workflow['steps']])
        self.assertEqual(result['execution']['selected_tools'], [
            'omics_run_analysis', 'sequence_pipeline', 'sequence_report'
        ])

        built = run_tool('research_build_workflow', {
            'task': '分析 RNA-seq 差异表达，使用 KEGG 解释通路并设计 mRNA',
            'inputs': inputs,
        })
        self.assertTrue(built['ready'])
        dry_run = run_tool('research_execute', {
            'workflow': built['workflow'],
            'domains': built['selected_domains'],
            'dry_run': True,
            'output_path': 'output/test_auto_research_manifest.json',
        })
        self.assertEqual(dry_run['status'], 'planned')
        self.assertEqual(dry_run['manifest']['completed_steps'], 3)

    def test_research_planner_requires_local_evidence_file(self):
        result = run_tool('research_build_workflow', {
            'task': '查询本地基因证据',
            'domains': ['literature'],
            'inputs': {
                'gene_ids': ['GeneA'],
                'evidence_provider': 'local',
            },
        })
        self.assertFalse(result['ready'])
        self.assertIn('evidence_csv', result['missing_inputs'])

    def test_research_planner_requires_gencode_gtf_for_gencode_evidence(self):
        result = run_tool('research_build_workflow', {
            'task': '根据 GENCODE GTF 注释查询 TP53 基因',
            'domains': ['literature'],
            'inputs': {
                'gene_ids': ['TP53'],
                'evidence_provider': 'gencode',
            },
        })
        self.assertFalse(result['ready'])
        self.assertIn('gencode_gtf', result['missing_inputs'])

    def test_research_planner_builds_genomics_qc_workflow(self):
        result = run_tool('research_build_workflow', {
            'task': 'Run FASTQ sequencing quality control',
            'domains': ['omics'],
            'inputs': {
                'input_path': 'examples/rnaseq/expression.csv',
                'input_type': 'fastq',
            },
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], ['omics_run_genomics_qc'])
        self.assertEqual(result['workflow']['steps'][0]['args']['input_type'], 'fastq')

    def test_research_planner_builds_variant_calling_workflow(self):
        result = run_tool('research_build_workflow', {
            'task': 'Call variants from aligned BAM against a reference genome',
            'domains': ['omics'],
            'inputs': {
                'bam_path': 'data/sample.bam',
                'reference_fasta': 'data/reference.fa',
                'region': 'chr1:1-1000',
                'min_mapping_quality': 20,
            },
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], ['omics_run_variant_calling'])
        self.assertEqual(result['workflow']['steps'][0]['args']['region'], 'chr1:1-1000')
        self.assertIn('bcftools mpileup/call', result['rationale'][0])

    def test_research_planner_builds_feature_counts_workflow(self):
        result = run_tool('research_build_workflow', {
            'task': 'Generate gene counts from aligned RNA-seq BAM using featureCounts',
            'domains': ['omics'],
            'inputs': {
                'bam_path': 'data/sample.bam',
                'annotation_gtf': 'data/gencode.gtf',
                'strand': 1,
                'threads': 4,
            },
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], ['omics_run_feature_counts'])
        step = result['workflow']['steps'][0]
        self.assertEqual(step['id'], 'rnaseq_feature_counts')
        self.assertEqual(step['args']['alignment_paths'], 'data/sample.bam')
        self.assertEqual(step['args']['annotation_gtf'], 'data/gencode.gtf')
        self.assertEqual(step['args']['strand'], 1)
        self.assertIn('featureCounts', result['rationale'][0])

    def test_research_planner_chains_feature_counts_into_rnaseq_analysis(self):
        result = run_tool('research_build_workflow', {
            'task': 'Quantify aligned RNA-seq reads and run differential expression and pathway enrichment',
            'domains': ['omics'],
            'inputs': {
                'alignment_paths': ['data/control.bam', 'data/treated.bam'],
                'annotation_gtf': 'data/gencode.gtf',
                'metadata_csv': 'examples/rnaseq/metadata.csv',
                'gene_sets_csv': 'examples/rnaseq/gene_sets.csv',
                'statistics_backend': 'scipy',
            },
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], [
            'omics_run_feature_counts', 'omics_run_analysis',
        ])
        analysis_step = result['workflow']['steps'][1]
        self.assertEqual(analysis_step['depends_on'], ['rnaseq_feature_counts'])
        self.assertEqual(
            analysis_step['args']['expression_csv'],
            '${rnaseq_feature_counts.output_csv}',
        )
        self.assertEqual(analysis_step['args']['statistics_backend'], 'scipy')

    def test_research_planner_chains_variant_normalization_and_annotation(self):
        result = run_tool('research_build_workflow', {
            'task': 'Normalize variants and annotate them before interpretation',
            'domains': ['omics'],
            'inputs': {
                'vcf_path': 'data/raw.vcf',
                'reference_fasta': 'data/reference.fa',
                'annotation_csv': 'examples/variants/gene_annotations.csv',
            },
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], [
            'omics_normalize_variants', 'omics_annotate_variants',
        ])
        self.assertEqual(
            result['workflow']['steps'][1]['args']['vcf_path'],
            '${variant_normalization.output_vcf}',
        )
        self.assertEqual(result['workflow']['steps'][1]['depends_on'], ['variant_normalization'])

    def test_research_planner_supports_gencode_gtf_variant_annotation(self):
        result = run_tool('research_build_workflow', {
            'task': 'Annotate VCF variants with GENCODE gene coordinates',
            'domains': ['omics'],
            'inputs': {
                'vcf_path': 'data/raw.vcf',
                'annotation_backend': 'gencode_gtf',
                'annotation_gtf': 'data/gencode.gtf.gz',
            },
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], ['omics_annotate_variants'])
        self.assertEqual(
            result['workflow']['steps'][0]['args']['annotation_gtf'],
            'data/gencode.gtf.gz',
        )

        evidence_result = run_tool('research_build_workflow', {
            'task': 'Annotate VCF variants with GENCODE and retrieve gene evidence',
            'domains': ['omics', 'literature'],
            'inputs': {
                'vcf_path': 'data/raw.vcf',
                'annotation_backend': 'gencode_gtf',
                'annotation_gtf': 'data/gencode.gtf.gz',
            },
        })
        self.assertTrue(evidence_result['ready'])
        self.assertEqual(
            evidence_result['workflow']['steps'][1]['args']['gencode_gtf'],
            'data/gencode.gtf.gz',
        )

    def test_research_planner_builds_single_cell_qc_workflow(self):
        result = run_tool('research_build_workflow', {
            'task': '分析单细胞 RNA-seq 表达矩阵并进行 QC',
            'domains': ['omics'],
            'inputs': {
                'matrix_csv': 'examples/omics/single_cell_counts.csv',
                'min_genes': 2,
                'max_mito_percent': 20,
            },
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], ['omics_run_single_cell_qc'])
        self.assertEqual(result['workflow']['steps'][0]['args']['min_genes'], 2)

    def test_research_planner_builds_single_cell_10x_qc_workflow(self):
        result = run_tool('research_build_workflow', {
            'task': 'Run 10x single-cell quality control',
            'domains': ['omics'],
            'inputs': {
                'matrix_mtx': 'examples/omics/tenx/matrix.mtx',
                'barcodes_tsv': 'examples/omics/tenx/barcodes.tsv',
                'features_tsv': 'examples/omics/tenx/features.tsv',
            },
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], ['omics_run_single_cell_10x_qc'])
        self.assertEqual(result['workflow']['steps'][0]['args']['matrix_mtx'], 'examples/omics/tenx/matrix.mtx')

    def test_research_planner_builds_metagenomics_qc_workflow(self):
        result = run_tool('research_build_workflow', {
            'task': '分析宏基因组物种丰度并计算 Shannon 多样性',
            'domains': ['omics'],
            'inputs': {
                'abundance_csv': 'examples/omics/metagenome_abundance.csv',
                'min_prevalence': 1,
            },
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], ['omics_run_metagenomics_qc'])
        self.assertEqual(result['workflow']['steps'][0]['args']['min_prevalence'], 1)

    def test_bgi_preset_is_discoverable_and_dry_runnable(self):
        presets = run_tool('research_presets', {})
        self.assertEqual(presets['status'], 'ok')
        self.assertEqual(presets['presets'][0]['id'], 'bgi_research_demo')
        self.assertIn('rnaseq_research_agent', {item['id'] for item in presets['presets']})
        result = run_tool('research_run_preset', {
            'preset': 'bgi_research_demo',
            'dry_run': True,
            'output_path': 'output/test_bgi_preset_manifest.json',
            'report_path': 'output/test_bgi_preset_report.md',
        })
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['selected_domains'], ['omics', 'literature', 'knowledge', 'sequence'])
        self.assertEqual(result['manifest']['completed_steps'], 8)
        self.assertEqual(result['report']['status'], 'ok')
        omics_result = run_tool('research_run_preset', {
            'preset': 'rnaseq_research_agent',
            'dry_run': True,
            'output_path': 'output/test_rnaseq_preset_manifest.json',
            'report_path': 'output/test_rnaseq_preset_report.md',
        })
        self.assertEqual(omics_result['status'], 'planned')
        self.assertEqual(omics_result['selected_domains'], ['omics'])
        self.assertEqual(omics_result['manifest']['completed_steps'], 1)

    def test_bgi_multiomics_preset_is_discoverable_and_dry_runnable(self):
        presets = run_tool('research_presets', {})
        self.assertIn('bgi_multiomics_demo', {item['id'] for item in presets['presets']})
        result = run_tool('research_run_preset', {
            'preset': 'bgi_multiomics_demo',
            'dry_run': True,
            'output_path': 'output/test_bgi_multiomics_manifest.json',
            'report_path': 'output/test_bgi_multiomics_report.md',
        })
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['selected_domains'], ['omics', 'imaging', 'literature', 'knowledge', 'sequence'])
        self.assertEqual(result['manifest']['completed_steps'], 11)
        self.assertEqual(result['manifest']['failed_steps'], 0)
        self.assertIn('knowledge_build_graph', {
            step['tool'] for step in result['manifest']['steps']
        })
        self.assertIn('imaging_inspect_image', {
            step['tool'] for step in result['manifest']['steps']
        })

    def test_research_execute_dry_run_is_domain_scoped(self):
        workflow = {
            'name': 'sequence validation',
            'steps': [{
                'id': 'score',
                'tool': 'sequence_score',
                'args': {'mrna': 'ATGAAGACC', 'molecule': 'linear'},
            }],
        }
        result = run_tool('research_execute', {
            'workflow': workflow,
            'domains': ['sequence'],
            'dry_run': True,
            'output_path': 'output/test_research_manifest.json',
        })
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['selected_domains'], ['sequence'])
        self.assertEqual(result['manifest']['completed_steps'], 1)
        self.assertTrue(result['provenance']['dry_run'])

    def test_rnaseq_agent_workflow_is_dry_runnable(self):
        result = run_tool('research_execute', {
            'workflow': load_workflow('examples/workflows/rnaseq_research_agent.yaml'),
            'domains': ['omics'],
            'dry_run': True,
            'output_path': 'output/test_rnaseq_agent_manifest.json',
        })
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['selected_domains'], ['omics'])
        self.assertEqual(result['manifest']['completed_steps'], 1)

    def test_variant_planner_builds_annotation_and_evidence_workflow(self):
        inputs = {
            'vcf_path': 'examples/variants/variants.vcf',
            'annotation_csv': 'examples/variants/gene_annotations.csv',
            'evidence_csv': 'examples/rnaseq/evidence.csv',
        }
        result = run_tool('research_plan', {
            'task': '对 VCF 做基因注释和变异解读',
            'inputs': inputs,
        })
        self.assertEqual(result['selected_domains'], ['omics'])
        self.assertTrue(result['execution']['ready'])
        self.assertEqual(result['required_inputs'][0]['name'], 'vcf_path')
        self.assertEqual(result['execution']['selected_tools'], [
            'omics_annotate_variants',
        ])

        result = run_tool('research_build_workflow', {
            'task': '对 VCF 做变异解读并检索文献',
            'domains': ['omics', 'literature'],
            'inputs': inputs,
        })
        self.assertTrue(result['ready'])
        self.assertEqual(result['selected_tools'], [
            'omics_annotate_variants',
            'literature_search',
            'literature_summarize',
        ])

    def test_variant_preset_is_discoverable_and_dry_runnable(self):
        result = run_tool('research_run_preset', {
            'preset': 'bgi_variant_demo',
            'dry_run': True,
            'output_path': 'output/test_bgi_variant_manifest.json',
            'report_path': 'output/test_bgi_variant_report.md',
        })
        self.assertEqual(result['status'], 'planned')
        self.assertEqual(result['selected_domains'], ['omics', 'literature'])
        self.assertEqual(result['manifest']['completed_steps'], 3)

    def test_cadd_planner_preserves_demo_runtime_controls(self):
        result = run_tool('research_plan', {
            'task': 'Run a reproducible CADD virtual screening workflow',
            'domains': ['cadd'],
            'inputs': {
                'receptor': 'src/agent.py',
                'ligand_library': 'examples/rnaseq/expression.csv',
                'max_ligands': 3,
                'exhaustiveness': 4,
            },
        })
        self.assertTrue(result['execution']['ready'])
        step = result['execution']['workflow']['steps'][0]
        self.assertEqual(step['tool'], 'cadd_run_screening')
        self.assertEqual(step['args']['max_ligands'], 3)
        self.assertEqual(step['args']['exhaustiveness'], 4)


if __name__ == '__main__':
    unittest.main()
