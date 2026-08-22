import tempfile
import unittest
from pathlib import Path

from src.domain_registry import available_domains, run_tool


class KnowledgePluginTests(unittest.TestCase):
    def test_knowledge_domain_is_registered(self):
        self.assertIn('knowledge', available_domains())

    def test_ingest_and_search_returns_ranked_citations(self):
        with tempfile.TemporaryDirectory(prefix='knowledge_test_') as raw:
            root = Path(raw)
            (root / 'rna.md').write_text(
                '# RNA-seq analysis\nDifferential expression and pathway enrichment.',
                encoding='utf-8',
            )
            (root / 'mrna.md').write_text(
                '# mRNA design\nCodon optimization and translation verification.',
                encoding='utf-8',
            )
            index = root / 'index.json'
            ingested = run_tool('knowledge_ingest_directory', {
                'input_dir': str(root),
                'output_path': str(index),
            })
            self.assertEqual(ingested['status'], 'ok')
            self.assertEqual(ingested['result']['n_documents'], 2)
            result = run_tool('knowledge_search', {
                'query': 'pathway enrichment',
                'index_path': str(index),
                'top_k': 2,
            })
            self.assertEqual(result['status'], 'ok')
            self.assertGreaterEqual(result['result']['n_matches'], 1)
            self.assertEqual(result['result']['matches'][0]['document_id'], 'rna.md')
            self.assertGreater(result['result']['matches'][0]['score'], 0)


if __name__ == '__main__':
    unittest.main()
