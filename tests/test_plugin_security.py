import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src import domain_registry
from src.plugin_manifest import build_manifest, validate_install_candidate
from src.plugin_health import assess_plugin_health
from src.plugin_registry import DomainRegistry
from src.job_execution import _sandbox_environment
from src.plugin_security import (
    PluginSecurityError,
    enforce_plugin_import_boundary,
    normalize_permissions,
    sandbox_environment,
)


def plugin():
    return SimpleNamespace(
        __name__='safe_plugin',
        PLUGIN_NAME='Safe plugin',
        PLUGIN_VERSION='1.0.0',
        PLUGIN_API_VERSION=1,
    )


def tool(function, permissions=None, properties=None):
    return {
        'description': 'Run sandboxed plugin',
        'parameters': {
            'type': 'object',
            'properties': properties or {},
            'additionalProperties': False,
        },
        'returns': {},
        'permissions': permissions or {},
        'function': function,
    }


class PluginSecurityTests(unittest.TestCase):
    def run_entry_point(self, definition, arguments=None):
        registry = DomainRegistry()
        registry.register(
            'safe_plugin', plugin(), {'run': definition}, kind='entry_point'
        )
        with (
            patch.object(domain_registry, 'REGISTRY', registry),
            patch.object(domain_registry, '_plugin_enabled', return_value=True),
            patch.dict(os.environ, {'PLUGIN_SANDBOX_PROCESS': '1'}, clear=False),
        ):
            return domain_registry.run_tool('safe_plugin_run', arguments or {})

    def test_entry_point_manifest_defaults_to_isolated_zero_trust(self):
        manifest = build_manifest(
            'safe_plugin', plugin(), {'run': tool(lambda: {'ok': True})},
            kind='entry_point',
        )
        self.assertEqual(manifest['security']['trust'], 'sandboxed')
        self.assertEqual(manifest['security']['execution'], 'isolated_process')
        self.assertEqual(
            manifest['tool_contracts']['run']['permissions'],
            {
                'filesystem': {'read': [], 'write': []},
                'network': [],
                'subprocess': [],
                'environment': [],
            },
        )

    def test_permission_change_changes_contract_digest(self):
        properties = {'input_path': {'type': 'string'}}
        first = build_manifest(
            'safe_plugin', plugin(),
            {'run': tool(lambda input_path: None, properties=properties)},
            kind='entry_point',
        )
        second = build_manifest(
            'safe_plugin', plugin(),
            {'run': tool(
                lambda input_path: None,
                {'filesystem': {'read': ['input_path']}},
                properties,
            )},
            kind='entry_point',
        )
        self.assertNotEqual(first['contract_digest'], second['contract_digest'])

    def test_permissions_reject_unknown_paths_wildcards_and_shells(self):
        parameters = {
            'type': 'object',
            'properties': {'input_path': {'type': 'string'}},
        }
        with self.assertRaisesRegex(ValueError, 'unknown parameters'):
            normalize_permissions(
                {'filesystem': {'read': ['missing_path']}}, parameters
            )
        with self.assertRaisesRegex(ValueError, 'invalid network host'):
            normalize_permissions({'network': ['*']}, parameters)
        with self.assertRaisesRegex(ValueError, 'unsafe network host'):
            normalize_permissions({'network': ['127.0.0.1']}, parameters)
        with self.assertRaisesRegex(ValueError, 'unsafe subprocess'):
            normalize_permissions({'subprocess': ['cmd.exe']}, parameters)

    def test_direct_url_requirement_is_rejected(self):
        candidate = plugin()
        candidate.PLUGIN_REQUIREMENTS = ('demo @ https://example.org/demo.whl',)
        with self.assertRaisesRegex(ValueError, 'direct URLs'):
            build_manifest(
                'safe_plugin', candidate,
                {'run': tool(lambda: None)}, kind='entry_point',
            )

    def test_unapproved_declared_capability_blocks_install(self):
        manifest = build_manifest(
            'safe_plugin', plugin(),
            {'run': tool(lambda: None, {'network': ['api.example.org']})},
            kind='entry_point',
        )
        with patch.dict(os.environ, {'PLUGIN_ALLOWED_NETWORK_HOSTS': ''}, clear=False):
            report = validate_install_candidate(manifest)
        self.assertFalse(report['compatible'])
        self.assertEqual(report['permissions']['denied']['network'], ['api.example.org'])

    def test_untrusted_plugin_cannot_run_inline(self):
        called = []
        registry = DomainRegistry()
        registry.register(
            'safe_plugin', plugin(),
            {'run': tool(lambda: called.append(True))}, kind='entry_point',
        )
        clean = {
            'PLUGIN_SANDBOX_PROCESS': '',
            'PLUGIN_ALLOW_UNTRUSTED_INLINE': '',
        }
        with (
            patch.object(domain_registry, 'REGISTRY', registry),
            patch.object(domain_registry, '_plugin_enabled', return_value=True),
            patch.dict(os.environ, clean, clear=False),
        ):
            result = domain_registry.run_tool('safe_plugin_run', {})
        self.assertEqual(result['error_type'], 'plugin_security')
        self.assertEqual(called, [])

    def test_declared_input_file_is_readable(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / 'input.txt'
            source.write_text('sequence', encoding='utf-8')
            result = self.run_entry_point(
                tool(
                    lambda input_path: {'value': Path(input_path).read_text(encoding='utf-8')},
                    {'filesystem': {'read': ['input_path']}},
                    {'input_path': {'type': 'string'}},
                ),
                {'input_path': str(source)},
            )
        self.assertEqual(result, {'value': 'sequence'})

    def test_undeclared_file_read_and_write_are_denied(self):
        source_file = Path(__file__).resolve()
        read_result = self.run_entry_point(
            tool(lambda: source_file.read_text(encoding='utf-8'))
        )
        target = source_file.with_name('plugin-security-escape.tmp')
        target.unlink(missing_ok=True)
        write_result = self.run_entry_point(
            tool(lambda: target.write_text('escape', encoding='utf-8'))
        )
        self.assertEqual(read_result['error_type'], 'plugin_security')
        self.assertEqual(write_result['error_type'], 'plugin_security')
        self.assertFalse(target.exists())

    def test_subprocess_is_denied(self):
        def run_process():
            subprocess.run([sys.executable, '-c', 'pass'], check=True)

        process_result = self.run_entry_point(tool(run_process))
        self.assertEqual(process_result['error_type'], 'plugin_security')

    def test_network_connection_is_denied_before_connect(self):
        def connect():
            with socket.socket() as client:
                client.connect(('127.0.0.1', 1))

        result = self.run_entry_point(tool(connect))
        self.assertEqual(result['error_type'], 'plugin_security')

    def test_connectionless_network_send_is_denied(self):
        def send():
            with socket.socket(type=socket.SOCK_DGRAM) as client:
                client.sendto(b'data', ('127.0.0.1', 9))

        result = self.run_entry_point(tool(send))
        self.assertEqual(result['error_type'], 'plugin_security')

    def test_dynamic_library_loading_is_denied(self):
        def load_library():
            import ctypes
            ctypes.CDLL('kernel32.dll')

        result = self.run_entry_point(tool(load_library))
        self.assertEqual(result['error_type'], 'plugin_security')

    def test_sandbox_environment_only_exposes_approved_values(self):
        permissions = normalize_permissions({
            'network': ['api.example.org'],
            'subprocess': ['python.exe'],
            'environment': ['SCI_TOKEN'],
        })
        base = {
            'PATH': 'bin',
            'SECRET_TOKEN': 'hidden',
            'SCI_TOKEN': 'approved',
        }
        controls = {
            'PLUGIN_ALLOWED_NETWORK_HOSTS': 'api.example.org',
            'PLUGIN_ALLOWED_EXECUTABLES': 'python.exe',
            'PLUGIN_ALLOWED_ENV_VARS': 'SCI_TOKEN',
        }
        with patch.dict(os.environ, controls, clear=False):
            environment = sandbox_environment(
                base, {'trust': 'sandboxed'}, permissions
            )
        self.assertNotIn('SECRET_TOKEN', environment)
        self.assertEqual(environment['SCI_TOKEN'], 'approved')
        self.assertEqual(environment['PLUGIN_SANDBOX_PROCESS'], '1')
        self.assertEqual(
            environment['PLUGIN_ALLOWED_NETWORK_HOSTS'], 'api.example.org'
        )

    def test_executor_selects_only_target_plugin_and_private_temp(self):
        spec = {
            'name': 'safe_plugin_run',
            'domain': 'safe_plugin',
            'plugin_security': {'trust': 'sandboxed'},
            'permissions': normalize_permissions({}),
        }
        with tempfile.TemporaryDirectory() as temporary:
            with patch(
                'src.domain_registry.active_tool_specs', return_value=[spec]
            ):
                environment = _sandbox_environment(
                    'safe_plugin_run', temporary
                )
            plugin_temp = Path(environment['TEMP'])
            self.assertEqual(
                environment['PLUGIN_SANDBOX_DOMAIN'], 'safe_plugin'
            )
            self.assertEqual(plugin_temp.parent, Path(temporary))
            self.assertTrue(plugin_temp.is_dir())

    def test_sandbox_discovery_loads_only_target_entry_point(self):
        loaded = []
        source = plugin()
        source.TOOLS = {'run': tool(lambda: None)}
        entry_points = [
            SimpleNamespace(
                name=name,
                value=f'{name}.plugin',
                load=lambda name=name: loaded.append(name) or source,
            )
            for name in ('safe_plugin', 'other_plugin')
        ]
        with (
            patch('src.domain_registry.entry_points', return_value=entry_points),
            patch.dict(
                os.environ,
                {'PLUGIN_SANDBOX_DOMAIN': 'safe_plugin'},
                clear=False,
            ),
        ):
            discovered, _, errors = domain_registry._discover_external_domains(
                reserved_domains=set()
            )
        self.assertEqual(set(discovered), {'safe_plugin'})
        self.assertEqual(errors, {})
        self.assertEqual(loaded, ['safe_plugin'])

    def test_third_party_health_callable_never_runs_inline(self):
        called = []
        source = plugin()
        source.health_check = lambda: called.append(True) or {'healthy': True}
        manifest = build_manifest(
            'safe_plugin', source,
            {'run': tool(lambda: None)}, kind='entry_point',
        )
        result = assess_plugin_health('safe_plugin', source, manifest)
        self.assertTrue(result['healthy'])
        self.assertEqual(result['source'], 'manifest')
        self.assertEqual(called, [])

    def test_import_boundary_hides_secrets_and_denies_project_reads(self):
        source_file = Path(__file__).resolve()
        with patch.dict(os.environ, {'PLUGIN_TEST_SECRET': 'hidden'}, clear=False):
            with self.assertRaises(PluginSecurityError):
                with enforce_plugin_import_boundary('safe_plugin'):
                    self.assertNotIn('PLUGIN_TEST_SECRET', os.environ)
                    source_file.read_text(encoding='utf-8')
            self.assertEqual(os.environ['PLUGIN_TEST_SECRET'], 'hidden')


if __name__ == '__main__':
    unittest.main()
