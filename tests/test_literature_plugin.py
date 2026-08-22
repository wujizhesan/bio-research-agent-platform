import unittest

from src.domain_registry import available_domains, run_tool


class LiteraturePluginTests(unittest.TestCase):
    def test_literature_domain_is_registered(self):
        self.assertIn('literature', available_domains())

    def test_local_evidence_search_and_summary(self):
        result = run_tool('literature_search', {
            'gene_ids': ['GeneA'],
            'provider': 'local',
            'evidence_csv': 'examples/rnaseq/evidence.csv',
        })
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['plugin'], 'literature')
        self.assertEqual(result['result']['n_matches'], 1)
        summary = run_tool('literature_summarize', {'evidence': result})
        self.assertEqual(summary['status'], 'ok')
        self.assertEqual(summary['result']['n_matches'], 1)
        self.assertEqual(summary['result']['sources'], {'local_fixture': 1})


if __name__ == '__main__':
    unittest.main()
