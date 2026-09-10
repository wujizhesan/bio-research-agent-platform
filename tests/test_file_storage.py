import asyncio
import gzip
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from src.file_security import (
    ClamAVScanner,
    ContentDisarmReconstructor,
    FileSecurityError,
    FileSecurityPipeline,
    build_file_security_pipeline_from_env,
)
from src.file_storage import LocalFileStorage, S3FileStorage


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

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.objects[(bucket, key)] = Path(filename).read_bytes()
        self.uploads.append((bucket, key, ExtraArgs))

    def list_objects_v2(self, Bucket, Prefix):
        return {
            'Contents': [
                {'Key': key}
                for bucket, key in self.objects
                if bucket == Bucket and key.startswith(Prefix)
            ]
        }

    def head_object(self, Bucket, Key):
        content = self.objects[(Bucket, Key)]
        return {'ContentType': 'text/plain', 'ContentLength': len(content), 'Metadata': {}}

    def download_file(self, bucket, key, filename):
        Path(filename).write_bytes(self.objects[(bucket, key)])


class S3FileStorageTests(unittest.TestCase):
    def test_upload_and_cache_miss_download(self):
        client = FakeS3Client()
        fake_boto3 = types.ModuleType('boto3')
        fake_boto3.client = lambda *_args, **_kwargs: client
        with tempfile.TemporaryDirectory(prefix='s3_storage_') as raw:
            with mock.patch.dict(sys.modules, {'boto3': fake_boto3}):
                storage = S3FileStorage(Path(raw) / 'uploads', bucket='bio-test', prefix='research')
                stored = asyncio.run(storage.save(Upload(b'@read1\nACGT\n')))
                self.assertEqual(stored.storage_key, f'research/{stored.file_id}/reads.fastq')
                self.assertEqual(client.uploads[0][0:2], ('bio-test', stored.storage_key))
                stored.path.unlink()
                stored.path.parent.joinpath('metadata.json').unlink()
                restored = asyncio.run(storage.aget(stored.file_id))
                self.assertEqual(restored.storage_key, stored.storage_key)
                self.assertEqual(restored.path.read_bytes(), b'@read1\nACGT\n')


class LocalFileStorageSecurityTests(unittest.TestCase):
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


if __name__ == '__main__':
    unittest.main()
