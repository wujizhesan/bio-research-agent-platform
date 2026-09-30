import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.job_execution import JobExecutionError
from src.plugin_container import ContainerToolExecutor
from src.storage_workspace import (
    S3ObjectReference, StorageIntegrityError, StorageQuotaExceededError,
    materialize_storage_references,
)


class InputClient:
    def __init__(self, payloads, fallback=False):
        self.payloads = payloads
        self.heads = []
        self.downloads = []
        self.bytes_written = 0
        self.head_override = {}
        self.delivered = None
        if fallback:
            self.download_fileobj = None

    def head_object(self, **request):
        self.heads.append(request)
        payload = self.payloads[request['Key']]
        return {
            'ContentLength': len(payload), 'VersionId': 'version-1',
            'Metadata': {'sha256': hashlib.sha256(payload).hexdigest()},
            **self.head_override,
        }

    def download_fileobj(self, bucket, key, fileobj, ExtraArgs=None):
        self.downloads.append((bucket, key, ExtraArgs))
        data = self.payloads[key] if self.delivered is None else self.delivered
        self.bytes_written += fileobj.write(data)

    def download_file(self, bucket, key, filename, ExtraArgs=None):
        self.downloads.append((bucket, key, ExtraArgs))
        data = self.payloads[key] if self.delivered is None else self.delivered
        self.bytes_written += Path(filename).write_bytes(data)


def reference(key, payload, bucket='research-inputs'):
    return S3ObjectReference(bucket, key, 'version-1', hashlib.sha256(payload).hexdigest(), len(payload)).serialize()


