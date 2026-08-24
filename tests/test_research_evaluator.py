import unittest

from src.research_evaluator import _load_suite, evaluate_suite


class ResearchEvaluatorTests(unittest.TestCase):
    def test_default_suite_passes_with_reproducible_planning_metrics(self):
        result = evaluate_suite()
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(result['summary']['tasks'], 11)
        self.assertEqual(result['summary']['task_pass_rate'], 1.0)
        self.assertEqual(result['summary']['domain_exact_accuracy'], 1.0)
        self.assertEqual(result['summary']['tool_exact_accuracy'], 1.0)
        self.assertEqual(result['summary']['workflow_valid_rate'], 1.0)

    def test_evaluator_reports_domain_and_tool_mismatches(self):
        result = evaluate_suite({
            'tasks': [{
                'id': 'mismatch',
                'task': 'Search PubMed literature evidence for TP53',
                'inputs': {'gene_ids': ['TP53'], 'evidence_provider': 'pubmed'},
                'expected': {
                    'domains': ['omics'],
                    'tools': ['omics_run_analysis'],
                    'ready': True,
                    'missing_inputs': [],
                },
            }],
        })
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['summary']['failed'], 1)
        self.assertEqual(result['summary']['domain_exact_accuracy'], 0.0)
        self.assertEqual(result['summary']['tool_exact_accuracy'], 0.0)

    def test_suite_rejects_duplicate_task_ids(self):
        with self.assertRaisesRegex(ValueError, 'duplicate'):
            _load_suite({'tasks': [
                {'id': 'same', 'task': 'one', 'expected': {}},
                {'id': 'same', 'task': 'two', 'expected': {}},
            ]})


if __name__ == '__main__':
    unittest.main()
