import hashlib
import tempfile
import unittest
from pathlib import Path

from src.artifact_store import (
    LocalArtifactStore,
    S3ArtifactStore,
    pack_execution_result,
    unpack_execution_result,
)
from src.storage_workspace import S3ObjectReference, StorageIntegrityError


class FakeS3Client:
    def __init__(self, version_id='version-1'):
        self.version_id = version_id
        self.objects = {}
        self.deleted = []

    def upload_file(self, filename, bucket, key, ExtraArgs=None):
        self.objects[(bucket, key)] = {
            'body': Path(filename).read_bytes(),
            'metadata': dict((ExtraArgs or {}).get('Metadata') or {}),
            'content_type': (ExtraArgs or {}).get('ContentType'),
        }

    def head_object(self, Bucket, Key, **_kwargs):
        item = self.objects[(Bucket, Key)]
        return {
            'VersionId': self.version_id,
            'ContentLength': len(item['body']),
            'Metadata': item['metadata'],
        }

    def delete_object(self, Bucket, Key, VersionId, **_kwargs):
        self.deleted.append((Bucket, Key, VersionId))
        self.objects.pop((Bucket, Key), None)


class ArtifactStoreTests(unittest.TestCase):
    def test_local_store_publishes_integrity_manifest(self):
        with tempfile.TemporaryDirectory(prefix='artifact_local_') as raw:
            root = Path(raw)
            staged = root / '.report.staging'
            target = root / 'report.txt'
            staged.write_bytes(b'reproducible result\n')
            handle = LocalArtifactStore().publish(
                staged,
                target,
                'output_path',
                'file',
                {'job_id': 'job-1', 'execution_key': 'execution-1'},
                0,
            )
            self.assertFalse(staged.exists())
            self.assertEqual(target.read_bytes(), b'reproducible result\n')
            self.assertEqual(
                handle.record['sha256'],
                hashlib.sha256(target.read_bytes()).hexdigest(),
            )
            self.assertEqual(handle.record['size_bytes'], target.stat().st_size)
            self.assertEqual(
                handle.record['reserved_bytes'],
                target.stat().st_size,
            )
            handle.rollback()
            self.assertFalse(target.exists())

    def test_s3_store_requires_versioning_and_verifies_metadata(self):
        with tempfile.TemporaryDirectory(prefix='artifact_s3_') as raw:
            staged = Path(raw) / '.result.staging'
            staged.write_bytes(b'published result')
            client = FakeS3Client()
            handle = S3ArtifactStore(
                'research-results',
                prefix='platform',
                client=client,
            ).publish(
                staged,
                Path(raw) / 'result.json',
                'output_path',
                'file',
                {
                    'job_id': 'job-2',
                    'project_id': 'project-1',
                    'execution_key': 'execution-2',
                },
                0,
            )
            reference = S3ObjectReference.parse(handle.reference)
            self.assertEqual(reference.version_id, 'version-1')
            self.assertEqual(reference.sha256, handle.record['sha256'])
            self.assertEqual(
                handle.record['reserved_bytes'],
                len(b'published result'),
            )
            self.assertTrue(staged.exists())
            handle.finalize()
            self.assertFalse(staged.exists())
            self.assertTrue(client.objects)

    def test_s3_directory_archives_are_deterministic(self):
        with tempfile.TemporaryDirectory(prefix='artifact_directory_') as raw:
            root = Path(raw)
            client = FakeS3Client()
            store = S3ArtifactStore('research-results', client=client)
            bodies = []
            for index in range(2):
                staged = root / f'stage-{index}' / '.dataset.staging'
                staged.mkdir(parents=True)
                (staged / 'b.txt').write_text('beta\n', encoding='utf-8')
                (staged / 'a.txt').write_text('alpha\n', encoding='utf-8')
                handle = store.publish(
                    staged,
                    root / f'target-{index}' / 'dataset',
                    'output_dir',
                    'directory',
                    {
                        'job_id': f'job-{index}',
                        'project_id': 'project-1',
                        'execution_key': f'execution-{index}',
                    },
                    0,
                )
                bodies.append(client.objects[('research-results', handle.record['storage_key'])]['body'])
                handle.finalize()
            self.assertEqual(bodies[0], bodies[1])

    def test_s3_store_rejects_unversioned_bucket(self):
        with tempfile.TemporaryDirectory(prefix='artifact_unversioned_') as raw:
            staged = Path(raw) / '.result.staging'
            staged.write_bytes(b'unsafe')
            with self.assertRaisesRegex(StorageIntegrityError, 'versioning'):
                S3ArtifactStore(
                    'research-results',
                    client=FakeS3Client(version_id='null'),
                ).publish(
                    staged,
                    Path(raw) / 'result.txt',
                    'output_path',
                    'file',
                    {'job_id': 'job-3', 'execution_key': 'execution-3'},
                    0,
                )
            self.assertTrue(staged.exists())

    def test_execution_result_bundle_round_trip(self):
        artifact = {'artifact_id': 'a' * 32, 'sha256': 'b' * 64}
        packed = pack_execution_result({'status': 'ok'}, [artifact])
        result, artifacts = unpack_execution_result(packed)
        self.assertEqual(result, {'status': 'ok'})
        self.assertEqual(artifacts, [artifact])
        self.assertEqual(unpack_execution_result({'status': 'ok'}), ({'status': 'ok'}, []))


if __name__ == '__main__':
    unittest.main()
