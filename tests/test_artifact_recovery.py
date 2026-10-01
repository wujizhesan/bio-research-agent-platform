import asyncio
from datetime import datetime, timedelta, timezone
import tempfile
import unittest
from pathlib import Path

from src.artifact_recovery import ArtifactRecoveryService
from src.artifact_store import S3ArtifactStore


class FakeDatabase:
    def __init__(self, execution=None):
        self.execution = execution
        self.committed = []
        self.updates = []

    async def get_execution_result(self, _execution_key):
        return self.execution

    async def finish_job_artifact_recovery(
        self,
        publication_id,
        recovery_token,
        status,
        error=None,
        retention_until=None,
    ):
        self.updates.append({
            'publication_id': publication_id,
            'recovery_token': recovery_token,
            'status': status,
            'error': error,
            'retention_until': retention_until,
        })
        if status == 'committed':
            self.committed.append(publication_id)

    async def finish_file_recovery(
        self,
        file_id,
        recovery_token,
        status,
        error=None,
        retention_until=None,
    ):
        self.updates.append({
            'file_id': file_id,
            'recovery_token': recovery_token,
            'status': status,
            'error': error,
            'retention_until': retention_until,
        })


class FakeS3Client:
    def __init__(self, retain_until=None, delete_error=None, confirm_deletion=True):
        self.retain_until = retain_until
        self.delete_error = delete_error
        self.confirm_deletion = confirm_deletion
        self.deleted = []

    def head_object(self, **request):
        if self.confirm_deletion and request in self.deleted:
            error = FileNotFoundError('object version not found')
            error.response = {
                'Error': {'Code': 'NoSuchVersion'},
                'ResponseMetadata': {'HTTPStatusCode': 404},
            }
            raise error
        response = {'VersionId': 'version-1'}
        if self.retain_until is not None:
            response['ObjectLockRetainUntilDate'] = self.retain_until
        return response

    def delete_object(self, **request):
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(request)


def artifact_record(**changes):
    return {
        'publication_id': 'p' * 64,
        'artifact_id': 'a' * 32,
        'job_id': 'job-1',
        'project_id': 'project-1',
        'execution_key': 'e' * 32,
        'fencing_token': '7',
        'attempt': 1,
        'parameter': 'output_path',
        'kind': 'file',
        'status': 'orphaned',
        'recovery_token': 'recovery-1',
        'recovery_lease_until': '2026-09-21T01:00:00+00:00',
        'revision': 1,
        'storage_backend': 's3',
        'filename': 'result.json',
        'storage_key': 'bio-agent/artifacts/project/job/result.json',
        'version_id': 'version-1',
        'created_at': '2026-09-21T00:00:00+00:00',
        'updated_at': '2026-09-21T00:00:00+00:00',
        **changes,
    }


