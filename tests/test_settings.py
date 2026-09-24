import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from src.settings import PlatformSettings, SettingsError


class PlatformSettingsTests(unittest.TestCase):
    def test_parses_typed_values_and_builds_sanitized_fingerprint(self):
        base = {
            'REDIS_URL': 'redis://user:secret@redis:6379/3',
            'DATABASE_URL': 'postgresql://user:secret@db:5432/bioagent',
            'WORKER_MAX_CONCURRENCY': '7',
            'WORKER_LIGHT_RESERVED_SLOTS': '3',
            'WORKER_CAPABILITY_ROUTING': 'true',
            'RESEARCH_PLANNER_API_KEY': 'first-secret',
        }
        first = PlatformSettings.from_env(base)
        second = PlatformSettings.from_env({
            **base,
            'RESEARCH_PLANNER_API_KEY': 'second-secret',
        })
        self.assertEqual(first.worker_max_concurrency, 7)
        self.assertEqual(first.worker_light_reserved_slots, 3)
        self.assertTrue(first.worker_capability_routing)
        sanitized = str(first.sanitized_configuration())
        self.assertNotIn('user:secret', sanitized)
        self.assertNotIn('first-secret', sanitized)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(len(first.fingerprint), 64)

    def test_invalid_typed_value_fails_at_startup(self):
        with self.assertRaisesRegex(SettingsError, 'WORKER_MAX_CONCURRENCY'):
            PlatformSettings.from_env({'WORKER_MAX_CONCURRENCY': 'many'})
        with self.assertRaisesRegex(SettingsError, 'WORKER_LIGHT_RESERVED_SLOTS'):
            PlatformSettings.from_env({'WORKER_LIGHT_RESERVED_SLOTS': 'many'})
        self.assertEqual(
            PlatformSettings.from_env({'WORKER_LIGHT_RESERVED_SLOTS': '-1'}).worker_light_reserved_slots,
            0,
        )
        with self.assertRaisesRegex(SettingsError, 'WORKER_CAPABILITY_ROUTING'):
            PlatformSettings.from_env({'WORKER_LIGHT_RESERVED_SLOTS': '1'}).validate()

    def test_metrics_scrape_token_loads_only_from_secret_file(self):
        with tempfile.TemporaryDirectory(prefix='metrics_secret_') as raw:
            secret_path = Path(raw) / 'metrics-token'
            secret_path.write_text('m' * 40, encoding='utf-8')
            settings = PlatformSettings.from_env({
                'METRICS_SCRAPE_TOKEN_FILE': str(secret_path),
            })
        self.assertEqual(settings.metrics_scrape_token, 'm' * 40)
        self.assertNotIn('m' * 40, str(settings.sanitized_configuration()))

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
            'PUBLIC_BASE_URL': 'https://platform.example',
            'CORS_ORIGINS': 'https://platform.example',
            'TRUSTED_PROXY_CIDRS': '172.16.0.0/12',
            'JOB_BACKEND': 'redis',
            'STORAGE_BACKEND': 's3',
            'S3_BUCKET': 'research-data',
            'DATABASE_ROLE': 'api',
        }).validate('api')
        self.assertEqual(settings.public_snapshot()['environment'], 'production')
        self.assertFalse(settings.allow_legacy_artifact_paths)
        self.assertFalse(settings.sanitized_configuration()['secrets_configured']['aws'])

    def test_production_rejects_legacy_artifact_paths(self):
        settings = PlatformSettings.from_env({
            'APP_ENV': 'production',
            'ALLOW_LEGACY_ARTIFACT_PATHS': 'true',
        })
        with self.assertRaisesRegex(
            SettingsError,
            'ALLOW_LEGACY_ARTIFACT_PATHS',
        ):
            settings.validate('api')

    def test_production_requires_component_database_role(self):
        base = {
            'APP_ENV': 'production',
            'PUBLIC_BASE_URL': 'https://platform.example',
            'CORS_ORIGINS': 'https://platform.example',
            'JOB_BACKEND': 'redis',
            'STORAGE_BACKEND': 's3',
            'S3_BUCKET': 'research-data',
        }
        with self.assertRaisesRegex(SettingsError, 'DATABASE_ROLE=api'):
            PlatformSettings.from_env(base).validate('api')
        with self.assertRaisesRegex(SettingsError, 'DATABASE_ROLE=worker'):
            PlatformSettings.from_env({**base, 'DATABASE_ROLE': 'api'}).validate('worker')
        with self.assertRaisesRegex(SettingsError, 'DATABASE_ROLE=dispatcher'):
            PlatformSettings.from_env({**base, 'DATABASE_ROLE': 'api'}).validate(
                'dispatcher'
            )
        with self.assertRaisesRegex(SettingsError, 'DATABASE_ROLE=maintenance'):
            PlatformSettings.from_env({**base, 'DATABASE_ROLE': 'worker'}).validate(
                'maintenance'
            )
        maintenance = PlatformSettings.from_env({
            **base,
            'DATABASE_ROLE': 'maintenance',
            'JOB_BACKEND': 'local',
        }).validate('maintenance')
        self.assertEqual(maintenance.database_role, 'maintenance')

    def test_production_rejects_local_job_backend(self):
        settings = PlatformSettings.from_env({
            'APP_ENV': 'production',
            'JOB_BACKEND': 'local',
            'STORAGE_BACKEND': 's3',
            'S3_BUCKET': 'research-data',
        })
        with self.assertRaisesRegex(SettingsError, 'JOB_BACKEND=redis'):
            settings.validate('api')

    def test_production_requires_https_public_origin(self):
        base = {
            'APP_ENV': 'production',
            'PUBLIC_BASE_URL': 'http://platform.example',
            'CORS_ORIGINS': 'https://platform.example',
        }
        with self.assertRaisesRegex(SettingsError, 'PUBLIC_BASE_URL'):
            PlatformSettings.from_env(base).validate('api')

    def test_production_rejects_insecure_cors_origin(self):
        base = {
            'APP_ENV': 'production',
            'PUBLIC_BASE_URL': 'https://platform.example',
            'CORS_ORIGINS': 'http://platform.example',
        }
        with self.assertRaisesRegex(SettingsError, 'CORS_ORIGINS'):
            PlatformSettings.from_env(base).validate('api')

    def test_trusted_hosts_derive_from_public_origin(self):
        settings = PlatformSettings.from_env({
            'PUBLIC_BASE_URL': 'https://platform.example:8443',
        })
        self.assertEqual(
            settings.trusted_hosts,
            ('127.0.0.1', 'api', 'localhost', 'platform.example'),
        )


if __name__ == '__main__':
    unittest.main()
