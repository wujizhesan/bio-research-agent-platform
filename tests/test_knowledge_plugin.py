from copy import deepcopy
import json
import os
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.domain_registry import available_domains, run_tool
from src.knowledge_plugin import (
    _build_tfidf_index,
    _indexed_corpus_scores,
    _small_corpus_scores,
    knowledge_ingest_directory,
    knowledge_search,
)


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

    def test_indexed_scores_and_rankings_match_legacy_search(self):
        from sklearn.feature_extraction.text import TfidfVectorizer
        from sklearn.metrics.pairwise import cosine_similarity

        rng = random.Random(19)
        vocabulary = ('TP53', 'DNA', 'repair', 'RNA', 'design', '单细胞', '7')
        texts = [' '.join(rng.choices(vocabulary, k=12)) for _ in range(128)]
        texts += ['', '!!!', 'TP53 DNA repair', 'TP53 DNA repair', 'TP53 TP53']
        index = _build_tfidf_index(texts)
        with tempfile.TemporaryDirectory(prefix='knowledge_indexed_') as raw:
            legacy_path = Path(raw) / 'legacy.json'
            indexed_path = Path(raw) / 'indexed.json'
            payload = {'documents': [{'id': str(i), 'text': text} for i, text in enumerate(texts)]}
            legacy_path.write_text(json.dumps(payload), encoding='utf-8')
            indexed_path.write_text(json.dumps({**payload, 'tfidf_index': index}), encoding='utf-8')
            for query in ('TP53', 'DNA repair', '单细胞 RNA', 'TP53 TP53', 'unknown', '!!!'):
                with self.subTest(query=query):
                    matrix = TfidfVectorizer(
                        ngram_range=(1, 2), token_pattern=r'(?u)\b\w+\b',
                    ).fit_transform(texts + [query])
                    expected = cosine_similarity(matrix[-1], matrix[:-1]).ravel()
                    actual = _indexed_corpus_scores(texts, query, index)
                    for observed, reference in zip(actual, expected):
                        self.assertAlmostEqual(observed, reference, places=10)
                    self.assertEqual(
                        knowledge_search(query, indexed_path, top_k=15)['result']['matches'],
                        knowledge_search(query, legacy_path, top_k=15)['result']['matches'],
                    )

    def test_ingest_precomputes_large_index_without_sklearn_on_search(self):
        source = """import json
import sys
sys.modules['sklearn'] = None
from src.knowledge_plugin import knowledge_search
print(json.dumps(knowledge_search('Gene9', sys.argv[1])))
"""
        with tempfile.TemporaryDirectory(prefix='knowledge_precompute_') as raw:
            root = Path(raw)
            for number in range(32):
                (root / f'{number}.txt').write_text(f'Gene{number} DNA repair', encoding='utf-8')
            path = root / 'index.json'
            knowledge_ingest_directory(root, path)
            self.assertNotIn('tfidf_index', json.loads(path.read_text(encoding='utf-8')))
            (root / '32.txt').write_text('Gene32 DNA repair', encoding='utf-8')
            knowledge_ingest_directory(root, path)
            self.assertIn('tfidf_index', json.loads(path.read_text(encoding='utf-8')))
            process = subprocess.run(
                [sys.executable, '-c', source, str(path)],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, 'BIO_AGENT_ISOLATED_TOOL_CHILD': '1'},
                capture_output=True, text=True, timeout=20,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['result']['n_matches'], 1)
        self.assertEqual(result['result']['matches'][0]['document_id'], '9.txt')

    def test_stale_or_corrupt_statistics_fall_back_to_legacy_search(self):
        texts = [f'Gene{number} DNA repair' for number in range(33)]
        original = {
            'documents': [{'id': str(i), 'text': text} for i, text in enumerate(texts)],
            'tfidf_index': _build_tfidf_index(texts),
        }
        with tempfile.TemporaryDirectory(prefix='knowledge_stale_') as raw:
            indexed_path = Path(raw) / 'indexed.json'
            legacy_path = Path(raw) / 'legacy.json'
            for change in ('text', 'order', 'append', 'counts', 'version', 'shape'):
                with self.subTest(change=change):
                    payload = deepcopy(original)
                    if change == 'text':
                        payload['documents'][9]['text'] = 'changed evidence'
                    elif change == 'order':
                        payload['documents'].reverse()
                    elif change == 'append':
                        payload['documents'].append({'id': 'new', 'text': 'Gene9 evidence'})
                    elif change == 'counts':
                        row = payload['tfidf_index']['term_frequencies'][9]
                        row[0] = (row[0][0], 999)
                    elif change == 'version':
                        payload['tfidf_index']['version'] = 2
                    else:
                        payload['tfidf_index'] = ['invalid statistics']
                    indexed_path.write_text(json.dumps(payload), encoding='utf-8')
                    payload.pop('tfidf_index')
                    legacy_path.write_text(json.dumps(payload), encoding='utf-8')
                    self.assertEqual(
                        knowledge_search('Gene9', indexed_path)['result']['matches'],
                        knowledge_search('Gene9', legacy_path)['result']['matches'],
                    )

    def test_empty_vocabulary_keeps_legacy_error(self):
        texts = ['!!!'] * 33
        with tempfile.TemporaryDirectory(prefix='knowledge_empty_') as raw:
            path = Path(raw) / 'index.json'
            path.write_text(json.dumps({
                'documents': [{'text': text} for text in texts],
                'tfidf_index': _build_tfidf_index(texts),
            }), encoding='utf-8')
            with self.assertRaisesRegex(ValueError, 'empty vocabulary'):
                knowledge_search('???', path)

    def test_cached_index_tracks_content_changes_with_preserved_file_metadata(self):
        texts = [f'Gene{number} DNA repair' for number in range(33)]
        payload = {
            'documents': [{'id': str(i), 'text': text} for i, text in enumerate(texts)],
            'tfidf_index': _build_tfidf_index(texts),
        }
        with tempfile.TemporaryDirectory(prefix='knowledge_change_') as raw:
            path = Path(raw) / 'index.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            original_stat = path.stat()
            self.assertEqual(knowledge_search('Gene9', path)['result']['n_matches'], 1)
            payload['documents'][9]['text'] = 'GeneZ DNA repair'
            path.write_text(json.dumps(payload), encoding='utf-8')
            os.utime(path, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
            self.assertEqual(path.stat().st_size, original_stat.st_size)
            self.assertEqual(knowledge_search('Gene9', path)['result']['n_matches'], 0)

    def test_returned_citations_do_not_mutate_cached_index(self):
        texts = [f'Gene{number} DNA repair' for number in range(33)]
        payload = {
            'documents': [
                {'id': str(i), 'text': text, 'title': {'label': 'original'}}
                for i, text in enumerate(texts)
            ],
            'tfidf_index': _build_tfidf_index(texts),
        }
        with tempfile.TemporaryDirectory(prefix='knowledge_citations_') as raw:
            path = Path(raw) / 'index.json'
            path.write_text(json.dumps(payload), encoding='utf-8')
            result = knowledge_search('Gene9', path)
            result['result']['matches'][0]['title']['label'] = 'modified'
            self.assertEqual(
                knowledge_search('Gene9', path)['result']['matches'][0]['title']['label'],
                'original',
            )

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
