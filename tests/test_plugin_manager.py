import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.api_server import route_request
from src import plugin_manager
from src import agent
from src.domain_registry import active_tool_specs, tool_specs
from src.job_manager import JobManager
from src.plugin_manager import PluginManager, is_domain_enabled


class PluginManagerTests(unittest.TestCase):
    def _manager(self, root):
        return PluginManager(
            state_path=Path(root) / 'plugin_state.json',
            catalog_loader=lambda: [
                {
                    'domain': 'demo',
                    'name': 'Demo plugin',
                    'status': 'available',
                    'tools': ['run'],
                    'tool_count': 1,
                },
                {
                    'domain': 'missing',
                    'name': 'Missing plugin',
                    'status': 'unavailable',
                    'tools': [],
                    'tool_count': 0,
                },
            ],
        )

    def test_enable_disable_state_persists(self):
        with tempfile.TemporaryDirectory(prefix='plugin_state_') as raw:
            manager = self._manager(raw)
            self.assertTrue(manager.get('demo')['enabled'])
            disabled = manager.disable('demo')
            self.assertFalse(disabled['enabled'])
            self.assertFalse(is_domain_enabled('demo', Path(raw) / 'plugin_state.json', manager.catalog_loader))
            reloaded = self._manager(raw)
            self.assertFalse(reloaded.get('demo')['enabled'])
            self.assertTrue(reloaded.enable('demo')['enabled'])

    def test_concurrent_state_updates_preserve_all_domains(self):
        from concurrent.futures import ThreadPoolExecutor

        with tempfile.TemporaryDirectory(prefix='plugin_state_') as raw:
            state = Path(raw) / 'plugin_state.json'
            catalog = lambda: [
                {
                    'domain': 'demo',
                    'name': 'Demo plugin',
                    'status': 'available',
                    'tools': ['run'],
                    'tool_count': 1,
                },
                {
                    'domain': 'other',
                    'name': 'Other plugin',
                    'status': 'available',
                    'tools': ['run'],
                    'tool_count': 1,
                },
            ]
            managers = [
                PluginManager(state_path=state, catalog_loader=catalog),
                PluginManager(state_path=state, catalog_loader=catalog),
            ]
            with ThreadPoolExecutor(max_workers=2) as executor:
                futures = [
                    executor.submit(managers[0].disable, 'demo'),
                    executor.submit(managers[1].disable, 'other'),
                ]
                for future in futures:
                    future.result()
            reloaded = PluginManager(state_path=state, catalog_loader=catalog)
            self.assertFalse(reloaded.get('demo')['enabled'])
            self.assertFalse(reloaded.get('other')['enabled'])
            self.assertEqual(json.loads(state.read_text(encoding='utf-8'))['version'], 1)
            self.assertEqual(list(Path(raw).glob('plugin_state.json.*.tmp')), [])
    def test_unknown_and_unavailable_plugins_are_rejected(self):
        with tempfile.TemporaryDirectory(prefix='plugin_state_') as raw:
            manager = self._manager(raw)
            self.assertFalse(manager.get('missing')['enabled'])
            with self.assertRaises(ValueError):
                manager.enable('missing')
            with self.assertRaises(ValueError):
                manager.disable('unknown')

    def test_disabled_domain_is_hidden_from_active_specs(self):
        with tempfile.TemporaryDirectory(prefix='plugin_state_') as raw:
            state = Path(raw) / 'plugin_state.json'
            manager = PluginManager(state_path=state)
            manager.disable('sequence')
            with patch.object(plugin_manager, 'DEFAULT_STATE_PATH', state):
                self.assertGreater(len(tool_specs('sequence')), 0)
                self.assertEqual(active_tool_specs('sequence'), [])

    def test_job_submission_rejects_unknown_tool(self):
        with tempfile.TemporaryDirectory(prefix='plugin_state_') as raw:
            jobs = JobManager(max_workers=1, store_path=Path(raw) / 'jobs.sqlite3')
            try:
                with self.assertRaisesRegex(ValueError, 'unknown tool'):
                    jobs.submit('does_not_exist', {})
            finally:
                jobs.shutdown()

    def test_job_submission_rejects_disabled_tool(self):
        with tempfile.TemporaryDirectory(prefix='plugin_state_') as raw:
            state = Path(raw) / 'plugin_state.json'
            manager = PluginManager(state_path=state)
            manager.disable('sequence')
            jobs = JobManager(max_workers=1, store_path=Path(raw) / 'jobs.sqlite3')
            try:
                with patch.object(plugin_manager, 'DEFAULT_STATE_PATH', state):
                    with self.assertRaises(ValueError):
                        jobs.submit('sequence_score', {'mrna': 'AUG'})
            finally:
                jobs.shutdown()
    def test_cadd_direct_entrypoints_respect_disabled_state(self):
        with tempfile.TemporaryDirectory(prefix='plugin_state_') as raw:
            state = Path(raw) / 'plugin_state.json'
            manager = PluginManager(state_path=state)
            manager.disable('cadd')
            with patch.object(plugin_manager, 'DEFAULT_STATE_PATH', state):
                self.assertEqual(agent.select_tools('cadd'), {})
                result = agent.run_tool('read_results', '{}')
                self.assertEqual(result['status'], 'error')
                self.assertIn('disabled', result['error'])
                result = __import__('src.streamlit_chat', fromlist=['execute_tool']).execute_tool('read_results', {}, 'cadd')
                self.assertEqual(result['status'], 'error')
                self.assertIn('disabled', result['error'])

    def test_disabled_cadd_does_not_emit_llm_tools(self):
        with patch.object(agent, 'API_KEY', 'test-key'):
            with patch.object(agent.urllib.request, 'urlopen') as urlopen:
                response = urlopen.return_value.__enter__.return_value
                response.read.return_value = b'{"choices": []}'
                agent.call_llm([], {})
                payload = json.loads(urlopen.call_args.args[0].data)
                self.assertEqual(payload['tools'], [])

    def test_http_routes_expose_lifecycle(self):
        with tempfile.TemporaryDirectory(prefix='plugin_state_') as raw:
            manager = self._manager(raw)
            status, payload = route_request('GET', '/plugins/demo', plugin_manager=manager)
            self.assertEqual(status, 200)
            self.assertTrue(payload['plugin']['enabled'])
            status, payload = route_request(
                'POST', '/plugins/demo/disable', plugin_manager=manager
            )
            self.assertEqual(status, 200)
            self.assertFalse(payload['plugin']['enabled'])


if __name__ == '__main__':
    unittest.main()
