import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts.verify_plugin_sandbox_e2e import MARKER, prepare_fixture, verify


class FakeExecutor:
    def __init__(self):
        self.stopped = False

    def execute(self, tool, arguments):
        self.tool = tool
        source = Path(arguments['input_dir']) / 'evidence.md'
        target = Path(arguments['output_path'])
        target.parent.mkdir(parents=True)
        target.write_text(json.dumps({
            'documents': [{
                'id': source.name,
                'text': source.read_text(encoding='utf-8'),
            }],
        }), encoding='utf-8')
        return {
            'status': 'ok',
            'result': {
                'n_documents': 1,
                'output_path': str(target),
            },
        }

    def shutdown(self):
        self.stopped = True


class PluginSandboxE2ETests(unittest.TestCase):
    def test_fixture_cleanup_is_restricted_to_named_test_root(self):
        with tempfile.TemporaryDirectory() as raw:
            artifact_root = Path(raw)
            with self.assertRaisesRegex(ValueError, 'plugin-sandbox-e2e'):
                prepare_fixture(artifact_root / 'other', artifact_root)

    def test_verifier_checks_artifact_content_and_workspace_cleanup(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            artifact_root = root / 'artifacts'
            workspace_root = root / 'exchange'
            artifact_root.mkdir()
            executor = FakeExecutor()
            with patch.dict(os.environ, {
                'PLUGIN_ARTIFACT_ROOT': str(artifact_root),
            }, clear=False):
                result = verify(
                    artifact_root / 'plugin-sandbox-e2e',
                    workspace_root,
                    executor_factory=lambda: executor,
                )
            self.assertEqual(result['status'], 'ok')
            self.assertTrue(result['workspace_clean'])
            self.assertTrue(executor.stopped)
            self.assertEqual(executor.tool, 'knowledge_ingest_directory')
            self.assertIn(
                MARKER,
                Path(result['artifact']).read_text(encoding='utf-8'),
            )


if __name__ == '__main__':
    unittest.main()
