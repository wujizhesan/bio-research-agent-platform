from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.reproducibility import (
    runtime_snapshot,
    seed_snapshot,
    verify_manifest,
)
from src.workflow_runner import run_workflow


def demo_function():
    return None


def changed_demo_function():
    return None


def specs(function=demo_function):
    return [{
        'name': 'demo_run',
        'domain': 'demo',
        'description': 'run',
        'parameters': {
            'type': 'object',
            'properties': {
                'input_path': {'type': 'string'},
                'output_path': {'type': 'string'},
                'random_seed': {'type': 'integer'},
            },
            'additionalProperties': False,
        },
        'returns': {},
        'resources': {},
        'plugin_version': '1.0.0',
        'plugin_api_version': 1,
        'plugin_contract_digest': 'contract-v1',
        'function': function,
    }]


class ReproducibilityTests(unittest.TestCase):
    def test_runtime_snapshot_uses_allowlist_and_stable_fingerprint(self):
        with patch.dict('os.environ', {
            'PYTHONHASHSEED': '42',
            'SECRET_API_KEY': 'must-not-leak',
        }, clear=False):
            first = runtime_snapshot()
            second = runtime_snapshot()
        self.assertEqual(first['fingerprint'], second['fingerprint'])
        self.assertEqual(first['environment']['PYTHONHASHSEED'], '42')
        self.assertNotIn('SECRET_API_KEY', first['environment'])

    def test_seed_snapshot_reports_declared_and_missing_seed(self):
        schema = specs()[0]['parameters']
        declared = seed_snapshot({'random_seed': 7}, schema)
        missing = seed_snapshot({}, schema)
        self.assertEqual(declared['status'], 'declared')
        self.assertEqual(declared['values'], {'random_seed': 7})
        self.assertEqual(missing['status'], 'unspecified')

    def test_workflow_manifest_captures_and_verifies_research_evidence(self):
        with tempfile.TemporaryDirectory(prefix='reproducibility_') as raw:
            root = Path(raw)
            source = root / 'input.txt'
            artifact = root / 'output.txt'
            checkpoint = root / 'manifest.json'
            source.write_text('input-v1', encoding='utf-8')
            workflow = {'name': 'evidence', 'steps': [{
                'id': 'analyze',
                'tool': 'demo_run',
                'args': {
                    'input_path': str(source),
                    'output_path': str(artifact),
                    'random_seed': 42,
                },
            }]}

            def execute(_tool, arguments):
                artifact.write_text('result-v1', encoding='utf-8')
                return {'status': 'ok', 'output_path': arguments['output_path']}

            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.run_tool', side_effect=execute
            ):
                manifest = run_workflow(workflow, output_path=checkpoint)

            reproducibility = manifest['reproducibility']
            trace = manifest['steps'][0]
            self.assertEqual(reproducibility['version'], 1)
            self.assertEqual(reproducibility['recipe']['workflow'], workflow)
            self.assertEqual(reproducibility['seed_status'], 'recorded')
            self.assertEqual(trace['reproducibility']['randomness']['values'], {'random_seed': 42})
            self.assertEqual(trace['reproducibility']['input_files'][0]['path'], str(source.resolve()))
            self.assertEqual(trace['artifacts'][0]['path'], str(artifact.resolve()))
            self.assertTrue(trace['reproducibility']['plugin']['implementation']['source_sha256'])

            verified = verify_manifest(manifest, tool_specs=specs())
            self.assertTrue(verified['reproducible'])
            self.assertEqual(verified['status'], 'verified')

            source.write_text('input-v2', encoding='utf-8')
            drift = verify_manifest(manifest, tool_specs=specs())
            self.assertFalse(drift['reproducible'])
            self.assertIn('input', {issue['category'] for issue in drift['issues']})

    def test_environment_change_invalidates_checkpoint_reuse(self):
        workflow = {'steps': [{'id': 'one', 'tool': 'demo_run', 'args': {}}]}
        with tempfile.TemporaryDirectory(prefix='reproducibility_env_') as raw:
            checkpoint = Path(raw) / 'manifest.json'
            environment_a = {'fingerprint': 'environment-a'}
            environment_b = {'fingerprint': 'environment-b'}
            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.runtime_snapshot', return_value=environment_a
            ), patch('src.workflow_runner.run_tool', return_value={'status': 'ok'}):
                run_workflow(workflow, output_path=checkpoint)
            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.runtime_snapshot', return_value=environment_b
            ), patch('src.workflow_runner.run_tool', return_value={'status': 'ok'}) as execute:
                resumed = run_workflow(workflow, output_path=checkpoint, resume=True)
            execute.assert_called_once()
            self.assertEqual(resumed['invalidated_steps'][0]['id'], 'one')

    def test_plugin_implementation_drift_is_detected(self):
        workflow = {'steps': [{'id': 'one', 'tool': 'demo_run', 'args': {}}]}
        with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
            'src.workflow_runner.run_tool', return_value={'status': 'ok'}
        ):
            manifest = run_workflow(workflow)
        result = verify_manifest(
            manifest,
            check_environment=False,
            tool_specs=specs(changed_demo_function),
        )
        self.assertFalse(result['reproducible'])
        self.assertIn('plugin', {issue['category'] for issue in result['issues']})

    def test_manifest_recipe_and_result_tampering_is_detected(self):
        workflow = {'steps': [{'id': 'one', 'tool': 'demo_run', 'args': {}}]}
        with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
            'src.workflow_runner.run_tool', return_value={'status': 'ok', 'value': 1}
        ):
            manifest = run_workflow(workflow)
        manifest['reproducibility']['recipe']['workflow']['steps'][0]['args']['changed'] = True
        manifest['steps'][0]['result']['value'] = 2
        result = verify_manifest(
            manifest,
            check_environment=False,
            tool_specs=specs(),
        )
        reasons = {issue['reason'] for issue in result['issues']}
        self.assertIn('recipe_fingerprint_changed', reasons)
        self.assertIn('result_digest_changed', reasons)


if __name__ == '__main__':
    unittest.main()
