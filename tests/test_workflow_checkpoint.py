import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.workflow_checkpoint import CheckpointStore, resumable_retry_arguments
from src.workflow_runner import run_workflow


def specs(contract='contract-v1'):
    return [{
        'name': 'demo_run',
        'domain': 'demo',
        'description': 'run',
        'parameters': {
            'type': 'object',
            'properties': {
                'name': {'type': 'string'},
                'input_path': {'type': 'string'},
                'output_path': {'type': 'string'},
            },
            'additionalProperties': False,
        },
        'returns': {},
        'resources': {},
        'plugin_version': '1.0.0',
        'plugin_api_version': 1,
        'plugin_contract_digest': contract,
        'function': lambda: None,
    }]


class WorkflowCheckpointTests(unittest.TestCase):
    def test_checkpoint_lock_rejects_concurrent_writer(self):
        with tempfile.TemporaryDirectory(prefix='workflow_lock_') as raw:
            store = CheckpointStore(Path(raw) / 'manifest.json')
            with store.lock():
                with self.assertRaisesRegex(TimeoutError, 'already in use'):
                    with store.lock(timeout_seconds=0.01):
                        pass

    def test_retry_enables_resume_when_checkpoint_exists(self):
        with tempfile.TemporaryDirectory(prefix='workflow_retry_') as raw:
            checkpoint = Path(raw) / 'manifest.json'
            checkpoint.write_text(
                json.dumps({'checkpoint_version': 1}),
                encoding='utf-8',
            )
            arguments = resumable_retry_arguments(
                {'output_path': str(checkpoint), 'resume': False},
                {'parameters': {'properties': {'output_path': {}, 'resume': {}}}},
            )
        self.assertTrue(arguments['resume'])

    def test_failed_workflow_resumes_from_last_completed_step(self):
        workflow = {
            'name': 'resume-demo',
            'steps': [
                {'id': 'one', 'tool': 'demo_run', 'args': {'name': 'one'}},
                {'id': 'two', 'tool': 'demo_run', 'depends_on': ['one'], 'args': {'name': 'two'}},
                {'id': 'three', 'tool': 'demo_run', 'depends_on': ['two'], 'args': {'name': 'three'}},
            ],
        }
        with tempfile.TemporaryDirectory(prefix='workflow_resume_') as raw:
            checkpoint = Path(raw) / 'manifest.json'
            first_calls = []

            def first_run(_tool, arguments):
                first_calls.append(arguments['name'])
                if arguments['name'] == 'two':
                    return {'status': 'error', 'error': 'interrupted'}
                return {'status': 'ok', 'value': arguments['name']}

            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.run_tool', side_effect=first_run
            ):
                first = run_workflow(workflow, output_path=checkpoint)
            self.assertEqual(first['status'], 'failed')
            self.assertEqual(first_calls, ['one', 'two'])

            resumed_calls = []

            def resumed_run(_tool, arguments):
                resumed_calls.append(arguments['name'])
                return {'status': 'ok', 'value': arguments['name']}

            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.run_tool', side_effect=resumed_run
            ):
                resumed = run_workflow(workflow, output_path=checkpoint, resume=True)
            self.assertEqual(resumed['status'], 'completed')
            self.assertEqual(resumed_calls, ['two', 'three'])
            self.assertTrue(resumed['steps'][0]['reused'])
            self.assertEqual(resumed['steps'][1]['attempt'], 2)
            self.assertEqual(resumed['resumed_steps'], 1)
            self.assertEqual(resumed['executed_steps'], 2)

            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.run_tool'
            ) as run_tool:
                reused = run_workflow(workflow, output_path=checkpoint, resume=True)
            run_tool.assert_not_called()
            self.assertEqual(reused['resumed_steps'], 3)
            self.assertEqual(reused['executed_steps'], 0)

    def test_changed_input_file_invalidates_checkpoint(self):
        workflow = {
            'steps': [{
                'id': 'analyze',
                'tool': 'demo_run',
                'args': {'input_path': ''},
            }],
        }
        with tempfile.TemporaryDirectory(prefix='workflow_input_') as raw:
            root = Path(raw)
            source = root / 'input.txt'
            source.write_text('first', encoding='utf-8')
            workflow['steps'][0]['args']['input_path'] = str(source)
            checkpoint = root / 'manifest.json'
            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.run_tool', return_value={'status': 'ok'}
            ) as run_tool:
                run_workflow(workflow, output_path=checkpoint)
                run_workflow(workflow, output_path=checkpoint, resume=True)
                self.assertEqual(run_tool.call_count, 1)
                source.write_text('changed', encoding='utf-8')
                changed = run_workflow(workflow, output_path=checkpoint, resume=True)
            self.assertEqual(run_tool.call_count, 2)
            self.assertEqual(changed['invalidated_steps'][0]['id'], 'analyze')

    def test_missing_or_changed_artifact_is_recomputed(self):
        with tempfile.TemporaryDirectory(prefix='workflow_artifact_') as raw:
            root = Path(raw)
            artifact = root / 'result.txt'
            checkpoint = root / 'manifest.json'
            workflow = {'steps': [{
                'id': 'produce',
                'tool': 'demo_run',
                'args': {'output_path': str(artifact)},
            }]}
            calls = []

            def produce(_tool, arguments):
                calls.append(1)
                artifact.write_text(f'result-{len(calls)}', encoding='utf-8')
                return {'status': 'ok', 'output_path': arguments['output_path']}

            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.run_tool', side_effect=produce
            ):
                run_workflow(workflow, output_path=checkpoint)
                reused = run_workflow(workflow, output_path=checkpoint, resume=True)
                self.assertEqual(reused['resumed_steps'], 1)
                artifact.unlink()
                rebuilt = run_workflow(workflow, output_path=checkpoint, resume=True)
            self.assertEqual(len(calls), 2)
            self.assertIn('artifacts', rebuilt['invalidated_steps'][0]['reason'])

    def test_plugin_contract_change_invalidates_step(self):
        workflow = {'steps': [{'id': 'one', 'tool': 'demo_run', 'args': {}}]}
        with tempfile.TemporaryDirectory(prefix='workflow_contract_') as raw:
            checkpoint = Path(raw) / 'manifest.json'
            with patch('src.workflow_runner.active_tool_specs', return_value=specs('v1')), patch(
                'src.workflow_runner.run_tool', return_value={'status': 'ok'}
            ) as run_tool:
                run_workflow(workflow, output_path=checkpoint)
            with patch('src.workflow_runner.active_tool_specs', return_value=specs('v2')), patch(
                'src.workflow_runner.run_tool', return_value={'status': 'ok'}
            ) as run_tool:
                manifest = run_workflow(workflow, output_path=checkpoint, resume=True)
            run_tool.assert_called_once()
            self.assertEqual(manifest['invalidated_steps'][0]['id'], 'one')

    def test_running_checkpoint_is_retried_and_writes_atomically(self):
        workflow = {'steps': [{'id': 'one', 'tool': 'demo_run', 'args': {}}]}
        with tempfile.TemporaryDirectory(prefix='workflow_running_') as raw:
            root = Path(raw)
            checkpoint = root / 'manifest.json'
            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.run_tool', return_value={'status': 'ok'}
            ):
                initial = run_workflow(workflow, output_path=checkpoint)
            payload = json.loads(checkpoint.read_text(encoding='utf-8'))
            payload['steps'][0]['status'] = 'running'
            checkpoint.write_text(json.dumps(payload), encoding='utf-8')
            with patch('src.workflow_runner.active_tool_specs', return_value=specs()), patch(
                'src.workflow_runner.run_tool', return_value={'status': 'ok'}
            ) as run_tool:
                resumed = run_workflow(workflow, output_path=checkpoint, resume=True)
            run_tool.assert_called_once()
            self.assertEqual(resumed['steps'][0]['attempt'], initial['steps'][0]['attempt'] + 1)
            self.assertEqual(list(root.glob('manifest.json.*.tmp')), [])


if __name__ == '__main__':
    unittest.main()
