import asyncio
import gzip
import hashlib
import json
import random
import sys
import tempfile
import threading
import types
import unittest
from contextvars import ContextVar
from pathlib import Path
from unittest import mock

from src.file_security import (
    ClamAVScanner,
    ContentDisarmReconstructor,
    FileSecurityError,
    FileSecurityPipeline,
    FileSecurityResult,
    build_file_security_pipeline_from_env,
)
from src.file_storage import LocalFileStorage, METADATA_RESERVE_BYTES, S3FileStorage, SNIFF_BYTES
from src.storage_workspace import StorageIntegrityError, materialize_storage_references


def gzip_cdr_growth_content():
    rng = random.Random(13579)
    content = b'##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n'
    return content + b''.join(
        f'1\t{index + 1}\t.\tA\tC\t30\tPASS\tTAG={rng.randbytes(64).hex()}\n'.encode()
        for index in range(1000)
    )


class Upload:
    filename = 'reads.fastq'
    content_type = 'text/plain'

    def __init__(self, content, filename=None):
        self.content = content
        if filename is not None:
            self.filename = filename

    async def read(self, size):
        chunk, self.content = self.content[:size], self.content[size:]
        return chunk


class FakeS3Client:
    def __init__(self):
        self.objects = {}
        self.uploads = []
        self.deletions = []
        self.bucket_owners = []

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.objects[(bucket, key)] = {
            'content': Path(filename).read_bytes(),
            'content_type': ExtraArgs['ContentType'],
            'metadata': ExtraArgs['Metadata'],
            'version_id': 'version-1',
        }
        self.uploads.append((bucket, key, ExtraArgs))

    def list_objects_v2(self, Bucket, Prefix, ExpectedBucketOwner=None):
        return {
            'Contents': [
                {'Key': key}
                for bucket, key in self.objects
                if bucket == Bucket and key.startswith(Prefix)
            ]
        }

    def head_object(self, Bucket, Key, VersionId=None, ExpectedBucketOwner=None):
        item = self.objects[(Bucket, Key)]
        if VersionId is not None and VersionId != item['version_id']:
            raise KeyError(VersionId)
        return {
            'ContentType': item['content_type'],
            'ContentLength': len(item['content']),
            'Metadata': item['metadata'],
            'VersionId': item['version_id'],
        }

    def download_file(self, bucket, key, filename, ExtraArgs=None):
        item = self.objects[(bucket, key)]
        if ExtraArgs and ExtraArgs.get('VersionId') != item['version_id']:
            raise KeyError(ExtraArgs['VersionId'])
        Path(filename).write_bytes(item['content'])

    def download_fileobj(self, bucket, key, fileobj, ExtraArgs=None):
        item = self.objects[(bucket, key)]
        if ExtraArgs and ExtraArgs.get('VersionId') != item['version_id']:
            raise KeyError(ExtraArgs['VersionId'])
        for offset in range(0, len(item['content']), 3):
            fileobj.write(item['content'][offset:offset + 3])

    def delete_object(self, Bucket, Key, VersionId=None, ExpectedBucketOwner=None):
        item = self.objects[(Bucket, Key)]
        if VersionId is not None and VersionId != item['version_id']:
            raise KeyError(VersionId)
        self.deletions.append((Bucket, Key, VersionId, ExpectedBucketOwner))
        del self.objects[(Bucket, Key)]
        return {'DeleteMarker': True}

    def head_bucket(self, Bucket, ExpectedBucketOwner=None):
        self.bucket_owners.append(ExpectedBucketOwner)
        return {'Bucket': Bucket}


