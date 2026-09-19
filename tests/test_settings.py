import unittest
from unittest.mock import patch

from src.settings import PlatformSettings, SettingsError


class PlatformSettingsTests(unittest.TestCase):
    def test_parses_typed_values_and_builds_sanitized_fingerprint(self):
        base = {
            'REDIS_URL': 'redis://user:secret@redis:6379/3',
            'DATABASE_URL': 'postgresql://user:secret@db:5432/bioagent',
            'WORKER_MAX_CONCURRENCY': '7',
            'WORKER_CAPABILITY_ROUTING': 'true',
            'RESEARCH_PLANNER_API_KEY': 'first-secret',
        }
        first = PlatformSettings.from_env(base)
        second = PlatformSettings.from_env({
            **base,
            'RESEARCH_PLANNER_API_KEY': 'second-secret',
        })
        self.assertEqual(first.worker_max_concurrency, 7)
        self.assertTrue(first.worker_capability_routing)
        sanitized = str(first.sanitized_configuration())
        self.assertNotIn('user:secret', sanitized)
        self.assertNotIn('first-secret', sanitized)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)

    def test_invalid_typed_value_fails_at_startup(self):
        with self.assertRaisesRegex(SettingsError, 'WORKER_MAX_CONCURRENCY'):
            PlatformSettings.from_env({'WORKER_MAX_CONCURRENCY': 'many'})

    def test_production_rejects_local_storage_and_legacy_token(self):
        settings = PlatformSettings.from_env({
            'APP_ENV': 'production',
            'STORAGE_BACKEND': 'local',
            'CADD_API_TOKEN': 'legacy',
        })
        with self.assertRaisesRegex(SettingsError, 'CADD_API_TOKEN'):
            settings.validate('api')

    def test_production_accepts_s3_without_exposing_credentials(self):
        settings = PlatformSettings.from_env({
            'APP_ENV': 'production',
            'STORAGE_BACKEND': 's3',
            'S3_BUCKET': 'research-data',
            'AWS_ACCESS_KEY_ID': 'key',
            'AWS_SECRET_ACCESS_KEY': 'hidden',
        }).validate('api')
        self.assertEqual(settings.public_snapshot()['environment'], 'production')
        self.assertNotIn('hidden', str(settings.public_snapshot()))


if __name__ == '__main__':
    unittest.main()
