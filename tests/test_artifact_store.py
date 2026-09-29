import hashlib
from io import BytesIO
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.benchmark_artifact_archiving import archive_with_reread

from src.artifact_store import (
    LocalArtifactStore,
    S3ArtifactStore,
    _ArchiveChecksumWriter,
    _archive_directory,
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

    def test_streaming_archive_matches_original_bytes_and_includes_gzip_footer(self):
        with tempfile.TemporaryDirectory(prefix='artifact_archive_') as raw:
            root = Path(raw)
            source = root / 'source'
            source.mkdir()
            (source / 'empty').mkdir()
            (source / 'a.txt').write_bytes(b'alpha\n')
            (source / 'empty.txt').write_bytes(b'')
            nested = source / ('nested-' + 'x' * 120)
            nested.mkdir()
            (nested / '科研结果.txt').write_bytes(bytes(range(256)) * 1024)
            original = root / 'original.tar.gz'
            streamed = root / 'streamed.tar.gz'
            expected = archive_with_reread(source, original, 'dataset')
            actual = _archive_directory(source, streamed, 'dataset')
            body = streamed.read_bytes()
            self.assertEqual(body, original.read_bytes())
            self.assertEqual(actual, expected)
            self.assertEqual(actual, (hashlib.sha256(body).hexdigest(), len(body)))
            self.assertNotEqual(actual[0], hashlib.sha256(body[:-8]).hexdigest())
            with tarfile.open(streamed, 'r:gz') as archive:
                self.assertEqual(archive.extractfile('dataset/a.txt').read(), b'alpha\n')
                self.assertTrue(archive.getmember('dataset/empty').isdir())
                self.assertEqual(archive.getmember('dataset/empty.txt').size, 0)

    def test_s3_directory_publishes_streamed_digest_without_archive_reread(self):
        with tempfile.TemporaryDirectory(prefix='artifact_no_reread_') as raw:
            root = Path(raw)
            source = root / 'source'
            source.mkdir()
            (source / 'report.txt').write_bytes(b'research result\n')
            client = FakeS3Client()
            store = S3ArtifactStore('research-results', client=client)
            with patch('src.artifact_store._sha256', side_effect=AssertionError('unexpected reread')):
                handle = store.publish(
                    source, root / 'dataset', 'output_dir', 'directory',
                    {'job_id': 'job-stream', 'execution_key': 'execution-stream'}, 0,
                )
            body = client.objects[('research-results', handle.record['storage_key'])]['body']
            self.assertEqual(handle.record['sha256'], hashlib.sha256(body).hexdigest())
            self.assertEqual(handle.record['size_bytes'], len(body))
            reference = S3ObjectReference.parse(handle.reference)
            self.assertEqual(reference.sha256, handle.record['sha256'])
            handle.rollback()
            self.assertFalse(source.exists())
            self.assertEqual(list(root.glob('*.tar.gz')), [])
            self.assertEqual(client.deleted[0][2], 'version-1')

    def test_archive_writer_rejects_short_write(self):
        class ShortWriter(BytesIO):
            def write(self, data):
                return super().write(data[:-1])

        writer = _ArchiveChecksumWriter(ShortWriter())
        with self.assertRaisesRegex(StorageIntegrityError, 'incomplete'):
            writer.write(b'archive data')
        self.assertEqual(writer.size_bytes, 0)

    def test_s3_packaging_failures_remove_partial_archive_before_upload(self):
        class FailingWriter(_ArchiveChecksumWriter):
            def write(self, data):
                if self.size_bytes >= 10 and data:
                    raise OSError('archive disk full')
                return super().write(data)

        for failure in ('write', 'source'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / 'source'
                source.mkdir()
                (source / 'report.txt').write_bytes(b'research result\n')
                client = FakeS3Client()
                store = S3ArtifactStore('research-results', client=client)
                failing = (
                    patch('src.artifact_store._ArchiveChecksumWriter', FailingWriter)
                    if failure == 'write' else
                    patch('tarfile.TarFile.addfile', side_effect=OSError('source read failed'))
                )
                with failing, self.assertRaises(OSError):
                    store.publish(
                        source, root / 'dataset', 'output_dir', 'directory',
                        {'job_id': 'job-fail', 'execution_key': 'execution-fail'}, 0,
                    )
                self.assertEqual(client.objects, {})
                self.assertEqual(list(root.glob('*.tar.gz')), [])
                self.assertTrue((source / 'report.txt').exists())

    def test_failed_s3_upload_or_head_verification_removes_packaged_directory(self):
        for failure in ('upload', 'checksum', 'size', 'version'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / 'source'
                source.mkdir()
                (source / 'report.txt').write_bytes(b'research result\n')
                client = FakeS3Client()
                if failure == 'upload':
                    def failed_upload(*_args, **_kwargs):
                        raise OSError('upload failed')

                    client.upload_file = failed_upload
                else:
                    original_head = client.head_object

                    def failed_head(**kwargs):
                        head = original_head(**kwargs)
                        if failure == 'checksum':
                            head['Metadata'] = {'sha256': '0' * 64}
                        elif failure == 'size':
                            head['ContentLength'] += 1
                        else:
                            head['VersionId'] = 'null'
                        return head

                    client.head_object = failed_head
                with self.assertRaises((OSError, StorageIntegrityError)):
                    S3ArtifactStore('research-results', client=client).publish(
                        source, root / 'dataset', 'output_dir', 'directory',
                        {'job_id': 'job-head', 'execution_key': 'execution-head'}, 0,
                    )
                self.assertEqual(list(root.glob('*.tar.gz')), [])
                self.assertTrue(source.exists())

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
