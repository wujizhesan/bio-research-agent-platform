import unittest
from unittest.mock import patch

from src.domain_registry import tool_specs
from src.execution_identity import (
    build_execution_identity,
    identity_mismatches,
    routing_descriptor,
)


class ExecutionIdentityTests(unittest.TestCase):
    def test_identity_pins_release_image_contract_implementation_and_toolchain(self):
        spec = next(item for item in tool_specs() if item['name'] == 'research_catalog')
        image = 'ghcr.io/example/backend@sha256:' + 'b' * 64
        with patch.dict('os.environ', {
            'APP_GIT_SHA': 'a' * 40,
            'APP_IMAGE_REFERENCE': image,
        }):
            identity = build_execution_identity(spec)
        self.assertEqual(identity['git_commit'], 'a' * 40)
        self.assertEqual(identity['worker_image_digest'], 'sha256:' + 'b' * 64)
        self.assertEqual(identity['plugin_contract_digest'], spec['plugin_contract_digest'])
        self.assertEqual(len(identity['plugin_implementation_sha256']), 64)
        self.assertEqual(len(identity['external_toolchain_fingerprint']), 64)
        self.assertEqual(len(identity['fingerprint']), 64)

    def test_identity_comparison_and_gpu_route_are_deterministic(self):
        expected = {
            'git_commit': 'a',
            'worker_image_digest': 'sha256:a',
            'plugin_version': '1.0.0',
            'plugin_api_version': 1,
            'plugin_contract_digest': 'contract',
            'plugin_implementation_sha256': 'implementation',
            'external_toolchain_fingerprint': 'tools',
            'fingerprint': 'all',
        }
        actual = dict(expected)
        actual['plugin_implementation_sha256'] = 'changed'
        self.assertEqual(
            identity_mismatches(expected, actual),
            ['plugin_implementation_sha256'],
        )
        route = routing_descriptor(
            'omics_run_variant_calling',
            {'gpu_count': 1, 'gpu_memory_mb': 4096, 'labels': ['cuda12']},
            {'plugin_domain': 'omics', 'plugin_version': '0.7.0', 'fingerprint': 'all'},
        )
        self.assertEqual(route['resource_class'], 'gpu')
        self.assertEqual(route['required_labels'], ['cuda12'])
        self.assertEqual(len(route['route_id']), 32)

    def test_production_rejects_mutable_or_unknown_deployment_identity(self):
        spec = next(item for item in tool_specs() if item['name'] == 'research_catalog')
        with patch.dict('os.environ', {
            'APP_ENV': 'production',
            'APP_GIT_SHA': 'unknown',
            'APP_IMAGE_REFERENCE': 'ghcr.io/example/backend:latest',
        }):
            with self.assertRaisesRegex(ValueError, 'execution identity is incomplete'):
                build_execution_identity(spec)


if __name__ == '__main__':
    unittest.main()
