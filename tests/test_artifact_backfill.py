import asyncio
import hashlib
from pathlib import Path
import tempfile
import unittest

from artifact_backfill import LegacyArtifactBackfill
from database import Database
from storage_workspace import S3ObjectReference


class FakeS3Client:
    def __init__(self, *, version_id, sha256, size_bytes):
        self.version_id = version_id
        self.sha256 = sha256
        self.size_bytes = size_bytes
        self.requests = []

    def head_object(self, **request):
        self.requests.append(request)
        return {
            'VersionId': self.version_id,
            'ContentLength': self.size_bytes,
            'ContentType': 'application/json',
            'Metadata': {'sha256': self.sha256},
        }


class LegacyArtifactBackfillTests(unittest.TestCase):
    def _database(self, root):
        database = Database(
            f"sqlite+aiosqlite:///{(root / 'database.sqlite3').as_posix()}"
        )
        asyncio.run(database.init_schema())
        return database

    def _job(self, database, artifacts, job_id='legacy-job'):
        asyncio.run(database.upsert_job({
            'job_id': job_id,
            'tool': 'legacy_tool',
            'status': 'completed',
            'created_at': '2026-09-21T00:00:00+00:00',
            'finished_at': '2026-09-21T00:00:01+00:00',
            '_arguments': {},
            'result': {'status': 'ok'},
            'artifacts': artifacts,
        }))

    def test_local_backfill_is_validated_accounted_and_idempotent(self):
        with tempfile.TemporaryDirectory(prefix='artifact_backfill_') as raw:
            root = Path(raw)
            artifact_root = root / 'output'
            artifact_root.mkdir()
            artifact = artifact_root / 'result.json'
            artifact.write_bytes(b'{"status":"ok"}')
            sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
            database = self._database(root)
            try:
                self._job(database, [{
                    'artifact_id': 'legacy-result',
                    'path': str(artifact),
                    'size_bytes': artifact.stat().st_size,
                    'sha256': sha256,
                }])
                runner = LegacyArtifactBackfill(
                    database,
                    artifact_root=artifact_root,
                    quota_bytes=1024,
                )
                checked = asyncio.run(runner.run())
                self.assertFalse(checked['passed'])
                self.assertEqual(checked['ready_to_register'], 1)
                applied = asyncio.run(runner.run(apply=True))
                self.assertTrue(applied['passed'])
                self.assertEqual(applied['registered'], 1)
                stored = asyncio.run(database.list_job_artifacts(
                    'legacy-job', statuses=('committed',)
                ))
                usage = asyncio.run(database.get_project_storage_usage(
                    'system-legacy'
                ))
                self.assertEqual(len(stored), 1)
                self.assertEqual(stored[0]['sha256'], sha256)
                self.assertEqual(usage['used_bytes'], artifact.stat().st_size)
                repeated = asyncio.run(runner.run(apply=True))
                repeated_usage = asyncio.run(database.get_project_storage_usage(
                    'system-legacy'
                ))
                self.assertTrue(repeated['passed'])
                self.assertEqual(repeated['already_registered'], 1)
                self.assertEqual(repeated_usage['used_bytes'], usage['used_bytes'])
            finally:
                asyncio.run(database.close())

    def test_invalid_artifact_is_quarantined_without_consuming_quota(self):
        with tempfile.TemporaryDirectory(prefix='artifact_quarantine_') as raw:
            root = Path(raw)
            artifact_root = root / 'output'
            artifact_root.mkdir()
            artifact = artifact_root / 'result.txt'
            artifact.write_text('actual', encoding='utf-8')
            database = self._database(root)
            try:
                self._job(database, [{
                    'artifact_id': 'legacy-result',
                    'path': str(artifact),
                    'sha256': '0' * 64,
                    'size_bytes': artifact.stat().st_size,
                }])
                runner = LegacyArtifactBackfill(
                    database,
                    artifact_root=artifact_root,
                )
                summary = asyncio.run(runner.run(apply=True))
                stored = asyncio.run(database.list_job_artifacts('legacy-job'))
                usage = asyncio.run(database.get_project_storage_usage(
                    'system-legacy'
                ))
                self.assertFalse(summary['passed'])
                self.assertEqual(summary['quarantined'], 1)
                self.assertEqual(summary['issues'][0]['reason'], 'checksum_mismatch')
                self.assertEqual(stored[0]['status'], 'quarantined')
                self.assertIn('checksum_mismatch', stored[0]['last_error'])
                self.assertIsNone(usage)
            finally:
                asyncio.run(database.close())

    def test_fixed_quarantine_can_be_registered(self):
        with tempfile.TemporaryDirectory(prefix='artifact_repair_') as raw:
            root = Path(raw)
            artifact_root = root / 'output'
            artifact_root.mkdir()
            artifact = artifact_root / 'result.txt'
            artifact.write_text('actual', encoding='utf-8')
            database = self._database(root)
            try:
                self._job(database, [{
                    'artifact_id': 'legacy-result',
                    'path': str(artifact),
                    'sha256': '0' * 64,
                }])
                runner = LegacyArtifactBackfill(
                    database,
                    artifact_root=artifact_root,
                )
                asyncio.run(runner.run(apply=True))
                sha256 = hashlib.sha256(artifact.read_bytes()).hexdigest()
                self._job(database, [{
                    'artifact_id': 'legacy-result',
                    'path': str(artifact),
                    'sha256': sha256,
                    'size_bytes': artifact.stat().st_size,
                }])
                summary = asyncio.run(runner.run(apply=True))
                stored = asyncio.run(database.list_job_artifacts('legacy-job'))
                self.assertTrue(summary['passed'], summary)
                self.assertEqual(summary['registered'], 1)
                self.assertEqual(stored[0]['status'], 'committed')
                self.assertNotIn('last_error', stored[0])
            finally:
                asyncio.run(database.close())

    def test_s3_backfill_requires_exact_version_and_integrity_metadata(self):
        with tempfile.TemporaryDirectory(prefix='artifact_s3_backfill_') as raw:
            root = Path(raw)
            database = self._database(root)
            sha256 = hashlib.sha256(b'{}').hexdigest()
            reference = S3ObjectReference(
                'research-bucket',
                'bio-agent/artifacts/project/job/result.json',
                'version-1',
                sha256,
                2,
            ).serialize()
            client = FakeS3Client(
                version_id='version-1',
                sha256=sha256,
                size_bytes=2,
            )
            try:
                self._job(database, [{
                    'artifact_id': 'legacy-s3-result',
                    'storage_backend': 's3',
                    'reference': reference,
                }])
                runner = LegacyArtifactBackfill(
                    database,
                    artifact_root=root / 'output',
                    s3_bucket='research-bucket',
                    s3_prefix='bio-agent',
                    s3_expected_owner='123456789012',
                    s3_client=client,
                )
                summary = asyncio.run(runner.run(apply=True))
                stored = asyncio.run(database.list_job_artifacts('legacy-job'))
                self.assertTrue(summary['passed'], summary)
                self.assertEqual(stored[0]['version_id'], 'version-1')
                self.assertEqual(client.requests[0]['ExpectedBucketOwner'], '123456789012')
            finally:
                asyncio.run(database.close())

    def test_malformed_size_and_outside_path_are_reported_not_fatal(self):
        with tempfile.TemporaryDirectory(prefix='artifact_bad_manifest_') as raw:
            root = Path(raw)
            artifact_root = root / 'output'
            artifact_root.mkdir()
            outside = root / 'outside.txt'
            outside.write_text('outside', encoding='utf-8')
            database = self._database(root)
            try:
                self._job(database, [{
                    'path': str(outside),
                    'size_bytes': 'not-an-integer',
                }])
                runner = LegacyArtifactBackfill(
                    database,
                    artifact_root=artifact_root,
                )
                summary = asyncio.run(runner.run(apply=True))
                self.assertFalse(summary['passed'])
                self.assertEqual(
                    summary['issues'][0]['reason'],
                    'invalid_integrity_metadata',
                )
                self.assertEqual(summary['quarantined'], 1)
            finally:
                asyncio.run(database.close())


if __name__ == '__main__':
    unittest.main()
