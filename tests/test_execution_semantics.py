import tempfile
from pathlib import Path
import unittest

from src.domain_registry import tool_specs
from src.execution_semantics import (
    ArtifactTransaction,
    normalize_execution_semantics,
    validate_required_artifacts,
)


class ExecutionSemanticsTests(unittest.TestCase):
    def test_output_tools_default_to_side_effecting(self):
        specs = {spec['name']: spec for spec in tool_specs()}
        self.assertEqual(specs['research_catalog']['execution_semantics'], 'pure')
        self.assertEqual(
            specs['research_execute']['execution_semantics'],
            'side_effecting',
        )
        self.assertEqual(specs['cadd_run_screening']['execution_semantics'], 'side_effecting')
        self.assertEqual(specs['cadd_run_screening']['artifacts'], [{
            'argument': 'out',
            'kind': 'directory',
            'publish': 'atomic',
            'overwrite': 'deny',
            'required': True,
        }])

    def test_invalid_semantics_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'execution_semantics'):
            normalize_execution_semantics('at-least-once')

    def test_pure_tool_cannot_declare_artifacts(self):
        with self.assertRaisesRegex(ValueError, 'pure tools'):
            normalize_execution_semantics(
                'pure',
                {'type': 'object', 'properties': {'out': {'type': 'string'}}},
                artifacts=[{'argument': 'out', 'kind': 'directory'}],
            )

    def test_required_artifact_argument_cannot_fall_back_to_unstaged_output(self):
        spec = {
            'parameters': {
                'type': 'object',
                'properties': {'out': {'type': 'string'}},
                'required': ['out'],
            },
            'artifacts': [{
                'argument': 'out',
                'kind': 'directory',
                'required': True,
            }],
        }
        with self.assertRaisesRegex(ValueError, 'artifact argument is required'):
            ArtifactTransaction.prepare({}, spec, 'execution')
        with self.assertRaisesRegex(ValueError, 'artifact argument is required'):
            validate_required_artifacts({}, spec)

    def test_file_artifact_is_staged_and_published(self):
        with tempfile.TemporaryDirectory(prefix='artifact_transaction_') as raw:
            target = Path(raw) / 'report.json'
            spec = {
                'parameters': {
                    'type': 'object',
                    'properties': {'output_path': {'type': 'string'}},
                },
            }
            transaction = ArtifactTransaction.prepare(
                {'output_path': str(target)},
                spec,
                'execution-key',
            )
            staged = Path(transaction.arguments['output_path'])
            self.assertNotEqual(staged, target)
            staged.write_text('{"status":"ok"}', encoding='utf-8')
            result = transaction.commit({'manifest_path': str(staged)})
            self.assertEqual(result['manifest_path'], str(target.resolve()))
            self.assertEqual(target.read_text(encoding='utf-8'), '{"status":"ok"}')
            self.assertFalse(staged.exists())

    def test_failed_directory_artifact_is_rolled_back(self):
        with tempfile.TemporaryDirectory(prefix='artifact_rollback_') as raw:
            target = Path(raw) / 'run'
            spec = {
                'parameters': {
                    'type': 'object',
                    'properties': {'output_dir': {'type': 'string'}},
                },
            }
            transaction = ArtifactTransaction.prepare(
                {'output_dir': str(target)},
                spec,
                'execution-key',
            )
            staged = Path(transaction.arguments['output_dir'])
            (staged / 'partial.txt').write_text('partial', encoding='utf-8')
            transaction.rollback()
            self.assertFalse(staged.exists())
            self.assertFalse(target.exists())

    def test_required_artifact_must_be_generated_before_commit(self):
        with tempfile.TemporaryDirectory(prefix='artifact_required_') as raw:
            target = Path(raw) / 'result.json'
            spec = {
                'parameters': {
                    'type': 'object',
                    'properties': {'output_path': {'type': 'string'}},
                    'required': ['output_path'],
                },
                'artifacts': [{
                    'argument': 'output_path',
                    'kind': 'file',
                    'required': True,
                }],
            }
            transaction = ArtifactTransaction.prepare(
                {'output_path': str(target)},
                spec,
                'execution-key',
            )
            with self.assertRaisesRegex(RuntimeError, 'was not generated'):
                transaction.commit({'status': 'ok'})

    def test_required_directory_cannot_be_empty(self):
        with tempfile.TemporaryDirectory(prefix='artifact_empty_dir_') as raw:
            target = Path(raw) / 'dataset'
            spec = {
                'parameters': {
                    'type': 'object',
                    'properties': {'output_dir': {'type': 'string'}},
                    'required': ['output_dir'],
                },
                'artifacts': [{
                    'argument': 'output_dir',
                    'kind': 'directory',
                    'required': True,
                }],
            }
            transaction = ArtifactTransaction.prepare(
                {'output_dir': str(target)},
                spec,
                'execution-key',
            )
            with self.assertRaisesRegex(RuntimeError, 'directory is empty'):
                transaction.commit({'status': 'ok'})

    def test_artifact_kind_must_match_contract(self):
        with tempfile.TemporaryDirectory(prefix='artifact_kind_') as raw:
            target = Path(raw) / 'result.json'
            spec = {
                'parameters': {
                    'type': 'object',
                    'properties': {'output_path': {'type': 'string'}},
                },
                'artifacts': [{
                    'argument': 'output_path',
                    'kind': 'file',
                }],
            }
            transaction = ArtifactTransaction.prepare(
                {'output_path': str(target)},
                spec,
                'execution-key',
            )
            staged = Path(transaction.arguments['output_path'])
            staged.mkdir()
            with self.assertRaisesRegex(RuntimeError, 'must be a file'):
                transaction.commit({'status': 'ok'})


if __name__ == '__main__':
    unittest.main()
