import unittest
import json
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import src.api_server as api_server
from src.api_server import is_authorized, route_request
from src.job_manager import JobManager


class ApiServerTests(unittest.TestCase):
    def test_configured_api_token_protects_non_health_requests(self):
        with patch.dict('os.environ', {'CADD_API_TOKEN': 'secret-token'}, clear=False):
            self.assertTrue(is_authorized('/health', {}))
            self.assertFalse(is_authorized('/jobs', {}))
            self.assertFalse(is_authorized('/jobs', {'Authorization': 'Bearer wrong'}))
            self.assertTrue(is_authorized('/jobs', {'Authorization': 'Bearer secret-token'}))

    def test_health_and_plugin_catalog(self):
        status, health = route_request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['status'], 'ok')

        status, payload = route_request('GET', '/plugins')
        self.assertEqual(status, 200)
        domains = {item['domain'] for item in payload['plugins']}
        self.assertTrue({'cadd', 'omics', 'research', 'literature', 'knowledge', 'imaging'}.issubset(domains))

    def test_read_only_routes_do_not_initialize_legacy_job_manager(self):
        with patch.object(api_server, '_default_job_manager', side_effect=AssertionError('job manager should be lazy')):
            status, _ = route_request('GET', '/health')
            self.assertEqual(status, 200)
            status, _ = route_request('GET', '/runs')
            self.assertEqual(status, 200)

    def test_tools_are_filtered_by_domain(self):
        status, payload = route_request('GET', '/tools?domain=knowledge')
        self.assertEqual(status, 200)
        self.assertEqual({item['name'] for item in payload['tools']}, {
            'knowledge_ingest_directory',
            'knowledge_search',
            'knowledge_build_graph',
        })

    def test_run_supports_registry_tools(self):
        status, payload = route_request('POST', '/run', {
            'tool': 'literature_search',
            'arguments': {
                'gene_ids': ['GeneA'],
                'provider': 'local',
                'evidence_csv': 'examples/rnaseq/evidence.csv',
            },
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload['status'], 'ok')
        self.assertEqual(payload['result']['n_matches'], 1)

        status, payload = route_request('POST', '/run/knowledge_search', {})
        self.assertEqual(status, 400)
        self.assertEqual(payload['status'], 'error')
    def test_runs_are_listed_and_path_traversal_is_rejected(self):
        with tempfile.TemporaryDirectory(prefix='bio_runs_') as raw:
            root = Path(raw)
            manifest = root / 'bgi' / 'research_manifest.json'
            manifest.parent.mkdir()
            manifest.write_text(json.dumps({
                'workflow': 'bgi-research-demo',
                'status': 'completed',
                'dry_run': False,
                'completed_steps': 8,
                'failed_steps': 0,
            }), encoding='utf-8')

            status, payload = route_request('GET', '/runs?limit=5', output_root=root)
            self.assertEqual(status, 200)
            self.assertEqual(payload['runs'][0]['run_id'], 'bgi/research_manifest.json')
            self.assertEqual(payload['runs'][0]['completed_steps'], 8)

            status, payload = route_request('GET', '/runs/bgi/research_manifest.json', output_root=root)
            self.assertEqual(status, 200)
            self.assertEqual(payload['manifest']['workflow'], 'bgi-research-demo')

            status, payload = route_request('GET', '/runs/../outside.json', output_root=root)
            self.assertEqual(status, 404)
            self.assertEqual(payload['status'], 'error')
    def test_async_job_submission_and_polling(self):
        manager = JobManager(max_workers=1)
        try:
            status, payload = route_request('POST', '/jobs', {
                'tool': 'literature_search',
                'arguments': {
                    'gene_ids': ['GeneA'],
                    'provider': 'local',
                    'evidence_csv': 'examples/rnaseq/evidence.csv',
                },
            }, job_manager=manager)
            self.assertEqual(status, 202)
            job_id = payload['job']['job_id']
            self.assertIn(payload['job']['status'], {'queued', 'running', 'completed'})

            terminal = None
            for _ in range(100):
                status, current = route_request('GET', f'/jobs/{job_id}', job_manager=manager)
                self.assertEqual(status, 200)
                terminal = current['job']
                if terminal['status'] in {'completed', 'failed'}:
                    break
                time.sleep(0.01)
            self.assertEqual(terminal['status'], 'completed')
            self.assertEqual(terminal['result']['result']['n_matches'], 1)

            status, current = route_request('GET', '/jobs?limit=5', job_manager=manager)
            self.assertEqual(status, 200)
            self.assertEqual(current['jobs'][0]['job_id'], job_id)
            status, retried = route_request('POST', '/jobs/' + job_id + '/retry', job_manager=manager)
            self.assertEqual(status, 202)
            self.assertEqual(retried['job']['retry_of'], job_id)
        finally:
            manager.shutdown()

if __name__ == '__main__':
    unittest.main()