class MaterializationQuotaTests(unittest.TestCase):
    def materialize(self, values, workspace, client, quota=None):
        return materialize_storage_references(
            values, workspace, client=client,
            configured_bucket='research-inputs', configured_prefix='bio-agent',
            expected_owner='123456789012', max_bytes=quota,
        )

    def environment(self):
        return patch.dict(os.environ, {
            'S3_BUCKET': 'research-inputs', 'S3_PREFIX': 'bio-agent',
            'S3_EXPECTED_BUCKET_OWNER': '123456789012',
        })

    def test_oversized_object_is_rejected_after_head_before_download(self):
        key, payload = 'bio-agent/input.bin', b'evidence'
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as raw:
                client = InputClient({key: payload}, fallback=fallback)
                workspace = Path(raw) / 'materialized'
                with self.assertRaises(StorageQuotaExceededError):
                    self.materialize(reference(key, payload), workspace, client, len(payload) - 1)
                self.assertEqual(client.heads, [{
                    'Bucket': 'research-inputs', 'Key': key, 'VersionId': 'version-1',
                    'ExpectedBucketOwner': '123456789012',
                }])
                self.assertEqual(client.downloads, [])
                self.assertEqual(client.bytes_written, 0)
                self.assertFalse(workspace.exists())

    def test_aggregate_budget_stops_before_next_unique_download(self):
        payloads = {f'bio-agent/{index}.bin': bytes([index]) * 4 for index in range(3)}
        values = [reference(key, payload) for key, payload in payloads.items()]
        for fallback in (False, True):
            with self.subTest(fallback=fallback), tempfile.TemporaryDirectory() as raw:
                client = InputClient(payloads, fallback=fallback)
                workspace = Path(raw) / 'materialized'
                with self.assertRaises(StorageQuotaExceededError):
                    self.materialize({'first': values[0], 'rest': (values[0], values[1], values[2])}, workspace, client, 8)
                self.assertEqual(len(client.heads), 3)
                self.assertEqual(len(client.downloads), 2)
                self.assertEqual(client.bytes_written, 8)
                self.assertEqual(sorted(path.read_bytes() for path in workspace.rglob('*.bin')), [b'\x00' * 4, b'\x01' * 4])
                rejected = hashlib.sha256(values[2].encode()).hexdigest()[:24]
                self.assertFalse((workspace / rejected).exists())

    def test_exact_boundary_deduplicates_and_preserves_nested_values(self):
        payloads = {'bio-agent/first.bin': b'1234', 'bio-agent/second.bin': b'5678'}
        first, second = [reference(key, payload) for key, payload in payloads.items()]
        values = {'first': first, 'rest': (first, [second, first], 'local.txt', 42, None)}
        for quota in (8, None):
            with self.subTest(quota=quota), tempfile.TemporaryDirectory() as raw:
                client = InputClient(payloads)
                result = self.materialize(values, raw, client, quota)
                self.assertEqual(result['first'], result['rest'][0])
                self.assertEqual(result['first'], result['rest'][1][1])
                self.assertIsInstance(result['rest'], tuple)
                self.assertIsInstance(result['rest'][1], list)
                self.assertEqual(result['rest'][2:], ('local.txt', 42, None))
                self.assertEqual(Path(result['first']).read_bytes(), b'1234')
                self.assertEqual(Path(result['rest'][1][0]).read_bytes(), b'5678')
                self.assertEqual(client.bytes_written, 8)
                self.assertEqual(len(client.heads), 2)
                self.assertEqual(len(client.downloads), 2)
                self.assertTrue(all(call[2] == {'VersionId': 'version-1', 'ExpectedBucketOwner': '123456789012'} for call in client.downloads))

    def test_zero_budget_and_invalid_budget_do_not_download(self):
        payload = b'data'
        key = 'bio-agent/input.bin'
        with tempfile.TemporaryDirectory() as raw:
            client = InputClient({key: payload})
            values = {'path': 'local.txt', 'other': [0, None, ('plain',)]}
            self.assertEqual(self.materialize(values, raw, client, 0), values)
            self.assertEqual(client.heads, [])
            with self.assertRaises(StorageQuotaExceededError):
                self.materialize(reference(key, payload), raw, client, 0)
            self.assertEqual(len(client.heads), 1)
            self.assertEqual(client.downloads, [])
            for quota in (-1, 1.5, '8', True):
                with self.subTest(quota=quota), self.assertRaises(ValueError):
                    self.materialize(reference(key, payload), raw, client, quota)
            self.assertEqual(len(client.heads), 1)

    def test_metadata_and_root_validation_precede_quota_rejection(self):
        key, payload = 'bio-agent/input.bin', b'data'
        cases = [
            ({'VersionId': 'different'}, 'version does not match', key, 'research-inputs'),
            ({'Metadata': {'sha256': '0' * 64}}, 'metadata checksum', key, 'research-inputs'),
            ({'ContentLength': 99}, 'size does not match', key, 'research-inputs'),
            ({}, 'bucket is not allowed', key, 'forbidden-inputs'),
            ({}, 'outside the configured prefix', 'other/input.bin', 'research-inputs'),
        ]
        for override, message, selected_key, bucket in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as raw:
                client = InputClient({selected_key: payload})
                client.head_override = override
                with self.assertRaisesRegex(StorageIntegrityError, message) as raised:
                    self.materialize(reference(selected_key, payload, bucket), raw, client, 1)
                self.assertNotIsInstance(raised.exception, StorageQuotaExceededError)
                self.assertEqual(client.downloads, [])
                self.assertEqual(len(client.heads), 1 if bucket == 'research-inputs' and selected_key == key else 0)

    def test_integrity_failures_keep_original_errors_and_remove_partial_files(self):
        key, payload = 'bio-agent/input.bin', b'data'
        for delivered in (b'bad!', b'dat', b'data!'):
            for fallback in (False, True):
                with self.subTest(delivered=delivered, fallback=fallback), tempfile.TemporaryDirectory() as raw:
                    client = InputClient({key: payload}, fallback=fallback)
                    client.delivered = delivered
                    with self.assertRaises(StorageIntegrityError) as raised:
                        self.materialize(reference(key, payload), raw, client, 4)
                    self.assertNotIsInstance(raised.exception, StorageQuotaExceededError)
                    self.assertEqual(list(Path(raw).rglob('*.bin')), [])

    def test_distinct_reference_strings_keep_existing_quota_accounting(self):
        key, payload = 'bio-agent/input.bin', b'data'
        value = reference(key, payload)
        with tempfile.TemporaryDirectory() as raw:
            client = InputClient({key: payload})
            with self.assertRaises(StorageQuotaExceededError):
                self.materialize([value, value + '&tag=other'], raw, client, 4)
            self.assertEqual(len(client.heads), 2)
            self.assertEqual(len(client.downloads), 1)
            self.assertEqual(client.bytes_written, 4)

    def test_container_quota_error_cleans_workspace_and_allows_retry(self):
        key, payload = 'bio-agent/input.bin', b'evidence'
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            client = InputClient({key: payload})
            observed = []

            def transport(_path, request, _timeout):
                path = Path(request['arguments']['input_path'])
                observed.append(path.read_bytes())
                return {'ok': True, 'result': {'status': 'ok'}}

            executor = ContainerToolExecutor(
                'http://sandbox.invalid', 'quota-test-token-0000000000000000',
                input_workspace_root=root / 'exchange', storage_client=client,
                workspace_max_bytes=4, transport=transport,
            )
            try:
                with self.environment(), patch('src.plugin_container._tool_filesystem_contract', return_value=({'input_path'}, {}, set())):
                    with self.assertRaisesRegex(JobExecutionError, 'plugin workspace input quota exceeded') as raised:
                        executor.execute('demo', {'input_path': reference(key, payload)})
                    self.assertEqual(raised.exception.error_code, 'execution_failed')
                    self.assertEqual(client.downloads, [])
                    self.assertEqual(observed, [])
                    self.assertEqual(list((root / 'exchange').iterdir()), [])
                    executor.workspace_max_bytes = 8
                    self.assertEqual(executor.execute('demo', {'input_path': reference(key, payload)})['status'], 'ok')
            finally:
                executor.shutdown()
            self.assertEqual(observed, [payload])
            self.assertEqual(list((root / 'exchange').iterdir()), [])

    def test_container_counts_materialized_and_local_inputs_together(self):
        key, payload = 'bio-agent/input.bin', b'data'
        for quota in (7, 8):
            with self.subTest(quota=quota), tempfile.TemporaryDirectory() as raw:
                root = Path(raw)
                source = root / 'local.bin'
                source.write_bytes(payload)
                client = InputClient({key: payload})
                transport = Mock(return_value={'ok': True, 'result': {'status': 'ok'}})
                executor = ContainerToolExecutor(
                    'http://sandbox.invalid', 'quota-test-token-0000000000000000',
                    input_workspace_root=root / 'exchange', input_roots=(root,),
                    storage_client=client, workspace_max_bytes=quota, transport=transport,
                )
                try:
                    with self.environment(), patch('src.plugin_container._tool_filesystem_contract', return_value=({'input_paths'}, {}, set())):
                        if quota == 7:
                            with self.assertRaisesRegex(JobExecutionError, 'input quota'):
                                executor.execute('demo', {'input_paths': [reference(key, payload), str(source)]})
                            transport.assert_not_called()
                        else:
                            self.assertEqual(executor.execute('demo', {'input_paths': [reference(key, payload), str(source)]})['status'], 'ok')
                            transport.assert_called_once()
                finally:
                    executor.shutdown()
                self.assertEqual(client.bytes_written, 4)
                self.assertEqual(list((root / 'exchange').iterdir()), [])
                self.assertEqual(source.read_bytes(), payload)

    def test_container_retains_actual_workspace_usage_scan(self):
        key, payload = 'bio-agent/input.bin', b'data'

        class ExtraFileClient(InputClient):
            def download_fileobj(self, bucket, key, fileobj, ExtraArgs=None):
                super().download_fileobj(bucket, key, fileobj, ExtraArgs=ExtraArgs)
                (Path(fileobj._handle.name).parents[2] / 'unexpected.bin').write_bytes(b'extra')

        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            transport = Mock()
            executor = ContainerToolExecutor(
                'http://sandbox.invalid', 'quota-test-token-0000000000000000',
                input_workspace_root=root / 'exchange', storage_client=ExtraFileClient({key: payload}),
                workspace_max_bytes=4, transport=transport,
            )
            try:
                with self.environment(), self.assertRaisesRegex(JobExecutionError, 'input quota'):
                    executor.execute('demo', {'input_path': reference(key, payload)})
            finally:
                executor.shutdown()
            transport.assert_not_called()
            self.assertEqual(list((root / 'exchange').iterdir()), [])


if __name__ == '__main__':
    unittest.main()
