import unittest

from src.domain_registry import available_domains, domain_catalog, run_tool, tool_specs


class SequencePluginTests(unittest.TestCase):
    def test_sequence_domain_is_discovered_when_backend_is_available(self):
        if 'sequence' not in available_domains():
            self.skipTest('mRNA-Forge backend is not installed')
        names = {spec['name'] for spec in tool_specs('sequence')}
        self.assertIn('sequence_optimize', names)
        self.assertIn('sequence_pipeline', names)

    def test_sequence_pipeline_returns_a_verified_result(self):
        if 'sequence' not in available_domains():
            self.skipTest('mRNA-Forge backend is not installed')
        result = run_tool('sequence_pipeline', {
            'protein': 'MKT',
            'molecule': 'linear',
            'method': 'greedy',
        })
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['plugin'], 'sequence')
        self.assertTrue(result['result']['verify'])
        self.assertEqual(result['result']['mrna_len'], 9)

    def test_platform_domain_catalog_reports_plugin_health(self):
        catalog = {item['domain']: item for item in domain_catalog()}
        self.assertIn('cadd', catalog)
        self.assertIn('omics', catalog)
        if 'sequence' in available_domains():
            self.assertEqual(catalog['sequence']['status'], 'available')
            self.assertGreaterEqual(catalog['sequence']['tool_count'], 7)
            self.assertIn('pipeline', catalog['sequence']['tools'])

    def test_sequence_input_validation_is_structured(self):
        if 'sequence' not in available_domains():
            self.skipTest('mRNA-Forge backend is not installed')
        result = run_tool('sequence_score', {'mrna': 'not-a-sequence'})
        self.assertEqual(result['status'], 'error')
        self.assertIn('unsupported', result['error'])


if __name__ == '__main__':
    unittest.main()
