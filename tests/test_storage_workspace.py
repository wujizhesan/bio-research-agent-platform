import hashlib
import io
from pathlib import Path
import tempfile
from threading import Event, Lock
import unittest

from boto3.s3.transfer import TransferConfig
from botocore.exceptions import ReadTimeoutError
from botocore.response import StreamingBody

from scripts.benchmark_storage_materialization import SimulatedS3Client
from src.storage_workspace import (
    S3ObjectReference, StorageIntegrityError, _ChecksumWriter, _verified_s3_download,
    materialize_storage_references,
)


class StreamingClient:
    def __init__(self, payload, delivered=None, failure=False):
        self.payload = payload
        self.delivered = payload if delivered is None else delivered
        self.failure = failure
        self.downloads = []

    def head_object(self, **_kwargs):
        return {
            'ContentLength': len(self.payload), 'VersionId': 'version-1',
            'Metadata': {'sha256': hashlib.sha256(self.payload).hexdigest()},
        }

    def download_fileobj(self, bucket, key, fileobj, ExtraArgs=None):
        self.downloads.append((bucket, key, ExtraArgs))
        for offset in range(0, len(self.delivered), 3):
            fileobj.write(self.delivered[offset:offset + 3])
            if self.failure:
                raise OSError('interrupted download')


def reference(payload):
    return S3ObjectReference(
        'research-inputs', 'bio-agent/input.bin', 'version-1',
        hashlib.sha256(payload).hexdigest(), len(payload),
    )


class StorageWorkspaceTests(unittest.TestCase):
    def materialize(self, client, target):
        return _verified_s3_download(
            reference(client.payload), target, client, configured_bucket='research-inputs',
            configured_prefix='bio-agent', expected_owner='123456789012',
        )

    def test_streaming_download_is_version_locked_and_deduplicated(self):
        payload = b'TP53 DNA damage repair'
        client = StreamingClient(payload)
        with tempfile.TemporaryDirectory(prefix='streaming_input_') as raw:
            resolved = materialize_storage_references(
                {'first': reference(payload).serialize(), 'nested': [reference(payload).serialize()]},
                raw, client=client, configured_bucket='research-inputs',
                configured_prefix='bio-agent', expected_owner='123456789012',
            )
            self.assertEqual(resolved['first'], resolved['nested'][0])
            self.assertEqual(Path(resolved['first']).read_bytes(), payload)
        self.assertEqual(client.downloads, [(
            'research-inputs', 'bio-agent/input.bin',
            {'VersionId': 'version-1', 'ExpectedBucketOwner': '123456789012'},
        )])

    def test_corruption_size_mismatch_and_interrupt_remove_partial_file(self):
        payload = b'TP53 DNA repair'
        cases = [
            (StreamingClient(payload, b'TP53 RNA repair'), 'checksum'),
            (StreamingClient(payload, payload[:-1]), 'size'),
            (StreamingClient(payload, payload + b'extra'), 'size'),
            (StreamingClient(payload, failure=True), 'download failed'),
        ]
        with tempfile.TemporaryDirectory(prefix='streaming_failure_') as raw:
            for index, (client, message) in enumerate(cases):
                with self.subTest(message=message):
                    target = Path(raw) / str(index) / 'input.bin'
                    with self.assertRaisesRegex(StorageIntegrityError, message):
                        self.materialize(client, target)
                    self.assertFalse(target.exists())

    def test_invalid_remote_metadata_rejects_before_download(self):
        with tempfile.TemporaryDirectory(prefix='streaming_metadata_') as raw:
            for field in ('VersionId', 'Metadata', 'ContentLength'):
                with self.subTest(field=field):
                    client = StreamingClient(b'evidence')
                    head = client.head_object()
                    head[field] = {'sha256': '0' * 64} if field == 'Metadata' else 'invalid'
                    if field == 'ContentLength':
                        head[field] = 999
                    client.head_object = lambda **_kwargs: head
                    target = Path(raw) / field / 'input.bin'
                    with self.assertRaises(StorageIntegrityError):
                        self.materialize(client, target)
                    self.assertEqual(client.downloads, [])
                    self.assertFalse(target.exists())

    def test_incomplete_file_write_is_rejected(self):
        class ShortWriter(io.BytesIO):
            def write(self, data):
                return super().write(data[:-1])

        writer = _ChecksumWriter(ShortWriter(), 10)
        with self.assertRaisesRegex(StorageIntegrityError, 'incomplete'):
            writer.write(b'evidence')

    def test_real_multipart_transfer_orders_reversed_completion(self):
        payload = b''.join(bytes([number]) * (1024 * 1024) for number in range(4)) + b'tail'

        class ReorderedClient(SimulatedS3Client):
            def __init__(self):
                super().__init__(payload, TransferConfig(
                    multipart_threshold=1024 * 1024, multipart_chunksize=1024 * 1024,
                    max_concurrency=4,
                ))
                self.completed = []
                self.guard = Lock()
                self.later_ready = Event()

            def get_object(self, Range=None, **kwargs):
                start = int(Range.removeprefix('bytes=').split('-')[0])
                if start == 0:
                    if not self.later_ready.wait(5):
                        raise RuntimeError('parallel download did not complete a later range')
                result = super().get_object(Range=Range, **kwargs)
                with self.guard:
                    self.completed.append(start)
                if start:
                    self.later_ready.set()
                return result

        client = ReorderedClient()
        with tempfile.TemporaryDirectory(prefix='streaming_multipart_') as raw:
            target = Path(raw) / 'job' / 'input.bin'
            self.materialize(client, target)
            self.assertEqual(target.read_bytes(), payload)
        self.assertNotEqual(client.completed[0], 0)

    def test_real_multipart_retry_does_not_hash_repeated_bytes(self):
        payload = b'a' * (2 * 1024 * 1024) + b'distinct tail'

        class FailingBody(io.BytesIO):
            reads = 0

            def read(self, amount=-1):
                self.reads += 1
                if self.reads == 2:
                    raise ReadTimeoutError(endpoint_url='https://example.invalid')
                return super().read(amount)

        class RetryClient(SimulatedS3Client):
            def __init__(self):
                super().__init__(payload, TransferConfig(
                    multipart_threshold=1024 * 1024, multipart_chunksize=1024 * 1024,
                    max_concurrency=1, io_chunksize=256 * 1024,
                ))
                self.attempts = 0

            def get_object(self, Range=None, **kwargs):
                result = super().get_object(Range=Range, **kwargs)
                if Range.startswith('bytes=0-'):
                    self.attempts += 1
                    if self.attempts == 1:
                        first_part = self.payload[:1024 * 1024]
                        result['Body'] = StreamingBody(FailingBody(first_part), len(first_part))
                return result

        client = RetryClient()
        with tempfile.TemporaryDirectory(prefix='streaming_retry_') as raw:
            target = Path(raw) / 'job' / 'input.bin'
            self.materialize(client, target)
            self.assertEqual(target.read_bytes(), payload)
        self.assertEqual(client.attempts, 2)
