import unittest

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


if __name__ == '__main__':
    unittest.main()