class S3FileStorageTests(unittest.TestCase):
    def test_gzip_cdr_publishes_rebuilt_checksum_and_size(self):
        class Scanner:
            def scan(self, _path):
                return 'clean'

        content = gzip_cdr_growth_content()
        original = gzip.compress(content, compresslevel=9, mtime=0)
        client = FakeS3Client()
        fake_boto3 = types.ModuleType('boto3')
        fake_boto3.client = lambda *_args, **_kwargs: client
        with tempfile.TemporaryDirectory(prefix='s3_gzip_cdr_') as raw:
            with mock.patch.dict(sys.modules, {'boto3': fake_boto3}):
                storage = S3FileStorage(
                    Path(raw) / 'uploads', bucket='bio-test',
                    security_pipeline=FileSecurityPipeline(
                        clamav=Scanner(), cdr=ContentDisarmReconstructor(), required=True,
                    ),
                )
                stored = asyncio.run(storage.save(Upload(original, 'variants.vcf.gz')))
                remote = client.objects[('bio-test', stored.storage_key)]
                rebuilt = remote['content']
                self.assertNotEqual(rebuilt, original)
                self.assertEqual(gzip.decompress(rebuilt), content)
                self.assertEqual(stored.size_bytes, len(rebuilt))
                self.assertEqual(stored.sha256, hashlib.sha256(rebuilt).hexdigest())
                self.assertEqual(remote['metadata']['sha256'], stored.sha256)
                self.assertEqual(remote['metadata']['security-status'], 'clean')
                metadata = json.loads((stored.path.parent / 'metadata.json').read_text())
                self.assertEqual(metadata['sha256'], stored.sha256)
                self.assertEqual(metadata['size_bytes'], stored.size_bytes)
                self.assertEqual(metadata['version_id'], stored.version_id)
                self.assertFalse(stored.path.exists())

    def test_discard_removes_exact_uploaded_version_and_local_metadata(self):
        client = FakeS3Client()
        fake_boto3 = types.ModuleType('boto3')
        fake_boto3.client = lambda *_args, **_kwargs: client
        with tempfile.TemporaryDirectory(prefix='s3_discard_') as raw:
            with mock.patch.dict(sys.modules, {'boto3': fake_boto3}):
                storage = S3FileStorage(
                    Path(raw) / 'uploads',
                    bucket='bio-test',
                    prefix='research',
                    expected_bucket_owner='123456789012',
                )
                stored = asyncio.run(storage.save(Upload(b'@read1\nACGT\n')))
                asyncio.run(storage.discard(stored))
                self.assertNotIn(('bio-test', stored.storage_key), client.objects)
                self.assertEqual(client.deletions, [(
                    'bio-test',
                    stored.storage_key,
                    'version-1',
                    '123456789012',
                )])
                self.assertFalse((storage.root / stored.file_id).exists())

    def test_upload_and_cache_miss_download(self):
        client = FakeS3Client()
        fake_boto3 = types.ModuleType('boto3')
        fake_boto3.client = lambda *_args, **_kwargs: client
        with tempfile.TemporaryDirectory(prefix='s3_storage_') as raw:
            with mock.patch.dict(sys.modules, {'boto3': fake_boto3}):
                storage = S3FileStorage(Path(raw) / 'uploads', bucket='bio-test', prefix='research')
                stored = asyncio.run(storage.save(Upload(b'@read1\nACGT\n')))
                self.assertEqual(stored.storage_key, f'research/{stored.file_id}/reads.fastq')
                self.assertEqual(stored.version_id, 'version-1')
                self.assertEqual(client.uploads[0][0:2], ('bio-test', stored.storage_key))
                self.assertFalse(stored.path.exists())
                payload = storage.payload(stored, raw, f'/api/v1/files/{stored.file_id}')
                self.assertTrue(payload['path'].startswith('bio+s3://bio-test/'))
                self.assertIn('storage_reference=', payload['download_url'])
                stored.path.parent.joinpath('metadata.json').unlink()
                restored = asyncio.run(storage.aget(
                    stored.file_id,
                    reference=payload['path'],
                ))
                self.assertEqual(restored.storage_key, stored.storage_key)
                self.assertEqual(restored.path.read_bytes(), b'@read1\nACGT\n')
                storage.release(restored)
                self.assertFalse(restored.path.exists())
                self.assertIsNone(storage.ping())

    def test_materialization_is_version_locked_and_reuses_duplicate_reference(self):
        client = FakeS3Client()
        fake_boto3 = types.ModuleType('boto3')
        fake_boto3.client = lambda *_args, **_kwargs: client
        with tempfile.TemporaryDirectory(prefix='s3_materialize_') as raw:
            with mock.patch.dict(sys.modules, {'boto3': fake_boto3}):
                storage = S3FileStorage(Path(raw) / 'uploads', bucket='bio-test', prefix='research')
                stored = asyncio.run(storage.save(Upload(b'@read1\nACGT\n')))
                reference = storage.payload(stored, raw, '/download')['path']
                resolved = materialize_storage_references(
                    {'first': reference, 'nested': [reference]},
                    Path(raw) / 'job',
                    client=client,
                    configured_bucket='bio-test',
                    configured_prefix='research',
                )
                self.assertEqual(resolved['first'], resolved['nested'][0])
                self.assertEqual(Path(resolved['first']).read_bytes(), b'@read1\nACGT\n')

    def test_download_rejects_tampered_object_content(self):
        client = FakeS3Client()
        fake_boto3 = types.ModuleType('boto3')
        fake_boto3.client = lambda *_args, **_kwargs: client
        with tempfile.TemporaryDirectory(prefix='s3_integrity_') as raw:
            with mock.patch.dict(sys.modules, {'boto3': fake_boto3}):
                storage = S3FileStorage(Path(raw) / 'uploads', bucket='bio-test', prefix='research')
                stored = asyncio.run(storage.save(Upload(b'@read1\nACGT\n')))
                client.objects[('bio-test', stored.storage_key)]['content'] = b'@read1\nTGCA\n'
                with self.assertRaisesRegex(StorageIntegrityError, 'checksum'):
                    storage.get(stored.file_id)
                downloads = storage.root / '.downloads'
                self.assertFalse(downloads.exists() and any(downloads.iterdir()))

    def test_ping_uses_expected_bucket_owner(self):
        client = FakeS3Client()
        fake_boto3 = types.ModuleType('boto3')
        fake_boto3.client = lambda *_args, **_kwargs: client
        with tempfile.TemporaryDirectory(prefix='s3_storage_') as raw:
            with mock.patch.dict(sys.modules, {'boto3': fake_boto3}):
                storage = S3FileStorage(
                    Path(raw) / 'uploads',
                    bucket='bio-test',
                    expected_bucket_owner='123456789012',
                )
                storage.ping()
        self.assertEqual(client.bucket_owners, ['123456789012'])


