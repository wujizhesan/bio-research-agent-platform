import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.domain_registry import available_domains, run_tool
from src.knowledge_plugin import _small_corpus_scores, knowledge_search


class KnowledgePluginTests(unittest.TestCase):
    def test_small_corpus_scores_match_sklearn(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        cases = (
            (['TP53 tumor suppressor', 'BRCA DNA repair'], 'TP53'),
            (['mRNA mRNA design', 'RNA design', 'DNA'], 'mRNA design'),
            (['单细胞 RNA 测序', '蛋白设计'], 'RNA 测序'),
            (['alpha beta', 'gamma delta'], 'unmatched'),
        )
        for texts, query in cases:
            with self.subTest(texts=texts, query=query):
                vectorizer = TfidfVectorizer(
                    lowercase=True,
                    ngram_range=(1, 2),
                    token_pattern=r'(?u)\b\w+\b',
                )
                matrix = vectorizer.fit_transform(texts + [query])
                expected = cosine_similarity(matrix[-1], matrix[:-1]).ravel()
                actual = _small_corpus_scores(texts, query)
                for observed, reference in zip(actual, expected):
                    self.assertAlmostEqual(observed, reference, places=10)

    def test_large_corpus_keeps_sklearn_path(self):
        with tempfile.TemporaryDirectory(prefix='knowledge_large_') as raw:
            index = Path(raw) / 'index.json'
            index.write_text(json.dumps({
                'documents': [
                    {'id': str(number), 'text': f'gene {number}'}
                    for number in range(33)
                ],
            }), encoding='utf-8')
            with patch('src.knowledge_plugin._small_corpus_scores',
                       side_effect=AssertionError('small path used')):
                result = knowledge_search('gene 7', index, top_k=1)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['result']['matches'][0]['document_id'], '7')

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

    def test_build_graph_preserves_gene_evidence_source_trace(self):
        with tempfile.TemporaryDirectory(prefix='knowledge_graph_test_') as raw:
            output_path = Path(raw) / 'evidence_graph.json'
            result = run_tool('knowledge_build_graph', {
                'evidence': {
                    'result': {
                        'matches': [
                            {'gene_id': 'GeneA', 'source': 'local_fixture', 'title': 'Example evidence'},
                            {'gene_id': 'GeneB', 'source': 'pubmed', 'title': 'Published evidence'},
                        ],
                    },
                },
                'output_path': str(output_path),
            })
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['result']['metrics']['n_genes'], 2)
            self.assertEqual(result['result']['metrics']['n_evidence'], 2)
            self.assertEqual(result['result']['metrics']['n_sources'], 2)
            self.assertEqual(result['result']['metrics']['n_edges'], 4)
            self.assertTrue(output_path.is_file())


if __name__ == '__main__':
    unittest.main()
