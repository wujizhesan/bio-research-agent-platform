import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from src.auth import AuthService, hash_password


class AuthConfigurationTests(unittest.TestCase):
    def test_production_rejects_legacy_token(self):
        environment = {
            'APP_ENV': 'production',
            'CADD_API_TOKEN': 'legacy',
            'CADD_JWT_SECRET': 's' * 32,
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, 'forbidden in production'):
                AuthService.from_env()

    def test_production_requires_jwt_secret(self):
        with patch.dict(os.environ, {'APP_ENV': 'production'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'JWT secret is required'):
                AuthService.from_env()

    def test_production_reads_external_secret_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            secret_path = root / 'jwt-secret'
            users_path = root / 'users.json'
            secret_path.write_text('s' * 32, encoding='utf-8')
            users_path.write_text(json.dumps({
                'alice': {
                    'password_hash': hash_password('secret'),
                    'roles': ['researcher'],
                },
            }), encoding='utf-8')
            environment = {
                'APP_ENV': 'production',
                'CADD_JWT_SECRET_FILE': str(secret_path),
                'CADD_AUTH_USERS_FILE': str(users_path),
            }
            with patch.dict(os.environ, environment, clear=True):
                service = AuthService.from_env()
                token = service.issue_token('alice', 'secret')
        self.assertEqual(token['principal']['auth_type'], 'jwt')
        self.assertIsNone(service.legacy_token)

    def test_production_rejects_plaintext_user_passwords(self):
        environment = {
            'APP_ENV': 'production',
            'CADD_JWT_SECRET': 's' * 32,
            'CADD_AUTH_USERS_JSON': json.dumps({
                'alice': {'password': 'secret', 'roles': ['researcher']},
            }),
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, 'plaintext passwords'):
                AuthService.from_env()


if __name__ == '__main__':
    unittest.main()