class ArtifactRecoveryTests(unittest.TestCase):
    def test_durable_execution_commits_instead_of_deleting(self):
        database = FakeDatabase({
            'status': 'completed',
            'result': {
                'schema': 'bioagent.execution-result.v1',
                'result': {'status': 'ok'},
                'artifacts': [{'publication_id': 'p' * 64}],
            },
        })
        client = FakeS3Client()
        service = ArtifactRecoveryService(
            database,
            S3ArtifactStore('bucket', client=client),
        )
        outcome = asyncio.run(service.reconcile(artifact_record()))
        self.assertEqual(outcome, 'committed')
        self.assertEqual(database.committed, ['p' * 64])
        self.assertEqual(client.deleted, [])

    def test_superseded_publication_is_reclaimed(self):
        database = FakeDatabase({
            'status': 'completed',
            'result': {
                'schema': 'bioagent.execution-result.v1',
                'result': {'status': 'ok'},
                'artifacts': [{'publication_id': 'q' * 64}],
            },
        })
        client = FakeS3Client()
        service = ArtifactRecoveryService(
            database,
            S3ArtifactStore('bucket', client=client),
        )
        outcome = asyncio.run(service.reconcile(artifact_record()))
        self.assertEqual(outcome, 'deleted')
        self.assertEqual(database.committed, [])

    def test_s3_recovery_deletes_exact_uncommitted_version(self):
        database = FakeDatabase()
        client = FakeS3Client()
        service = ArtifactRecoveryService(
            database,
            S3ArtifactStore('bucket', client=client),
        )
        outcome = asyncio.run(service.reconcile(artifact_record()))
        self.assertEqual(outcome, 'deleted')
        self.assertEqual(client.deleted[0]['VersionId'], 'version-1')
        self.assertEqual(database.updates[-1]['status'], 'deleted')

    def test_explicit_deletion_bypasses_durable_execution_and_confirms_version(self):
        database = FakeDatabase({
            'status': 'completed',
            'result': {
                'schema': 'bioagent.execution-result.v1',
                'result': {'status': 'ok'},
                'artifacts': [{'publication_id': 'p' * 64}],
            },
        })
        client = FakeS3Client()
        service = ArtifactRecoveryService(
            database,
            S3ArtifactStore('bucket', client=client),
        )
        outcome = asyncio.run(service.reconcile(artifact_record(
            status='deleting',
            delete_request_id='delete-request',
        )))
        self.assertEqual(outcome, 'deleted')
        self.assertEqual(database.committed, [])
        self.assertEqual(database.updates[-1]['status'], 'deleted')

    def test_explicit_deletion_does_not_release_when_object_still_exists(self):
        database = FakeDatabase()
        client = FakeS3Client(confirm_deletion=False)
        service = ArtifactRecoveryService(
            database,
            S3ArtifactStore('bucket', client=client),
        )
        outcome = asyncio.run(service.reconcile(artifact_record(
            status='deleting',
            delete_request_id='delete-request',
        )))
        self.assertEqual(outcome, 'delete_failed')
        self.assertEqual(database.updates[-1]['status'], 'delete_failed')

    def test_object_lock_marks_artifact_retained(self):
        database = FakeDatabase()
        retain_until = datetime.now(timezone.utc) + timedelta(days=7)
        client = FakeS3Client(
            retain_until=retain_until,
            delete_error=PermissionError('object is retained'),
        )
        service = ArtifactRecoveryService(
            database,
            S3ArtifactStore('bucket', client=client),
        )
        outcome = asyncio.run(service.reconcile(artifact_record()))
        self.assertEqual(outcome, 'retained')
        self.assertEqual(database.updates[-1]['status'], 'retained')
        self.assertEqual(
            database.updates[-1]['retention_until'],
            retain_until.isoformat(),
        )

    def test_local_recovery_refuses_path_outside_artifact_root(self):
        with tempfile.TemporaryDirectory(prefix='artifact_gc_') as raw:
            root = Path(raw)
            outside = root / 'outside.txt'
            outside.write_text('keep', encoding='utf-8')
            database = FakeDatabase()
            service = ArtifactRecoveryService(
                database,
                None,
                artifact_root=root / 'allowed',
            )
            outcome = asyncio.run(service.reconcile(artifact_record(
                storage_backend='local',
                path=str(outside),
                storage_key=None,
                version_id=None,
            )))
            self.assertEqual(outcome, 'orphaned')
            self.assertTrue(outside.exists())

    def test_local_recovery_never_deletes_artifact_root(self):
        with tempfile.TemporaryDirectory(prefix='artifact_root_gc_') as raw:
            root = Path(raw) / 'output'
            root.mkdir()
            protected = root / 'keep.txt'
            protected.write_text('keep', encoding='utf-8')
            database = FakeDatabase()
            service = ArtifactRecoveryService(
                database,
                None,
                artifact_root=root,
            )
            outcome = asyncio.run(service.reconcile(artifact_record(
                storage_backend='local',
                path=str(root),
                storage_key=None,
                version_id=None,
                status='deleting',
                delete_request_id='delete-root',
            )))
            self.assertEqual(outcome, 'delete_failed')
            self.assertTrue(protected.exists())
            self.assertIn('strict child', database.updates[-1]['error'])

    def test_local_file_recovery_removes_staging_directory(self):
        with tempfile.TemporaryDirectory(prefix='file_gc_') as raw:
            root = Path(raw) / 'uploads'
            directory = root / ('f' * 32)
            directory.mkdir(parents=True)
            (directory / 'input.csv').write_text('value\n1\n', encoding='utf-8')
            database = FakeDatabase()
            service = ArtifactRecoveryService(
                database,
                None,
                file_root=root,
            )
            outcome = asyncio.run(service.reconcile_file({
                'file_id': 'f' * 32,
                'status': 'reclaiming',
                'storage_backend': 'local',
                'recovery_token': 'file-recovery-1',
            }))
            self.assertEqual(outcome, 'deleted')
            self.assertFalse(directory.exists())
            self.assertEqual(database.updates[-1]['status'], 'deleted')

    def test_s3_file_recovery_deletes_planned_version(self):
        database = FakeDatabase()
        client = FakeS3Client()
        service = ArtifactRecoveryService(
            database,
            S3ArtifactStore('bucket', prefix='bio-agent', client=client),
        )
        outcome = asyncio.run(service.reconcile_file({
            'file_id': 'f' * 32,
            'status': 'reclaiming',
            'storage_backend': 's3',
            'storage_key': 'bio-agent/' + 'f' * 32 + '/input.csv',
            'version_id': None,
            'recovery_token': 'file-recovery-2',
        }))
        self.assertEqual(outcome, 'deleted')
        self.assertEqual(client.deleted[-1]['VersionId'], 'version-1')


if __name__ == '__main__':
    unittest.main()
