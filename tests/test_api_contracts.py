import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError

from src.api_contracts import (
    JobCreate,
    a2a_error,
    a2a_message_text,
    a2a_task,
    iter_artifact_values,
    resolve_artifact_path,
)


class ApiContractTests(unittest.TestCase):
    def test_job_request_validation_is_transport_independent(self):
        payload = JobCreate(tool='research_plan', arguments={'task': 'test'})
        self.assertEqual(payload.tool, 'research_plan')
        with self.assertRaises(ValidationError):
            JobCreate(tool='', arguments={})

    def test_artifact_discovery_is_recursive_and_confined_to_output(self):
        with tempfile.TemporaryDirectory(prefix='api_contracts_') as raw:
            project_root = Path(raw)
            output_root = project_root / 'output'
            output_root.mkdir()
            artifact = output_root / 'nested' / 'report.md'
            artifact.parent.mkdir()
            artifact.write_text('ok', encoding='utf-8')
            outside = project_root / 'outside.md'
            outside.write_text('no', encoding='utf-8')

            payload = {
                'result': [
                    {'report_path': str(artifact)},
                    {'ignored_path': str(outside)},
                ]
            }
            self.assertEqual(list(iter_artifact_values(payload)), [str(artifact)])
            self.assertEqual(resolve_artifact_path(artifact, output_root), artifact.resolve())
            self.assertIsNone(resolve_artifact_path(outside, output_root))

    def test_a2a_serializers_keep_protocol_shape(self):
        record = {
            'job_id': 'job-1',
            'tool': 'research_plan',
            'status': 'completed',
            'created_at': '2026-01-01T00:00:00+00:00',
            'result': {'status': 'ok'},
            'trace_id': 'trace-1',
            'request_id': 'request-1',
        }
        task = a2a_task(record, 'context-1')
        self.assertEqual(task['status']['state'], 'completed')
        self.assertEqual(task['artifacts'][0]['parts'][0]['data'], {'status': 'ok'})
        self.assertEqual(task['metadata']['bio.trace_id'], 'trace-1')
        self.assertEqual(task['metadata']['bio.request_id'], 'request-1')
        self.assertEqual(
            a2a_error('request-1', -32600, 'invalid')['error']['code'],
            -32600,
        )
        self.assertEqual(
            a2a_message_text({
                'parts': [
                    {'kind': 'text', 'text': 'first'},
                    {'kind': 'data', 'data': {}},
                    {'kind': 'text', 'text': 'second'},
                ]
            }),
            'first\nsecond',
        )


if __name__ == '__main__':
    unittest.main()