class LocalFileStorageSecurityTests(unittest.TestCase):
    def test_gzip_cdr_commits_rebuilt_size_and_checksum(self):
        class Scanner:
            def __init__(self):
                self.samples = []

            def scan(self, path):
                self.samples.append(Path(path).read_bytes())
                return 'clean'

        content = gzip_cdr_growth_content()
        original = gzip.compress(content, compresslevel=9, mtime=0)
        scanner = Scanner()
        with tempfile.TemporaryDirectory(prefix='gzip_cdr_commit_') as raw:
            storage = LocalFileStorage(raw, security_pipeline=FileSecurityPipeline(
                clamav=scanner, cdr=ContentDisarmReconstructor(), required=True,
            ))
            stored = asyncio.run(storage.save(Upload(original, 'variants.vcf.gz')))
            rebuilt = stored.path.read_bytes()
            self.assertNotEqual(rebuilt, original)
            self.assertEqual(gzip.decompress(rebuilt), content)
            self.assertEqual(stored.content_type, 'application/gzip')
            self.assertEqual(stored.size_bytes, len(rebuilt))
            self.assertEqual(stored.sha256, hashlib.sha256(rebuilt).hexdigest())
            metadata = json.loads((stored.path.parent / 'metadata.json').read_text())
            self.assertEqual(metadata['sha256'], stored.sha256)
            self.assertEqual(metadata['size_bytes'], stored.size_bytes)
            self.assertEqual(storage.get(stored.file_id), stored)
            self.assertEqual(scanner.samples, [original, rebuilt])
            self.assertEqual(stored.security['scan_count'], 2)
            self.assertEqual(storage._reserved_bytes, 0)

    def test_gzip_cdr_growth_rechecks_maximum_and_quota(self):
        class Scanner:
            def scan(self, _path):
                return 'clean'

        content = gzip_cdr_growth_content()
        original = gzip.compress(content, compresslevel=9, mtime=0)
        rebuilt = gzip.compress(content, compresslevel=6, mtime=0)
        self.assertGreater(len(rebuilt), len(original))
        for maximum, quota, message in (
            (len(original), len(rebuilt) + 4096, 'CDR output exceeds maximum upload size'),
            (len(rebuilt), len(original) + METADATA_RESERVE_BYTES, 'CDR output exceeds upload storage quota'),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory(prefix='gzip_cdr_growth_') as raw:
                storage = LocalFileStorage(
                    raw, max_bytes=maximum, total_quota_bytes=quota,
                    security_pipeline=FileSecurityPipeline(
                        clamav=Scanner(), cdr=ContentDisarmReconstructor(), required=True,
                    ),
                )
                with self.assertRaisesRegex(ValueError, message):
                    asyncio.run(storage.save(Upload(original, 'variants.vcf.gz')))
                self.assertEqual(list(storage.root.iterdir()), [])
                self.assertEqual(storage._reserved_bytes, 0)
                self.assertEqual(storage._upload_reservations, {})
                retry = asyncio.run(storage.save(Upload(b'retry', 'retry.txt')))
                self.assertEqual(retry.path.read_bytes(), b'retry')

    def test_clamav_and_cdr_run_before_file_is_committed(self):
        class Scanner:
            def __init__(self):
                self.samples = []

            def scan(self, path):
                self.samples.append(Path(path).read_bytes())
                return 'clean'

        scanner = Scanner()
        pipeline = FileSecurityPipeline(
            clamav=scanner,
            cdr=ContentDisarmReconstructor(),
            required=True,
        )
        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(
                Path(raw) / 'uploads', security_pipeline=pipeline
            )
            stored = asyncio.run(storage.save(Upload(
                b'<h1>Result</h1><script>steal()</script>',
                'report.html',
            )))
            self.assertEqual(stored.path.read_text(encoding='utf-8'), 'Result\n')
            self.assertEqual(stored.security, {
                'status': 'clean',
                'clamav': 'clean',
                'cdr': 'reconstructed',
                'scan_count': 2,
            })
            self.assertIn(b'<script>', scanner.samples[0])
            self.assertNotIn(b'<script>', scanner.samples[1])

    def test_scan_failure_removes_quarantined_upload(self):
        class Scanner:
            def scan(self, _path):
                raise FileSecurityError('ClamAV detected malware: Test.Signature')

        pipeline = FileSecurityPipeline(
            clamav=Scanner(),
            cdr=ContentDisarmReconstructor(),
            required=True,
        )
        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(
                Path(raw) / 'uploads', security_pipeline=pipeline
            )
            with self.assertRaisesRegex(FileSecurityError, 'Test.Signature'):
                asyncio.run(storage.save(Upload(b'content', 'sample.txt')))
            self.assertEqual(list(storage.root.iterdir()), [])

    def test_required_security_configuration_fails_closed(self):
        with mock.patch.dict('os.environ', {
            'FILE_SECURITY_MODE': 'required',
            'FILE_CDR_MODE': 'normalize',
            'CLAMAV_HOST': '',
        }, clear=False):
            with self.assertRaisesRegex(ValueError, 'both ClamAV and CDR'):
                build_file_security_pipeline_from_env()

    def test_clamav_client_uses_framed_instream_protocol(self):
        class Socket:
            def __init__(self):
                self.sent = []
                self.replies = [b'stream: OK\0']

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def settimeout(self, _timeout):
                return None

            def sendall(self, data):
                self.sent.append(data)

            def recv(self, _size):
                return self.replies.pop(0) if self.replies else b''

        client = Socket()
        with tempfile.TemporaryDirectory() as raw:
            path = Path(raw) / 'sample.txt'
            path.write_bytes(b'clean')
            with mock.patch(
                'src.file_security.socket.create_connection',
                return_value=client,
            ):
                result = ClamAVScanner('clamav').scan(path)
        self.assertEqual(result, 'clean')
        self.assertEqual(client.sent[0], b'zINSTREAM\0')
        self.assertEqual(client.sent[-1], b'\x00\x00\x00\x00')

    def test_rejects_archive_and_executable_content_with_safe_extensions(self):
        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(Path(raw) / 'uploads')
            for filename, content in (
                ('archive.csv', b'PK\x03\x04payload'),
                ('binary.txt', b'MZpayload'),
                ('malware.txt', b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR'),
            ):
                with self.subTest(filename=filename):
                    with self.assertRaises(ValueError):
                        asyncio.run(storage.save(Upload(content, filename)))
            self.assertEqual(list(storage.root.iterdir()), [])

    def test_enforces_total_disk_quota(self):
        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(
                Path(raw) / 'uploads',
                max_bytes=20,
                total_quota_bytes=1200,
            )
            asyncio.run(storage.save(Upload(b'123456', 'first.txt')))
            with self.assertRaisesRegex(ValueError, 'quota'):
                asyncio.run(storage.save(Upload(b'123456', 'second.txt')))
            self.assertEqual(len(list(storage.root.iterdir())), 1)

    def test_validates_gzip_content_and_expansion_ratio(self):
        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(
                Path(raw) / 'uploads',
                max_compression_ratio=5,
            )
            valid = gzip.compress(b'##fileformat=VCFv4.3\n#CHROM\tPOS\n')
            stored = asyncio.run(storage.save(
                Upload(valid, 'variants.vcf.gz')
            ))
            self.assertEqual(stored.content_type, 'application/gzip')
            bomb = gzip.compress(b'A' * 10000)
            with self.assertRaisesRegex(ValueError, 'compression ratio'):
                asyncio.run(storage.save(Upload(bomb, 'bomb.vcf.gz')))


class UploadTextSamplingTests(unittest.TestCase):
    def _storage(self, root):
        class Scanner:
            def __init__(self):
                self.samples = []

            def scan(self, path):
                self.samples.append(Path(path).read_bytes())
                return 'clean'

        return LocalFileStorage(root, security_pipeline=FileSecurityPipeline(
            clamav=Scanner(), cdr=ContentDisarmReconstructor(), required=True,
        ))

    def _assert_saved(self, storage, content, filename):
        compressed = filename.endswith('.gz')
        payload = gzip.compress(content, mtime=0) if compressed else content
        normalized = content.decode('utf-8-sig').replace('\r\n', '\n').replace('\r', '\n').encode()
        expected = gzip.compress(normalized, compresslevel=6, mtime=0) if compressed else normalized
        stored = asyncio.run(storage.save(Upload(payload, filename)))
        self.assertEqual(stored.path.read_bytes(), expected)
        self.assertEqual(stored.size_bytes, len(expected))
        self.assertEqual(stored.sha256, hashlib.sha256(expected).hexdigest())
        self.assertEqual(stored.content_type, 'application/gzip' if compressed else 'text/plain')
        self.assertEqual(stored.security['scan_count'], 2)
        self.assertEqual(storage.security_pipeline.clamav.samples, [payload, expected])
        metadata = json.loads((stored.path.parent / 'metadata.json').read_text(encoding='utf-8'))
        self.assertEqual(metadata['sha256'], stored.sha256)
        self.assertEqual(metadata['size_bytes'], len(expected))
        self.assertEqual(storage.get(stored.file_id), stored)
        self.assertEqual(storage._reserved_bytes, 0)
        self.assertEqual(storage._upload_reservations, {})

    def _assert_rejected(self, storage, content, filename):
        payload = gzip.compress(content, mtime=0) if filename.endswith('.gz') else content
        with self.assertRaisesRegex(ValueError, 'must be UTF-8 text') as caught:
            asyncio.run(storage.save(Upload(payload, filename)))
        self.assertIsInstance(caught.exception.__cause__, UnicodeDecodeError)
        self.assertEqual(storage.security_pipeline.clamav.samples, [])
        self.assertEqual(list(storage.root.iterdir()), [])
        self.assertEqual(storage._reserved_bytes, 0)
        self.assertEqual(storage._upload_reservations, {})

    def test_valid_utf8_crossing_sample_boundary_and_ending_at_eof(self):
        padding = random.Random(2345).randbytes(SNIFF_BYTES).hex().encode()
        for filename in ('sample.txt', 'sample.vcf', 'sample.vcf.gz'):
            header = b'##fileformat=VCFv4.3\n#CHROM\tPOS\n' if '.vcf' in filename else b''
            for char in ('¢', '中', '🙂'):
                encoded = char.encode()
                for split in range(1, len(encoded)):
                    for bom in (b'', b'\xef\xbb\xbf'):
                        for suffix in (b'', b'\r\nnext\r\n'):
                            with self.subTest(filename=filename, char=char, split=split, bom=bool(bom), eof=not suffix):
                                content = bom + header + padding[:SNIFF_BYTES - split - len(bom) - len(header)] + encoded + suffix
                                with tempfile.TemporaryDirectory(prefix='utf8_sample_') as raw:
                                    self._assert_saved(self._storage(raw), content, filename)

    def test_invalid_continuations_crossing_sample_boundary_are_rejected(self):
        padding = random.Random(3456).randbytes(SNIFF_BYTES).hex().encode()
        for filename in ('sample.txt', 'sample.vcf', 'sample.vcf.gz'):
            header = b'##fileformat=VCFv4.3\n#CHROM\tPOS\n' if '.vcf' in filename else b''
            for char in ('¢', '中', '🙂'):
                encoded = char.encode()
                for split in range(1, len(encoded)):
                    for invalid in (b'X', b'\xff'):
                        with self.subTest(filename=filename, char=char, split=split, invalid=invalid):
                            content = header + padding[:SNIFF_BYTES - split - len(header)] + encoded[:split] + invalid + b'\nrest\n'
                            with tempfile.TemporaryDirectory(prefix='utf8_invalid_') as raw:
                                self._assert_rejected(self._storage(raw), content, filename)

    def test_incomplete_boundary_characters_at_real_eof_are_rejected(self):
        padding = random.Random(4567).randbytes(SNIFF_BYTES).hex().encode()
        for filename in ('sample.txt', 'sample.vcf', 'sample.vcf.gz'):
            header = b'##fileformat=VCFv4.3\n#CHROM\tPOS\n' if '.vcf' in filename else b''
            for char in ('¢', '中', '🙂'):
                encoded = char.encode()
                for split in range(1, len(encoded)):
                    for available in range(len(encoded) - split):
                        with self.subTest(filename=filename, char=char, split=split, available=available):
                            content = header + padding[:SNIFF_BYTES - split - len(header)] + encoded[:split + available]
                            with tempfile.TemporaryDirectory(prefix='utf8_eof_') as raw:
                                self._assert_rejected(self._storage(raw), content, filename)

    def test_invalid_utf8_within_sample_is_rejected(self):
        padding = random.Random(7890).randbytes(SNIFF_BYTES).hex().encode()
        for filename in ('sample.txt', 'sample.vcf', 'sample.vcf.gz'):
            header = b'##fileformat=VCFv4.3\n#CHROM\tPOS\n' if '.vcf' in filename else b''
            for invalid in (b'\xff', b'\x80', b'\xc0\xaf', b'\xed\xa0\x80', b'\xf4\x90\x80\x80'):
                with self.subTest(filename=filename, invalid=invalid):
                    content = header + invalid + padding
                    with tempfile.TemporaryDirectory(prefix='utf8_inside_') as raw:
                        self._assert_rejected(self._storage(raw), content, filename)

    def test_cdr_newline_normalization_moves_valid_utf8_across_sample_boundary(self):
        rng = random.Random(5678)
        body = b''.join(('科研🙂\t' + rng.randbytes(8).hex() + 'XYZ\r\n').encode() for _ in range(4096))
        for filename in ('sample.txt', 'sample.vcf', 'sample.vcf.gz'):
            header = b''
            if '.vcf' in filename:
                header = b'##fileformat=VCFv4.3\n#CHROM\tPOS\n'
                header += b'##padding=' + b'A' * (992 - len(header) - 11) + b'\n'
            content = header + body
            content[:SNIFF_BYTES].decode('utf-8')
            with self.subTest(filename=filename), tempfile.TemporaryDirectory(prefix='utf8_cdr_') as raw:
                self._assert_saved(self._storage(raw), content, filename)

    def test_plain_text_sampling_reads_at_most_three_extra_bytes(self):
        with tempfile.TemporaryDirectory(prefix='utf8_bounded_') as raw:
            storage = LocalFileStorage(raw)
            target = Path(raw) / 'sample.txt'
            target.write_bytes(b'A' * (SNIFF_BYTES * 4))
            original_open = Path.open
            reads = []

            class Reader:
                def __enter__(self):
                    self.handle = original_open(target, 'rb')
                    return self

                def __exit__(self, *_args):
                    self.handle.close()

                def read(self, size=-1):
                    reads.append(size)
                    if size < 0 or size > SNIFF_BYTES + 3:
                        raise AssertionError('text sampling read must remain bounded')
                    return self.handle.read(size)

            with mock.patch.object(Path, 'open', return_value=Reader()):
                self.assertEqual(storage._inspect_content(target, target.name, target.stat().st_size), 'text/plain')
            self.assertEqual(len(reads), 1)
            self.assertLessEqual(reads[0], SNIFF_BYTES + 3)

    def test_gzip_sampling_stays_bounded_and_checks_the_complete_stream(self):
        rng = random.Random(6789)
        content = b'##fileformat=VCFv4.3\n#CHROM\tPOS\n' + rng.randbytes(SNIFF_BYTES * 9).hex().encode()
        compressed = gzip.compress(content, mtime=0)
        malware = gzip.compress(content + b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR', mtime=0)
        corrupt = compressed[:-8] + bytes([compressed[-8] ^ 1]) + compressed[-7:]
        with tempfile.TemporaryDirectory(prefix='utf8_gzip_') as raw:
            storage = LocalFileStorage(raw)
            target = Path(raw) / 'sample.vcf.gz'
            target.write_bytes(compressed)
            with mock.patch.object(storage, '_validate_text', wraps=storage._validate_text) as validate:
                self.assertEqual(storage._inspect_content(target, target.name, len(compressed)), 'application/gzip')
            self.assertLessEqual(len(validate.call_args.args[0]), SNIFF_BYTES + 3)
            for payload, message in ((malware, 'malicious'), (corrupt, 'invalid gzip'), (compressed[:-8], 'invalid gzip')):
                with self.subTest(message=message):
                    target.write_bytes(payload)
                    with self.assertRaisesRegex(ValueError, message):
                        storage._inspect_content(target, target.name, len(payload))


class ConcurrentUploadTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_reader_does_not_block_another_upload(self):
        reading = asyncio.Event()
        release = asyncio.Event()

        class SlowUpload(Upload):
            async def read(self, size):
                reading.set()
                await release.wait()
                return await super().read(size)

        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(raw)
            slow = asyncio.create_task(storage.save(SlowUpload(b'slow', 'slow.txt')))
            try:
                await asyncio.wait_for(reading.wait(), 5)
                fast = await asyncio.wait_for(storage.save(Upload(b'fast', 'fast.txt')), 5)
                self.assertEqual(fast.path.read_bytes(), b'fast')
                self.assertFalse(slow.done())
            finally:
                release.set()
                await slow
            self.assertEqual(storage._reserved_bytes, 0)

    async def test_scans_overlap_with_a_bound_and_queued_cancellation(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        guard = threading.Lock()
        counts = {'active': 0, 'peak': 0, 'started': 0}

        class Pipeline:
            def process(self, _path, _filename):
                with guard:
                    counts['active'] += 1
                    counts['started'] += 1
                    counts['peak'] = max(counts['peak'], counts['active'])
                    if counts['active'] == 2:
                        loop.call_soon_threadsafe(started.set)
                try:
                    if not release.wait(5):
                        raise TimeoutError('scan was not released')
                    return FileSecurityResult('clean', 'clean', 'reconstructed', 2)
                finally:
                    with guard:
                        counts['active'] -= 1

        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(raw, security_pipeline=Pipeline())
            tasks = [asyncio.create_task(storage.save(
                Upload(b'content', 'sample.txt'), file_id=str(index) * 32,
            )) for index in (1, 2, 3)]
            try:
                await asyncio.wait_for(started.wait(), 5)
                self.assertEqual(counts['started'], 2)
                for index in (1, 2, 3):
                    with self.assertRaises(FileNotFoundError):
                        storage.get(str(index) * 32)
                tasks[2].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[2]
                self.assertFalse((storage.root / ('3' * 32)).exists())
            finally:
                release.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertEqual(counts['peak'], 2)
            self.assertEqual(counts['started'], 2)
            self.assertEqual(sum(not isinstance(item, BaseException) for item in results), 2)
            self.assertEqual(storage._reserved_bytes, 0)

    async def test_parallel_quota_claims_do_not_double_count_staged_files(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        class Pipeline:
            def process(self, _path, _filename):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise TimeoutError('scan was not released')
                return FileSecurityResult('clean', 'clean', 'disabled', 1)

        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(raw, total_quota_bytes=5500, security_pipeline=Pipeline())
            first = asyncio.create_task(storage.save(Upload(b'a' * 1500, 'first.txt')))
            try:
                await asyncio.wait_for(started.wait(), 5)
                second = asyncio.create_task(storage.save(Upload(b'b' * 1500, 'second.txt')))
                await asyncio.sleep(0)
            finally:
                release.set()
            results = await asyncio.gather(first, second)
            self.assertEqual(len(results), 2)
            self.assertLessEqual(storage._storage_usage(), storage.total_quota_bytes)
            self.assertEqual(storage._reserved_bytes, 0)

    async def test_in_flight_quota_cannot_be_claimed_twice(self):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()

        class Pipeline:
            def process(self, _path, _filename):
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise TimeoutError('scan was not released')
                return FileSecurityResult('clean', 'clean', 'disabled', 1)

        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(raw, total_quota_bytes=4000, security_pipeline=Pipeline())
            first = asyncio.create_task(storage.save(Upload(b'a' * 1500, 'first.txt')))
            try:
                await asyncio.wait_for(started.wait(), 5)
                with self.assertRaisesRegex(ValueError, 'quota'):
                    await storage.save(Upload(b'b' * 1500, 'second.txt'))
            finally:
                release.set()
                await first
            small = await storage.save(Upload(b'small', 'small.txt'))
            self.assertEqual(small.path.read_bytes(), b'small')
            self.assertLessEqual(storage._storage_usage(), storage.total_quota_bytes)
            self.assertEqual(storage._reserved_bytes, 0)

    async def test_reader_cancellation_cleans_up_and_releases_quota(self):
        reading = asyncio.Event()

        class SlowUpload(Upload):
            async def read(self, _size):
                reading.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(raw, total_quota_bytes=1400)
            task = asyncio.create_task(storage.save(SlowUpload(b'content', 'slow.txt')))
            await asyncio.wait_for(reading.wait(), 5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertEqual(list(storage.root.iterdir()), [])
            stored = await storage.save(Upload(b'retry', 'retry.txt'))
            self.assertEqual(stored.path.read_bytes(), b'retry')
            self.assertEqual(storage._reserved_bytes, 0)

    async def test_repeated_cancellation_waits_for_scan_before_cleanup(self):
        for fail_scan in (False, True):
            with self.subTest(fail_scan=fail_scan), tempfile.TemporaryDirectory() as raw:
                loop = asyncio.get_running_loop()
                started = asyncio.Event()
                release = threading.Event()

                class Pipeline:
                    def process(self, path, _filename):
                        loop.call_soon_threadsafe(started.set)
                        if not release.wait(5):
                            raise TimeoutError('scan was not released')
                        Path(path).write_bytes(b'late rewrite')
                        if fail_scan:
                            raise FileSecurityError('late failure')
                        return FileSecurityResult('clean', 'clean', 'reconstructed', 2)

                storage = LocalFileStorage(raw, security_pipeline=Pipeline())
                task = asyncio.create_task(storage.save(Upload(b'content', 'sample.txt')))
                try:
                    await asyncio.wait_for(started.wait(), 5)
                    for _ in range(2):
                        task.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(task.done())
                        self.assertGreater(storage._reserved_bytes, 0)
                        self.assertTrue(list(storage.root.iterdir()))
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
                self.assertEqual(list(storage.root.iterdir()), [])
                self.assertEqual(storage._reserved_bytes, 0)
                retry = await storage.save(Upload(b'retry', 'retry.txt')) if not fail_scan else None
                if retry is not None:
                    self.assertEqual(retry.sha256, hashlib.sha256(b'late rewrite').hexdigest())

    async def test_cdr_growth_rechecks_size_and_quota(self):
        class Pipeline:
            def process(self, path, _filename):
                Path(path).write_bytes(b'x' * 2000)
                return FileSecurityResult('clean', 'clean', 'reconstructed', 2)

        for maximum, quota, error in ((1000, 10000, 'maximum'), (3000, 1800, 'quota')):
            with self.subTest(maximum=maximum), tempfile.TemporaryDirectory() as raw:
                storage = LocalFileStorage(raw, max_bytes=maximum, total_quota_bytes=quota, security_pipeline=Pipeline())
                with self.assertRaisesRegex(ValueError, error):
                    await storage.save(Upload(b'small', 'sample.txt'))
                self.assertEqual(list(storage.root.iterdir()), [])
                self.assertEqual(storage._reserved_bytes, 0)

    async def test_commit_rechecks_external_disk_growth(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)

            class Pipeline:
                def process(self, _path, _filename):
                    (root / 'external.txt').write_bytes(b'x' * 1900)
                    return FileSecurityResult('clean', 'clean', 'disabled', 1)

            storage = LocalFileStorage(root, total_quota_bytes=2200, security_pipeline=Pipeline())
            with self.assertRaisesRegex(ValueError, 'quota'):
                await storage.save(Upload(b'a' * 100, 'sample.txt'))
            self.assertEqual(list(root.iterdir()), [root / 'external.txt'])
            self.assertEqual(storage._reserved_bytes, 0)

    async def test_duplicate_id_preserves_another_in_flight_upload(self):
        reading = asyncio.Event()
        release = asyncio.Event()

        class SlowUpload(Upload):
            async def read(self, size):
                reading.set()
                await release.wait()
                return await super().read(size)

        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(raw)
            file_id = 'a' * 32
            first = asyncio.create_task(storage.save(SlowUpload(b'first', 'first.txt'), file_id))
            try:
                await asyncio.wait_for(reading.wait(), 5)
                with self.assertRaises(FileExistsError):
                    await storage.save(Upload(b'second', 'second.txt'), file_id)
            finally:
                release.set()
            stored = await first
            self.assertEqual(stored.path.read_bytes(), b'first')
            self.assertEqual(storage._reserved_bytes, 0)

    async def test_metadata_failure_cleans_up_and_releases_quota(self):
        write_bytes = Path.write_bytes

        def fail_metadata(path, payload):
            if path.name == 'metadata.json':
                write_bytes(path, payload[:4])
                raise OSError('metadata disk failure')
            return write_bytes(path, payload)

        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(raw, total_quota_bytes=1400)
            with mock.patch.object(Path, 'write_bytes', fail_metadata):
                with self.assertRaisesRegex(OSError, 'metadata disk failure'):
                    await storage.save(Upload(b'content', 'sample.txt'))
            self.assertEqual(list(storage.root.iterdir()), [])
            stored = await storage.save(Upload(b'retry', 'retry.txt'))
            self.assertEqual(stored.path.read_bytes(), b'retry')
            self.assertEqual(storage._reserved_bytes, 0)

    async def test_parallel_scans_keep_each_upload_context(self):
        subject = ContextVar('upload-test-subject', default='missing')

        class Pipeline:
            def process(self, path, _filename):
                Path(path).write_bytes(subject.get().encode('utf-8'))
                return FileSecurityResult('clean', 'clean', 'reconstructed', 2)

        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(raw, security_pipeline=Pipeline())

            async def save(name):
                token = subject.set(name)
                try:
                    return await storage.save(Upload(b'content', f'{name}.txt'))
                finally:
                    subject.reset(token)

            results = await asyncio.gather(save('alice'), save('bob'))
            self.assertEqual([stored.path.read_bytes() for stored in results], [b'alice', b'bob'])
            self.assertEqual(subject.get(), 'missing')

    async def test_failed_cleanup_accounts_for_remaining_disk_bytes(self):
        class Pipeline:
            def process(self, _path, _filename):
                raise FileSecurityError('scan failure')

        unlink = Path.unlink

        def blocked_unlink(path, *args, **kwargs):
            if path.name == 'sample.txt':
                raise OSError('cleanup failure')
            return unlink(path, *args, **kwargs)

        with tempfile.TemporaryDirectory() as raw:
            storage = LocalFileStorage(raw, total_quota_bytes=2200, security_pipeline=Pipeline())
            with mock.patch.object(Path, 'unlink', blocked_unlink):
                with self.assertRaisesRegex(OSError, 'cleanup failure'):
                    await storage.save(Upload(b'x' * 1000, 'sample.txt'))
            self.assertEqual(storage._storage_usage(), 1000)
            self.assertEqual(storage._reserved_bytes, 0)
            self.assertEqual(storage._upload_reservations, {})
            with self.assertRaisesRegex(ValueError, 'quota'):
                await storage.save(Upload(b'x' * 1000, 'retry.txt'))


class UploadInspectionAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def _exercise_stage(self, phase, cancel=False, fail=False):
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        release = threading.Event()
        subject = ContextVar('inspection-test-subject', default='missing')
        observations = []
        state = {'calls': 0, 'scanned': False, 'stat_seen': False, 'hash_ready': False, 'hash_opened': False}

        def observe(stage):
            observations.append((stage, threading.get_ident(), subject.get()))
            if stage == phase:
                loop.call_soon_threadsafe(started.set)
                if not release.wait(5):
                    raise TimeoutError('inspection was not released')
                if fail:
                    raise OSError(f'{stage} worker failure')

        class Storage(LocalFileStorage):
            def _inspect_content(self, target, filename, size_bytes):
                if filename != 'sample.txt':
                    return super()._inspect_content(target, filename, size_bytes)
                state['calls'] += 1
                observe('before' if state['calls'] == 1 else 'after')
                result = super()._inspect_content(target, filename, size_bytes)
                state['hash_ready'] = state['calls'] == 2
                return result

        class Pipeline:
            def process(self, path, _filename):
                observations.append(('scan', threading.get_ident(), subject.get()))
                Path(path).write_bytes(b'rebuilt\r\n')
                state['scanned'] = True
                return FileSecurityResult('clean', 'clean', 'reconstructed', 2)

        original_stat, original_open = Path.stat, Path.open

        def stat(path, *args, **kwargs):
            if path.name == 'sample.txt' and state['scanned'] and not state['stat_seen']:
                state['stat_seen'] = True
                observe('stat')
            return original_stat(path, *args, **kwargs)

        class HashReader:
            def __init__(self, handle):
                self.handle = handle

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return self.handle.__exit__(*args)

            def read(self, size):
                if not state['hash_opened']:
                    state['hash_opened'] = True
                    observe('hash')
                return self.handle.read(size)

        def open_file(path, mode='r', *args, **kwargs):
            handle = original_open(path, mode, *args, **kwargs)
            if path.name == 'sample.txt' and mode == 'rb' and state['hash_ready']:
                return HashReader(handle)
            return handle

        with tempfile.TemporaryDirectory() as raw:
            storage = Storage(raw, security_pipeline=Pipeline())
            token = subject.set(phase)
            try:
                with mock.patch.object(Path, 'stat', stat), mock.patch.object(Path, 'open', open_file):
                    task = asyncio.create_task(storage.save(Upload(b'original', 'sample.txt'), 'a' * 32))
                    try:
                        await asyncio.wait_for(started.wait(), 5)
                        await asyncio.sleep(0)
                        self.assertFalse(task.done())
                        self.assertFalse(storage._quota_lock.locked())
                        self.assertGreater(storage._reserved_bytes, 0)
                        self.assertTrue((storage.root / ('a' * 32) / 'sample.txt').exists())
                        self.assertFalse((storage.root / ('a' * 32) / 'metadata.json').exists())
                        if cancel:
                            for _ in range(3):
                                task.cancel()
                                await asyncio.sleep(0)
                                self.assertFalse(task.done())
                                self.assertGreater(storage._reserved_bytes, 0)
                                self.assertEqual(len(list(storage.root.iterdir())), 1)
                    finally:
                        release.set()
                    if cancel:
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    elif fail:
                        with self.assertRaisesRegex(OSError, f'{phase} worker failure'):
                            await task
                    else:
                        stored = await task
            finally:
                subject.reset(token)
            self.assertEqual(storage._reserved_bytes, 0)
            self.assertEqual(storage._upload_reservations, {})
            self.assertTrue(observations)
            self.assertTrue(all(thread != threading.get_ident() for _, thread, _ in observations))
            self.assertEqual({context for _, _, context in observations}, {phase})
            if cancel or fail:
                self.assertEqual(list(storage.root.iterdir()), [])
                retry = await storage.save(Upload(b'retry', 'retry.txt'))
                self.assertEqual(retry.path.read_bytes(), b'rebuilt\r\n')
            else:
                self.assertEqual([stage for stage, _, _ in observations], ['before', 'scan', 'stat', 'after', 'hash'])
                self.assertEqual(stored.path.read_bytes(), b'rebuilt\r\n')
                self.assertEqual(stored.size_bytes, len(b'rebuilt\r\n'))
                self.assertEqual(stored.sha256, hashlib.sha256(b'rebuilt\r\n').hexdigest())
                self.assertEqual(storage.get(stored.file_id), stored)

    async def test_inspection_stat_and_hash_allow_other_work_and_keep_context(self):
        for phase in ('before', 'stat', 'after', 'hash'):
            with self.subTest(phase=phase):
                await self._exercise_stage(phase)

    async def test_repeated_cancellation_waits_for_inspection_and_hash(self):
        for phase in ('before', 'stat', 'after', 'hash'):
            for fail in (False, True):
                with self.subTest(phase=phase, fail=fail):
                    await self._exercise_stage(phase, cancel=True, fail=fail)

    async def test_inspection_stat_and_hash_errors_clean_up_and_allow_retry(self):
        for phase in ('before', 'stat', 'after', 'hash'):
            with self.subTest(phase=phase):
                await self._exercise_stage(phase, fail=True)

    async def test_inspection_and_scan_share_bound_and_queued_cancellation(self):
        loop = asyncio.get_running_loop()
        scanned, both = asyncio.Event(), asyncio.Event()
        release, guard = threading.Event(), threading.Lock()
        counts = {'active': 0, 'peak': 0, 'started': 0}

        def block(stage):
            with guard:
                counts['active'] += 1
                counts['started'] += 1
                counts['peak'] = max(counts['peak'], counts['active'])
                if stage == 'scan':
                    loop.call_soon_threadsafe(scanned.set)
                if counts['active'] == 2:
                    loop.call_soon_threadsafe(both.set)
            try:
                if not release.wait(5):
                    raise TimeoutError('file worker was not released')
            finally:
                with guard:
                    counts['active'] -= 1

        class Storage(LocalFileStorage):
            def _inspect_content(self, target, filename, size_bytes):
                if filename in {'inspect.txt', 'queued.txt'}:
                    block('inspect')
                return super()._inspect_content(target, filename, size_bytes)

        class Pipeline:
            def process(self, _path, filename):
                if filename == 'scan.txt':
                    block('scan')
                return FileSecurityResult('clean', 'clean', 'disabled', 1)

        with tempfile.TemporaryDirectory() as raw:
            storage = Storage(raw, security_pipeline=Pipeline())
            tasks = [asyncio.create_task(storage.save(Upload(b'content', 'scan.txt'), 'a' * 32))]
            try:
                await asyncio.wait_for(scanned.wait(), 5)
                tasks.append(asyncio.create_task(storage.save(Upload(b'content', 'inspect.txt'), 'b' * 32)))
                await asyncio.wait_for(both.wait(), 5)
                tasks.append(asyncio.create_task(storage.save(Upload(b'content', 'queued.txt'), 'c' * 32)))
                async def wait_staged():
                    while 'c' * 32 not in storage._upload_reservations:
                        await asyncio.sleep(0)

                await asyncio.wait_for(wait_staged(), 5)
                for _ in range(3):
                    tasks[0].cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(tasks[0].done())
                    self.assertEqual(counts['started'], 2)
                tasks[2].cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await tasks[2]
                self.assertFalse((storage.root / ('c' * 32)).exists())
                self.assertEqual(counts['started'], 2)
            finally:
                release.set()
                results = await asyncio.gather(*tasks, return_exceptions=True)
            self.assertIsInstance(results[0], asyncio.CancelledError)
            self.assertEqual(results[1].path.read_bytes(), b'content')
            self.assertEqual(counts['peak'], 2)
            self.assertEqual(list(storage.root.iterdir()), [results[1].path.parent])
            self.assertEqual(storage._reserved_bytes, 0)
            self.assertEqual(storage._upload_reservations, {})

    async def test_reconstructed_content_is_rejected_before_commit(self):
        header = b'##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n'
        for filename, original, rebuilt, limits, message in (
            ('sample.txt', b'original', b'MZpayload', {}, 'executable'),
            ('sample.txt', b'original', b'PK\x03\x04payload', {}, 'archive'),
            ('sample.vcf.gz', gzip.compress(header), b'\x1f\x8bbroken', {}, 'invalid gzip'),
            ('sample.vcf.gz', gzip.compress(header), gzip.compress(b'plain text'), {}, 'does not match'),
            ('sample.vcf.gz', gzip.compress(header), gzip.compress(header + b'A' * 2000), {'max_decompressed_bytes': 1024}, 'decompressed size'),
            ('sample.vcf.gz', gzip.compress(header), gzip.compress(header + b'A' * 2000), {'max_compression_ratio': 3}, 'compression ratio'),
        ):
            with self.subTest(message=message), tempfile.TemporaryDirectory() as raw:
                class Pipeline:
                    def process(self, path, _filename):
                        Path(path).write_bytes(rebuilt)
                        return FileSecurityResult('clean', 'clean', 'reconstructed', 2)

                storage = LocalFileStorage(raw, security_pipeline=Pipeline(), **limits)
                with self.assertRaisesRegex(ValueError, message):
                    await storage.save(Upload(original, filename))
                self.assertEqual(list(storage.root.iterdir()), [])
                self.assertEqual(storage._reserved_bytes, 0)
                self.assertEqual(storage._upload_reservations, {})


class BlockingUsageStorage(LocalFileStorage):
    def __init__(self, root, blocked_call, fail_blocked=False, subject=None):
        super().__init__(root)
        self.loop = asyncio.get_running_loop()
        self.blocked_call = blocked_call
        self.fail_blocked = fail_blocked
        self.subject = subject
        self.started = asyncio.Event()
        self.release = threading.Event()
        self.guard = threading.Lock()
        self.calls = 0
        self.active = 0
        self.peak = 0
        self.threads = []
        self.contexts = []

    def _storage_usage(self, exclude_uploads=()):
        with self.guard:
            self.calls += 1
            call = self.calls
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.threads.append(threading.get_ident())
            if self.subject is not None:
                self.contexts.append(self.subject.get())
        try:
            if call == self.blocked_call:
                self.loop.call_soon_threadsafe(self.started.set)
                if not self.release.wait(5):
                    raise TimeoutError('storage usage was not released')
                if self.fail_blocked:
                    raise OSError('storage usage failure')
            return super()._storage_usage(exclude_uploads)
        finally:
            with self.guard:
                self.active -= 1


class StorageUsageAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_begin_commit_and_abort_allow_other_event_loop_work(self):
        subject = ContextVar('usage-test-subject', default='missing')
        for phase, blocked_call, payload in (
            ('begin', 1, b'content'),
            ('commit', 2, b'content'),
            ('abort', 2, b''),
        ):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw:
                storage = BlockingUsageStorage(raw, blocked_call, subject=subject)
                token = subject.set(phase)
                try:
                    task = asyncio.create_task(storage.save(Upload(payload, 'sample.txt')))
                finally:
                    subject.reset(token)
                try:
                    await asyncio.wait_for(storage.started.wait(), 5)
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertTrue(storage._quota_lock.locked())
                    self.assertFalse(list(storage.root.glob('*/metadata.json')))
                finally:
                    storage.release.set()
                if phase == 'abort':
                    with self.assertRaisesRegex(ValueError, 'empty'):
                        await task
                    self.assertEqual(list(storage.root.iterdir()), [])
                else:
                    stored = await task
                    self.assertEqual(storage.get(stored.file_id), stored)
                self.assertEqual(storage._reserved_bytes, 0)
                self.assertEqual(set(storage.contexts), {phase})
                self.assertNotIn(threading.get_ident(), storage.threads)
                self.assertEqual(storage.peak, 1)

    async def test_repeated_cancellation_keeps_usage_worker_bounded(self):
        for phase, blocked_call in (('begin', 1), ('commit', 2)):
            for fail_worker in (False, True):
                with self.subTest(phase=phase, fail_worker=fail_worker), tempfile.TemporaryDirectory() as raw:
                    storage = BlockingUsageStorage(raw, blocked_call, fail_worker)
                    task = asyncio.create_task(storage.save(
                        Upload(b'cancelled', 'cancelled.txt'), 'a' * 32,
                    ))
                    queued = None
                    try:
                        await asyncio.wait_for(storage.started.wait(), 5)
                        queued = asyncio.create_task(storage.save(
                            Upload(b'retry', 'retry.txt'), 'b' * 32,
                        ))
                        for _ in range(2):
                            task.cancel()
                            await asyncio.sleep(0)
                            self.assertFalse(task.done())
                            self.assertFalse(queued.done())
                            self.assertEqual(storage.calls, blocked_call)
                            self.assertTrue(storage._quota_lock.locked())
                    finally:
                        storage.release.set()
                        results = await asyncio.gather(task, *([queued] if queued else []), return_exceptions=True)
                    self.assertIsInstance(results[0], asyncio.CancelledError)
                    stored = results[1]
                    self.assertEqual(stored.path.read_bytes(), b'retry')
                    self.assertEqual(list(storage.root.iterdir()), [stored.path.parent])
                    self.assertEqual(storage._reserved_bytes, 0)
                    self.assertEqual(storage._upload_reservations, {})
                    self.assertEqual(storage.peak, 1)

    async def test_cancellation_cannot_interrupt_cleanup_waiting_for_quota_lock(self):
        reading = asyncio.Event()

        class SlowUpload(Upload):
            async def read(self, _size):
                reading.set()
                await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as raw:
            storage = BlockingUsageStorage(raw, 2)
            task = asyncio.create_task(storage.save(
                SlowUpload(b'content', 'cancelled.txt'), 'a' * 32,
            ))
            queued = None
            try:
                await asyncio.wait_for(reading.wait(), 5)
                queued = asyncio.create_task(storage.save(Upload(b'retry', 'retry.txt'), 'b' * 32))
                await asyncio.wait_for(storage.started.wait(), 5)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertIn('a' * 32, storage._upload_reservations)
            finally:
                storage.release.set()
                results = await asyncio.gather(task, *([queued] if queued else []), return_exceptions=True)
            self.assertIsInstance(results[0], asyncio.CancelledError)
            stored = results[1]
            self.assertEqual(list(storage.root.iterdir()), [stored.path.parent])
            self.assertEqual(storage._reserved_bytes, 0)
            self.assertEqual(storage._upload_reservations, {})
            self.assertEqual(storage.peak, 1)

    async def test_repeated_cancellation_waits_for_cleanup_usage_refresh(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'external.txt').write_bytes(b'existing')
            storage = BlockingUsageStorage(root, 2)
            task = asyncio.create_task(storage.save(Upload(b'', 'empty.txt')))
            try:
                await asyncio.wait_for(storage.started.wait(), 5)
                for _ in range(3):
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
            finally:
                storage.release.set()
            with self.assertRaisesRegex(ValueError, 'empty'):
                await task
            self.assertEqual(storage._quota_usage_bytes, len(b'existing'))
            self.assertEqual(storage._reserved_bytes, 0)
            self.assertEqual(storage._upload_reservations, {})
            self.assertEqual(list(root.iterdir()), [root / 'external.txt'])

    async def test_usage_worker_errors_propagate_and_release_quota(self):
        for phase, blocked_call in (('begin', 1), ('commit', 2)):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as raw:
                storage = BlockingUsageStorage(raw, blocked_call, fail_blocked=True)
                storage.release.set()
                with self.assertRaisesRegex(OSError, 'storage usage failure'):
                    await storage.save(Upload(b'content', 'sample.txt'))
                self.assertEqual(list(storage.root.iterdir()), [])
                self.assertEqual(storage._reserved_bytes, 0)
                retry = await storage.save(Upload(b'retry', 'retry.txt'))
                self.assertEqual(retry.path.read_bytes(), b'retry')


if __name__ == '__main__':
    unittest.main()
