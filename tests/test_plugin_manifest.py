import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.domain_registry import _discover_external_domains, domain_catalog
from src.plugin_manifest import build_manifest, validate_manifest


class PluginManifestTests(unittest.TestCase):
    def test_domain_catalog_exposes_valid_manifests(self):
        for item in domain_catalog():
            manifest = item['manifest']
            self.assertEqual(manifest['key'], item['domain'])
            self.assertEqual(manifest['tool_count'], len(manifest['tools']))
            self.assertEqual(manifest['tools'], item['tools'])
            self.assertEqual(validate_manifest(manifest), manifest)

    def test_broken_external_entry_point_is_quarantined(self):
        entry_point = SimpleNamespace(
            name='broken_plugin',
            value='broken.module:plugin',
            load=lambda: (_ for _ in ()).throw(RuntimeError('import failed')),
        )
        with patch('src.domain_registry.entry_points', return_value=[entry_point]):
            discovered, sources, errors = _discover_external_domains()
        self.assertEqual(discovered, {})
        self.assertEqual(sources, {})
        self.assertEqual(errors['broken_plugin']['status'], 'error')
        self.assertIn('import failed', errors['broken_plugin']['reason'])

    def test_error_manifest_can_have_no_tools(self):
        plugin = SimpleNamespace(
            PLUGIN_NAME='Broken plugin',
            PLUGIN_VERSION='unknown',
            PLUGIN_API_VERSION=1,
            PLUGIN_CAPABILITIES=(),
        )
        manifest = build_manifest(
            'broken_plugin',
            plugin,
            {},
            kind='entry_point',
            status='error',
            health={'reason': 'import failed'},
        )
        self.assertEqual(manifest['status'], 'error')
        self.assertEqual(manifest['tool_count'], 0)
    def test_declared_manifest_can_override_metadata(self):
        plugin = SimpleNamespace(
            PLUGIN_NAME='Test plugin',
            PLUGIN_VERSION='2.0.0',
            PLUGIN_API_VERSION=1,
            PLUGIN_CAPABILITIES=('test.run',),
            PLUGIN_MANIFEST={'domains': ['test'], 'entrypoint': 'test.plugin'},
        )
        manifest = build_manifest(
            'test',
            plugin,
            {'run': {'description': 'run', 'parameters': {}, 'function': lambda: None}},
            kind='external',
        )
        self.assertEqual(manifest['name'], 'Test plugin')
        self.assertEqual(manifest['version'], '2.0.0')
        self.assertEqual(manifest['entrypoint'], 'test.plugin')
        self.assertEqual(manifest['capabilities'], ['test.run'])

    def test_manifest_rejects_incompatible_api(self):
        with self.assertRaises(ValueError):
            validate_manifest({
                'manifest_version': 1,
                'key': 'test',
                'name': 'Test',
                'version': '1.0.0',
                'api_version': 2,
                'kind': 'external',
                'status': 'available',
                'entrypoint': 'test.plugin',
                'domains': ['test'],
                'capabilities': [],
                'tools': ['run'],
                'tool_count': 1,
            })


if __name__ == '__main__':
    unittest.main()
