import io
import json
import tempfile
import time
import unittest
import warnings
from datetime import date
from pathlib import Path
from unittest.mock import Mock, patch

import src.api_server as api_server
from src.api_server import is_authorized, route_request
from src.job_manager import JobManager


class ApiServerTests(unittest.TestCase):
    def test_legacy_api_has_fixed_removal_date_and_successor(self):
        lifecycle = api_server.legacy_api_lifecycle(date(2026, 12, 30))
        self.assertEqual(lifecycle['removal_date'], '2026-12-31')
        self.assertFalse(lifecycle['expired'])
        self.assertEqual(lifecycle['successor'], 'FastAPI')
        headers = api_server.legacy_api_response_headers()
        self.assertEqual(headers['Deprecation'], 'true')
        self.assertEqual(headers['Sunset'], 'Thu, 31 Dec 2026 00:00:00 GMT')
        self.assertIn('bio-agent-api', headers['X-API-Successor'])

    def test_legacy_api_warns_before_and_stops_on_removal_date(self):
        with patch.dict('os.environ', {'APP_ENV': 'development'}, clear=False):
            with warnings.catch_warnings(record=True) as captured:
                warnings.simplefilter('always')
                lifecycle = api_server.ensure_legacy_api_available(
                    date(2026, 12, 30)
                )
            self.assertFalse(lifecycle['expired'])
            self.assertTrue(any(
                item.category is FutureWarning and '2026-12-31' in str(item.message)
                for item in captured
            ))
            with self.assertRaisesRegex(SystemExit, 'reached its removal date'):
                api_server.ensure_legacy_api_available(date(2026, 12, 31))

    def test_legacy_http_responses_include_deprecation_headers(self):
        handler = api_server.BioAPIHandler.__new__(api_server.BioAPIHandler)
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.wfile = io.BytesIO()
        handler._write(200, {'status': 'ok'})
        handler.send_header.assert_any_call('Deprecation', 'true')
        handler.send_header.assert_any_call(
            'Sunset', 'Thu, 31 Dec 2026 00:00:00 GMT'
        )
        handler.send_header.assert_any_call(
            'X-API-Successor', 'FastAPI; command="bio-agent-api"'
        )

    def test_running_legacy_server_returns_gone_after_removal_date(self):
        handler = api_server.BioAPIHandler.__new__(api_server.BioAPIHandler)
        handler._write = Mock()
        with patch.object(
            api_server, '_utc_today', return_value=date(2026, 12, 31)
        ):
            self.assertFalse(handler._check_auth())
        status, payload = handler._write.call_args.args[:2]
        self.assertEqual(status, 410)
        self.assertTrue(payload['lifecycle']['expired'])
        self.assertIn('bio-agent-api', payload['error'])

    def test_configured_api_token_protects_non_health_requests(self):
        with patch.dict('os.environ', {'CADD_API_TOKEN': 'secret-token'}, clear=False):
            self.assertTrue(is_authorized('/health', {}))
            self.assertFalse(is_authorized('/jobs', {}))
            self.assertFalse(is_authorized('/jobs', {'Authorization': 'Bearer wrong'}))
            self.assertTrue(is_authorized('/jobs', {'Authorization': 'Bearer secret-token'}))

    def test_legacy_server_denies_non_health_routes_in_production(self):
        with patch.dict('os.environ', {
            'APP_ENV': 'production',
            'CADD_API_TOKEN': 'legacy',
        }, clear=False):
            self.assertTrue(is_authorized('/health', {}))
            self.assertFalse(is_authorized(
                '/jobs', {'Authorization': 'Bearer legacy'}
            ))
            with self.assertRaisesRegex(SystemExit, 'disabled in production'):
                api_server.ensure_legacy_api_available(date(2026, 9, 7))

    def test_health_and_plugin_catalog(self):
        status, health = route_request('GET', '/health')
        self.assertEqual(status, 200)
        self.assertEqual(health['status'], 'ok')
        self.assertTrue(health['lifecycle']['deprecated'])
        self.assertEqual(health['lifecycle']['removal_date'], '2026-12-31')

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
