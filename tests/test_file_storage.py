import asyncio
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock

from src.file_storage import S3FileStorage


class Upload:
    filename = 'reads.fastq'
    content_type = 'text/plain'

    def __init__(self, content):
        self.content = content

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


if __name__ == '__main__':
    unittest.main()
