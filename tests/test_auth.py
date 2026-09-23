import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import jwt

from src.auth import (
    AuthService, LoginRateLimiter, Principal, RateLimiterUnavailable,
    hash_password, resolve_client_address,
)


class AuthConfigurationTests(unittest.TestCase):
    def test_only_admin_can_approve_indeterminate_jobs(self):
        service = AuthService()
        self.assertTrue(service.has_permission(
            Principal('admin-user', ('admin',), 'jwt'),
            'jobs:approve',
        ))
        self.assertFalse(service.has_permission(
            Principal('researcher-user', ('researcher',), 'jwt'),
            'jobs:approve',
        ))

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
            context_secret_path = root / 'context-secret'
            users_path = root / 'users.json'
            secret_path.write_text('s' * 32, encoding='utf-8')
            context_secret_path.write_text('c' * 32, encoding='utf-8')
            users_path.write_text(json.dumps({
                'alice': {
                    'password_hash': hash_password('secret'),
                    'roles': ['researcher'],
                },
            }), encoding='utf-8')
            environment = {
                'APP_ENV': 'production',
                'CADD_JWT_SECRET_FILE': str(secret_path),
                'RLS_CONTEXT_SIGNING_KEY_FILE': str(context_secret_path),
                'CADD_AUTH_USERS_FILE': str(users_path),
            }
            with patch.dict(os.environ, environment, clear=True):
                service = AuthService.from_env()
                token = service.issue_token('alice', 'secret')
        self.assertEqual(token['principal']['auth_type'], 'jwt')
        self.assertIsNone(service.legacy_token)
        self.assertEqual(service.session_cookie_name, '__Host-bioagent_session')
        self.assertEqual(service.csrf_cookie_name, '__Host-bioagent_csrf')

    def test_production_rejects_direct_auth_secrets(self):
        environment = {
            'APP_ENV': 'production',
            'CADD_JWT_SECRET': 's' * 32,
            'RLS_CONTEXT_SIGNING_KEY': 'c' * 32,
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, r'must use \*_FILE'):
                AuthService.from_env()

    def test_production_rejects_plaintext_user_passwords(self):
        environment = {
            'APP_ENV': 'production',
            'CADD_JWT_SECRET': 's' * 32,
            'RLS_CONTEXT_SIGNING_KEY': 'c' * 32,
            'CADD_AUTH_USERS_JSON': json.dumps({
                'alice': {'password': 'secret', 'roles': ['researcher']},
            }),
        }
        with patch.dict(os.environ, environment, clear=True):
            with self.assertRaisesRegex(ValueError, 'plaintext passwords'):
                AuthService.from_env()

    def test_issued_token_contains_revocable_session_claims(self):
        service = AuthService(
            jwt_secret='s' * 32,
            users={
                'alice': {
                    'password': 'secret',
                    'roles': ['researcher'],
                }
            },
        )
        issued = service.issue_token('alice', 'secret')
        claims = jwt.decode(
            issued['access_token'],
            's' * 32,
            algorithms=['HS256'],
            issuer='bio-research-agent',
        )
        self.assertGreaterEqual(len(claims['jti']), 20)
        self.assertEqual(claims['ver'], 0)
        principal = service.authenticate(f"Bearer {issued['access_token']}")
        self.assertEqual(principal.session_id, claims['jti'])

    def test_csrf_token_is_bound_to_cookie_session(self):
        service = AuthService(jwt_secret='s' * 32)
        principal = Principal(
            'alice',
            ('researcher',),
            'jwt_cookie',
            'session-alice-valid-001',
        )
        csrf_token = service.csrf_token(principal.session_id)

        self.assertTrue(service.validate_csrf(
            principal,
            csrf_token,
            csrf_token,
        ))
        self.assertFalse(service.validate_csrf(
            principal,
            csrf_token,
            'wrong-token',
        ))

    async def _redis_login_limiter_uses_hashed_scoped_keys(self):
        redis_client = AsyncMock()
        redis_client.eval.return_value = [1, 0]
        limiter = LoginRateLimiter(
            5,
            60,
            client_max_attempts=20,
            redis_client=redis_client,
            namespace='test',
            fail_closed=True,
        )
        self.assertTrue(await limiter.allow('203.0.113.10', 'alice@example.test'))
        arguments = redis_client.eval.call_args.args
        self.assertEqual(arguments[1], 2)
        self.assertNotIn('alice@example.test', ' '.join(map(str, arguments)))
        self.assertNotIn('203.0.113.10', ' '.join(map(str, arguments)))

    async def _production_login_limiter_fails_closed(self):
        redis_client = AsyncMock()
        redis_client.eval.side_effect = OSError('redis unavailable')
        limiter = LoginRateLimiter(
            redis_client=redis_client,
            fail_closed=True,
        )
        with self.assertRaises(RateLimiterUnavailable):
            await limiter.allow('203.0.113.10', 'alice')

    def test_redis_login_limiter_uses_hashed_scoped_keys(self):
        import asyncio
        asyncio.run(self._redis_login_limiter_uses_hashed_scoped_keys())

    def test_production_login_limiter_fails_closed(self):
        import asyncio
        asyncio.run(self._production_login_limiter_fails_closed())

    def test_redis_login_limiter_has_short_operation_timeout(self):
        import asyncio

        async def exercise():
            redis_client = AsyncMock()

            async def slow_eval(*_args):
                await asyncio.sleep(1)

            redis_client.eval.side_effect = slow_eval
            limiter = LoginRateLimiter(
                redis_client=redis_client,
                fail_closed=True,
                operation_timeout_seconds=0.05,
            )
            with self.assertRaises(RateLimiterUnavailable):
                await limiter.allow('203.0.113.10', 'alice')

        asyncio.run(exercise())

    def test_client_address_only_trusts_configured_proxy_chain(self):
        trusted = ('172.16.0.0/12', '127.0.0.1/32')
        self.assertEqual(
            resolve_client_address(
                '172.20.0.4',
                '198.51.100.7, 127.0.0.1',
                trusted,
            ),
            '198.51.100.7',
        )
        self.assertEqual(
            resolve_client_address('203.0.113.9', '198.51.100.7', trusted),
            '203.0.113.9',
        )


if __name__ == '__main__':
    unittest.main()
