import hashlib
import os
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
    DirectoryManifest,
    _ArchiveChecksumWriter,
    _archive_directory,
    _sha256,
    pack_execution_result,
    unpack_execution_result,
)
from src.execution_semantics import ArtifactTransaction, StagedArtifact
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
    def _directory_transaction(self, root, store, required=True, plan=True):
        staged, target = root / 'staged', root / 'dataset'
        staged.mkdir()
        (staged / 'nested').mkdir()
        (staged / 'empty').mkdir()
        (staged / 'a.txt').write_bytes(b'alpha\n')
        (staged / 'nested' / '科研结果.txt').write_bytes(b'beta\n')
        transaction = ArtifactTransaction({}, [
            StagedArtifact(0, 'output_dir', target, staged, True, required),
        ])
        context = {'job_id': 'job-directory', 'execution_key': 'execution-directory'}
        plans = transaction.plan(store, context) if plan else ()
        return transaction, staged, target, context, plans

    def test_manifest_publication_preserves_bytes_metadata_and_reservation(self):
        for backend in ('local', 's3'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as raw:
                root, client = Path(raw), FakeS3Client()
                store = LocalArtifactStore() if backend == 'local' else S3ArtifactStore('results', client=client)
                transaction, staged, target, context, plans = self._directory_transaction(root, store)
                publication = transaction.publish({'output_dir': str(staged)}, store, context)
                record = publication.artifacts[0]
                self.assertEqual(record['reserved_bytes'], plans[0]['reserved_bytes'])
                self.assertEqual(record['artifact_id'], plans[0]['artifact_id'])
                if backend == 'local':
                    digest = hashlib.sha256()
                    for relative, payload in (('a.txt', b'alpha\n'), ('nested/科研结果.txt', b'beta\n')):
                        digest.update(relative.encode())
                        digest.update(b'\0')
                        digest.update(hashlib.sha256(payload).hexdigest().encode())
                        self.assertEqual((target / relative).read_bytes(), payload)
                    self.assertEqual(record['sha256'], digest.hexdigest())
                    self.assertEqual(record['size_bytes'], 11)
                    self.assertEqual(publication.result['output_dir'], str(target))
                else:
                    body = client.objects[('results', record['storage_key'])]['body']
                    expected = root / 'expected.tar.gz'
                    _archive_directory(staged, expected, 'dataset', compresslevel=1)
                    self.assertEqual(body, expected.read_bytes())
                    self.assertEqual(record['sha256'], hashlib.sha256(body).hexdigest())
                    self.assertEqual(record['size_bytes'], len(body))
                    self.assertLessEqual(len(body), record['reserved_bytes'])
                publication.finalize()
                if backend == 's3':
                    self.assertFalse(staged.exists())

    def test_directory_changes_after_planning_are_rejected_and_rolled_back(self):
        for backend in ('local', 's3'):
            for mutation in ('add', 'delete', 'rename', 'rewrite', 'replace', 'type', 'empty_directory', 'root'):
                with self.subTest(backend=backend, mutation=mutation), tempfile.TemporaryDirectory() as raw:
                    root, client = Path(raw), FakeS3Client()
                    store = LocalArtifactStore() if backend == 'local' else S3ArtifactStore('results', client=client)
                    transaction, staged, target, context, _ = self._directory_transaction(root, store)
                    path = staged / 'a.txt'
                    if mutation == 'add':
                        (staged / 'new.txt').write_bytes(b'new')
                    elif mutation == 'delete':
                        path.unlink()
                    elif mutation == 'rename':
                        path.rename(staged / 'renamed.txt')
                    elif mutation == 'rewrite':
                        info = path.stat()
                        path.write_bytes(b'other\n')
                        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
                    elif mutation == 'replace':
                        replacement = staged / 'replacement'
                        replacement.write_bytes(path.read_bytes())
                        info = path.stat()
                        os.utime(replacement, ns=(info.st_atime_ns, info.st_mtime_ns))
                        replacement.replace(path)
                    elif mutation == 'type':
                        path.unlink()
                        path.mkdir()
                    elif mutation == 'empty_directory':
                        (staged / 'empty' / 'new').mkdir()
                    else:
                        staged.rename(root / 'old-stage')
                        staged.mkdir()
                        (staged / 'replacement.txt').write_bytes(b'replaced')
                    with self.assertRaisesRegex(StorageIntegrityError, 'changed after planning'):
                        transaction.publish({'output_dir': str(staged)}, store, context)
                    self.assertFalse(staged.exists())
                    self.assertFalse(target.exists())
                    self.assertEqual(client.objects, {})
                    self.assertEqual(list(root.glob('*.tar.gz')), [])

    def test_directory_links_are_rejected_before_and_after_planning(self):
        for backend in ('local', 's3'):
            for timing in ('before', 'after'):
                for link_root in (False, True):
                    with self.subTest(backend=backend, timing=timing, link_root=link_root), tempfile.TemporaryDirectory() as raw:
                        root, client = Path(raw), FakeS3Client()
                        outside = root / 'outside'
                        outside.mkdir()
                        (outside / 'secret.txt').write_bytes(b'outside')
                        store = LocalArtifactStore() if backend == 'local' else S3ArtifactStore('results', client=client)
                        transaction, staged, target, context, _ = self._directory_transaction(root, store, plan=timing == 'after')
                        link = staged / 'nested' / 'link'
                        if link_root:
                            staged.rename(root / 'old-stage')
                            link = staged
                        try:
                            link.symlink_to(outside, target_is_directory=True)
                        except OSError as exc:
                            self.skipTest(f'symbolic links unavailable: {exc}')
                        with self.assertRaisesRegex(RuntimeError, 'symbolic'):
                            if timing == 'before':
                                transaction.plan(store, context)
                            else:
                                transaction.publish({'output_dir': str(staged)}, store, context)
                        transaction.rollback()
                        self.assertFalse(staged.exists())
                        self.assertFalse(target.exists())
                        self.assertEqual((outside / 'secret.txt').read_bytes(), b'outside')
                        self.assertEqual(client.objects, {})

    def test_local_change_during_hashing_removes_moved_target(self):
        with tempfile.TemporaryDirectory() as raw:
            root, store = Path(raw), LocalArtifactStore()
            transaction, staged, target, context, _ = self._directory_transaction(root, store)

            def mutate(path, expected=None):
                Path(path).write_bytes(b'changed during hashing')
                return _sha256(path, expected)

            with patch('src.artifact_store._sha256', side_effect=mutate):
                with self.assertRaisesRegex(StorageIntegrityError, 'changed during publication'):
                    transaction.publish({'output_dir': str(staged)}, store, context)
            self.assertFalse(staged.exists())
            self.assertFalse(target.exists())

    def test_s3_change_during_archive_read_removes_partial_package(self):
        with tempfile.TemporaryDirectory() as raw:
            root, client = Path(raw), FakeS3Client()
            store = S3ArtifactStore('results', client=client)
            transaction, staged, target, context, _ = self._directory_transaction(root, store)
            addfile = tarfile.TarFile.addfile

            def mutate(archive, info, fileobj=None):
                if fileobj is not None:
                    Path(fileobj.name).write_bytes(b'changed during archive read')
                return addfile(archive, info, fileobj)

            with patch.object(tarfile.TarFile, 'addfile', mutate):
                with self.assertRaisesRegex(StorageIntegrityError, 'changed during publication'):
                    transaction.publish({'output_dir': str(staged)}, store, context)
            self.assertFalse(staged.exists())
            self.assertFalse(target.exists())
            self.assertEqual(list(root.glob('*.tar.gz')), [])
            self.assertEqual(client.objects, {})

    def test_optional_directory_growth_after_empty_plan_is_rejected(self):
        for generated in (False, True):
            with self.subTest(generated=generated), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                staged, target = root / 'staged', root / 'dataset'
                if generated:
                    staged.mkdir()
                transaction = ArtifactTransaction({}, [StagedArtifact(0, 'output_dir', target, staged, True, False)])
                store = LocalArtifactStore()
                self.assertEqual(transaction.plan(store, {}), ())
                staged.mkdir(exist_ok=True)
                (staged / 'unexpected.txt').write_bytes(b'new artifact')
                with self.assertRaisesRegex(StorageIntegrityError, 'changed after planning'):
                    transaction.publish({}, store, {})
                self.assertFalse(staged.exists())
                self.assertFalse(target.exists())

    def test_legacy_store_keeps_existing_plan_and_publish_arguments(self):
        class LegacyStore:
            def plan(self, staged, target, parameter, kind, context, index):
                return LocalArtifactStore().plan(staged, target, parameter, kind, context, index)

            def publish(self, staged, target, parameter, kind, context, index):
                return LocalArtifactStore().publish(staged, target, parameter, kind, context, index)

        class LegacySubclass(LocalArtifactStore):
            def plan(self, staged, target, parameter, kind, context, index):
                return super().plan(staged, target, parameter, kind, context, index)

        for store in (LegacyStore(), LegacySubclass()):
            with self.subTest(store=type(store).__name__), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                transaction, staged, target, context, _ = self._directory_transaction(root, store)
                publication = transaction.publish({}, store, context)
                self.assertEqual((target / 'a.txt').read_bytes(), b'alpha\n')
                self.assertEqual(publication.artifacts[0]['size_bytes'], 11)
                publication.finalize()

    def test_manifest_from_another_directory_is_rejected(self):
        for backend in ('local', 's3'):
            with self.subTest(backend=backend), tempfile.TemporaryDirectory() as raw:
                root, client = Path(raw), FakeS3Client()
                staged, other = root / 'staged', root / 'other'
                staged.mkdir()
                other.mkdir()
                (staged / 'result.txt').write_bytes(b'result')
                manifest = DirectoryManifest.capture(other)
                store = LocalArtifactStore() if backend == 'local' else S3ArtifactStore('results', client=client)
                for operation in (store.plan, store.publish):
                    with self.assertRaisesRegex(StorageIntegrityError, 'does not match directory'):
                        operation(staged, root / 'target', 'output_dir', 'directory', {}, 0, directory_manifest=manifest)
                self.assertEqual((staged / 'result.txt').read_bytes(), b'result')
                self.assertFalse((root / 'target').exists())
                self.assertEqual(client.objects, {})

    def test_replanning_refreshes_manifest_and_allows_read_access(self):
        with tempfile.TemporaryDirectory() as raw:
            root, store = Path(raw), LocalArtifactStore()
            transaction, staged, target, context, first = self._directory_transaction(root, store, plan=False)
            path = staged / 'a.txt'
            os.utime(path, ns=(0, 0))
            first = transaction.plan(store, context)
            path.read_bytes()
            (staged / 'new.txt').write_bytes(b'new')
            refreshed = transaction.plan(store, context)
            path.read_bytes()
            self.assertEqual(refreshed[0]['reserved_bytes'], first[0]['reserved_bytes'] + 3)
            publication = transaction.publish({}, store, context)
            self.assertEqual(publication.artifacts[0]['size_bytes'], 14)
            self.assertEqual((target / 'new.txt').read_bytes(), b'new')
            publication.finalize()

    def test_manifest_archive_preserves_hardlinks(self):
        with tempfile.TemporaryDirectory() as raw:
            root, client = Path(raw), FakeS3Client()
            store = S3ArtifactStore('results', client=client)
            transaction, staged, target, context, _ = self._directory_transaction(root, store, plan=False)
            try:
                os.link(staged / 'a.txt', staged / 'alias.txt')
            except OSError as exc:
                self.skipTest(f'hard links unavailable: {exc}')
            transaction.plan(store, context)
            publication = transaction.publish({}, store, context)
            record = publication.artifacts[0]
            body = client.objects[('results', record['storage_key'])]['body']
            expected = root / 'expected.tar.gz'
            _archive_directory(staged, expected, 'dataset', compresslevel=1)
            self.assertEqual(body, expected.read_bytes())
            with tarfile.open(fileobj=BytesIO(body), mode='r:gz') as archive:
                self.assertTrue(archive.getmember('dataset/alias.txt').islnk())
                self.assertEqual(archive.extractfile('dataset/alias.txt').read(), b'alpha\n')
            publication.finalize()

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

    def test_s3_directory_uses_fast_compression_with_valid_reservation(self):
        with tempfile.TemporaryDirectory(prefix='artifact_fast_gzip_') as raw:
            root = Path(raw)
            source = root / 'source'
            source.mkdir()
            payload = (b'@read\n' + b'ACGT' * 25 + b'\n+\n' + b'I' * 100 + b'\n') * 4096
            (source / 'reads.fastq').write_bytes(payload)
            original = root / 'original.tar.gz'
            archive_with_reread(source, original, 'dataset')
            client = FakeS3Client()
            handle = S3ArtifactStore('research-results', client=client).publish(
                source, root / 'dataset', 'output_dir', 'directory',
                {'job_id': 'job-fast', 'execution_key': 'execution-fast'}, 0,
            )
            body = client.objects[('research-results', handle.record['storage_key'])]['body']
            self.assertNotEqual(body, original.read_bytes())
            self.assertLessEqual(len(body), handle.record['reserved_bytes'])
            self.assertEqual(handle.record['sha256'], hashlib.sha256(body).hexdigest())
            with tarfile.open(fileobj=BytesIO(body), mode='r:gz') as archive:
                self.assertEqual(archive.extractfile('dataset/reads.fastq').read(), payload)
            handle.finalize()
            self.assertFalse(source.exists())

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
