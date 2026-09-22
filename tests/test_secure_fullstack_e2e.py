import json
from pathlib import Path
import tempfile
import unittest

from scripts.verify_secure_fullstack_e2e import MARKER, prepare_fixture, verify


class SecureFullStackE2ETests(unittest.TestCase):
    def test_fixture_cleanup_is_restricted_to_named_root(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            with self.assertRaisesRegex(ValueError, 'secure-fullstack-e2e'):
                prepare_fixture(root / 'other', root)

    def test_verifier_submits_authenticated_container_job(self):
        with tempfile.TemporaryDirectory() as raw:
            artifact_root = Path(raw) / 'output'
            host_root = artifact_root / 'secure-fullstack-e2e'
            artifact_root.mkdir()
            states = {
                'job-1': iter(('running', 'completed')),
                'job-2': iter(('running', 'failed')),
            }
            submissions = []

            def requester(_base_url, path, method='GET', data=None,
                          token=None, form=False):
                if path == '/api/v1/auth/token':
                    self.assertTrue(form)
                    return {'access_token': 'test-access-token'}
                if path == '/health':
                    self.assertIsNone(token)
                    return {'status': 'ok', 'job_backend': 'redis'}
                self.assertEqual(token, 'test-access-token')
                if path == '/api/v1/projects':
                    self.assertEqual(method, 'POST')
                    return {'project': {'project_id': 'project-1'}}
                if path == '/api/v1/jobs':
                    self.assertEqual(method, 'POST')
                    submissions.append(data)
                    job_id = f'job-{len(submissions)}'
                    if job_id == 'job-1':
                        target = host_root / 'artifacts' / 'knowledge-index.json'
                        target.parent.mkdir(parents=True)
                        target.write_text(json.dumps({
                            'documents': [{'text': MARKER}],
                        }), encoding='utf-8')
                    return {'job': {'job_id': job_id, 'status': 'queued'}}
                if path.startswith('/api/v1/jobs/job-'):
                    job_id = path.rsplit('/', 1)[-1]
                    status = next(states[job_id])
                    job = {'job_id': job_id, 'status': status}
                    if status == 'failed':
                        job.update({
                            'error_code': 'tool_execution_failed',
                            'error': 'tool execution failed',
                        })
                    return {'job': job}
                raise AssertionError(path)

            result = verify(
                'http://api.test',
                host_root,
                artifact_root,
                '/app/output/secure-fullstack-e2e',
                'ci-user',
                'ci-password',
                requester=requester,
                sleep_fn=lambda _seconds: None,
            )
            self.assertEqual(result['status'], 'ok')
            self.assertEqual(result['execution_mode'], 'container')
            self.assertEqual(result['failure_job_id'], 'job-2')
            self.assertEqual(len(submissions), 2)
            self.assertEqual(submissions[0]['tool'], 'knowledge_ingest_directory')
            self.assertEqual(submissions[1]['tool'], 'knowledge_search')
            self.assertEqual(submissions[0]['project_id'], 'project-1')
            self.assertEqual(
                submissions[0]['arguments']['input_dir'],
                '/app/output/secure-fullstack-e2e/input',
            )
            self.assertEqual(
                submissions[1]['arguments']['index_path'],
                '/app/output/secure-fullstack-e2e/invalid-index.json',
            )


if __name__ == '__main__':
    unittest.main()
