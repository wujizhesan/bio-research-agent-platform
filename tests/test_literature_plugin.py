import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from src.domain_registry import available_domains, run_tool
from src.evidence_providers import UniProtEvidenceProvider, _write_cache


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

    def test_cached_uniprot_search_runs_without_pandas(self):
        source = """import json
import sys
from unittest.mock import patch
sys.modules['pandas'] = None
from src.literature_plugin import literature_search
with patch('src.evidence_providers.requests.get', side_effect=AssertionError('unexpected HTTP request')):
    result = literature_search(['TP53'], provider='uniprot', cache_dir=sys.argv[1])
print(json.dumps(result))
"""
        with tempfile.TemporaryDirectory(prefix='literature_cache_') as raw:
            provider = UniProtEvidenceProvider(cache_dir=raw)
            _write_cache(
                provider._cache_path('TP53'),
                {'results': [{'primaryAccession': 'P04637', 'uniProtkbId': 'P53_HUMAN'}]},
                provider='uniprot',
                request={'gene_id': 'TP53', 'organism_id': 9606},
            )
            process = subprocess.run(
                [sys.executable, '-c', source, raw],
                cwd=Path(__file__).resolve().parents[1],
                env={**os.environ, 'EVIDENCE_CACHE_MODE': 'frozen', 'BIO_AGENT_ISOLATED_TOOL_CHILD': '1'},
                capture_output=True, text=True, timeout=20,
            )
        self.assertEqual(process.returncode, 0, process.stderr)
        result = json.loads(process.stdout)
        self.assertEqual(result['status'], 'ok')
        self.assertEqual(result['result']['n_matches'], 1)
        self.assertEqual(result['result']['matches'][0]['accession'], 'P04637')


if __name__ == '__main__':
    unittest.main()
