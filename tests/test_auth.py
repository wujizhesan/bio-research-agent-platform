import hashlib
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import jwt

from src.auth import (
    AuthenticationError, AuthService, LoginRateLimiter, Principal, RateLimiterUnavailable,
    hash_password, resolve_client_address,
)
from src.api_dependencies import ApiDependencies


class AuthConfigurationTests(unittest.TestCase):
    def test_token_ttl_must_match_database_session_bounds(self):
        for ttl in (59, 86401, 'invalid'):
            with self.subTest(ttl=ttl):
                with self.assertRaisesRegex(ValueError, 'AUTH_TOKEN_TTL_SECONDS'):
                    AuthService(ttl_seconds=ttl)
        self.assertEqual(AuthService(ttl_seconds=60).ttl_seconds, 60)
        self.assertEqual(AuthService(ttl_seconds=86400).ttl_seconds, 86400)

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
                'CADD_JWT_SECRET_SHA256': hashlib.sha256(
                    secret_path.read_bytes(),
                ).hexdigest(),
                'RLS_CONTEXT_SIGNING_KEY_FILE': str(context_secret_path),
                'RLS_CONTEXT_SIGNING_KEY_SHA256': hashlib.sha256(
                    context_secret_path.read_bytes(),
                ).hexdigest(),
                'CADD_AUTH_USERS_FILE': str(users_path),
            }
            with patch.dict(os.environ, environment, clear=True):
                service = AuthService.from_env()
                token = service.issue_token('alice', 'secret')
                secret_path.write_text('x' * 32, encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'checksum mismatch'):
                    AuthService.from_env()
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

    def test_key_ring_accepts_old_cookie_and_retires_old_tokens(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            key_path = root / 'jwt-keys'
            context_path = root / 'context-key'
            context_path.write_text('c' * 32, encoding='utf-8')
            key_ring = {
                'active_kid': '2026-09',
                'keys': {'2026-09': 'n' * 32, '2026-08': 'o' * 32},
                'retire_at': {'2026-08': int(time.time()) + 3600},
            }
            key_path.write_text(json.dumps(key_ring), encoding='utf-8')
            environment = {
                'APP_ENV': 'production',
                'CADD_JWT_SECRET_FILE': str(key_path),
                'RLS_CONTEXT_SIGNING_KEY_FILE': str(context_path),
            }
            with patch.dict(os.environ, environment, clear=True):
                service = AuthService.from_env()
                now = int(time.time())
                old_claims = {
                    'sub': 'alice', 'roles': ['researcher'],
                    'iat': now, 'exp': now + 3600,
                    'iss': service.issuer, 'jti': 's' * 32, 'ver': 0,
                }
                old_token = jwt.encode(old_claims, 'o' * 32, algorithm='HS256')
                principal = service.authenticate(f'Bearer {old_token}')
                self.assertEqual(principal.key_id, '2026-08')
                cookie_principal = Principal(
                    principal.subject, principal.roles, 'jwt_cookie',
                    principal.session_id, principal.token_version, principal.key_id,
                )
                old_csrf = service.csrf_token(principal.session_id, principal.key_id)
                self.assertTrue(service.validate_csrf(
                    cookie_principal, old_csrf, old_csrf,
                ))
                self.assertFalse(service.validate_csrf(
                    cookie_principal,
                    service.csrf_token(principal.session_id),
                    service.csrf_token(principal.session_id),
                ))
                new_token = service.issue_token_for_principal(
                    Principal('alice', ('researcher',), 'jwt'),
                    {
                        'subject': 'alice', 'jti': 't' * 32,
                        'token_version': 0, 'issued_at': now,
                        'expires_at': now + 3600,
                    },
                )['access_token']
                self.assertEqual(jwt.get_unverified_header(new_token)['kid'], '2026-09')
                self.assertEqual(
                    service.authenticate(f'Bearer {new_token}').key_id,
                    '2026-09',
                )
                unknown_key = jwt.encode(
                    old_claims, 'o' * 32, algorithm='HS256',
                    headers={'kid': 'unknown'},
                )
                with self.assertRaises(AuthenticationError):
                    service.authenticate(f'Bearer {unknown_key}')

                key_ring['keys'].pop('2026-08')
                key_ring['retire_at'].pop('2026-08')
                key_path.write_text(json.dumps(key_ring), encoding='utf-8')
                retired = AuthService.from_env()
                with self.assertRaises(AuthenticationError):
                    retired.authenticate(f'Bearer {old_token}')
                self.assertEqual(
                    retired.authenticate(f'Bearer {new_token}').subject,
                    'alice',
                )

    def test_key_ring_rejects_missing_or_reused_key_material(self):
        with self.assertRaisesRegex(ValueError, 'JWT key ring'):
            AuthService(jwt_keys={}, jwt_key_id='new')
        with self.assertRaisesRegex(ValueError, 'JWT key ring'):
            AuthService(
                jwt_keys={'new': 's' * 32, 'old': 's' * 32},
                jwt_key_id='new',
                jwt_retire_at={'old': int(time.time()) + 3600},
            )
        with self.assertRaisesRegex(ValueError, 'JWT key ring'):
            AuthService(
                jwt_keys={'new': 'n' * 32, 'old': 'o' * 32},
                jwt_key_id='new',
            )
        with self.assertRaisesRegex(ValueError, 'JWT key ring'):
            AuthService(
                jwt_keys={'new': 'n' * 32, 'old': 'o' * 32},
                jwt_key_id='new',
                jwt_retire_at={'old': int(time.time()) + 90000},
            )

    def test_expired_old_jwt_key_cannot_verify_access_or_sse_ticket(self):
        now = int(time.time())
        old = AuthService(jwt_secret='o' * 32, jwt_key_id='old')
        rotated = AuthService(
            jwt_keys={'new': 'n' * 32, 'old': 'o' * 32},
            jwt_key_id='new',
            jwt_retire_at={'old': now - 1},
        )
        token = jwt.encode({
            'sub': 'alice', 'roles': ['researcher'], 'iat': now,
            'exp': now + 3600, 'iss': old.issuer,
            'jti': 's' * 32, 'ver': 0,
        }, 'o' * 32, algorithm='HS256', headers={'kid': 'old'})
        with self.assertRaises(AuthenticationError):
            rotated.authenticate(f'Bearer {token}')
        legacy_token = jwt.encode({
            'sub': 'alice', 'roles': ['researcher'], 'iat': now,
            'exp': now + 3600, 'iss': old.issuer,
            'jti': 's' * 32, 'ver': 0,
        }, 'o' * 32, algorithm='HS256')
        with self.assertRaises(AuthenticationError):
            rotated.authenticate(f'Bearer {legacy_token}')
        ticket = ApiDependencies(old, AsyncMock()).issue_stream_ticket(
            'job-1', Principal('alice', ('researcher',), 'jwt', 's' * 32),
        )
        with self.assertRaises(jwt.InvalidTokenError):
            rotated.decode_jwt(
                ticket, issuer=f'{old.issuer}:sse',
                required=['exp', 'iat', 'iss', 'sub', 'job_id', 'purpose', 'sid', 'ver'],
            )

    def test_stream_ticket_survives_key_rotation(self):
        import asyncio
        from starlette.requests import Request

        old = AuthService(jwt_secret='o' * 32, jwt_key_id='old')
        rotated = AuthService(
            jwt_keys={'new': 'n' * 32, 'old': 'o' * 32},
            jwt_key_id='new',
            jwt_retire_at={'old': int(time.time()) + 3600},
        )
        database = AsyncMock()
        database.validate_auth_session.return_value = True
        old_api = ApiDependencies(old, database)
        rotated_api = ApiDependencies(rotated, database)
        ticket = old_api.issue_stream_ticket(
            'job-1',
            Principal('alice', ('researcher',), 'jwt', 's' * 32),
        )
        request = Request({
            'type': 'http', 'method': 'GET', 'path': '/api/v1/jobs/job-1/events',
            'headers': [], 'query_string': b'',
        })
        principal = asyncio.run(rotated_api.stream_principal(
            request, 'job-1', ticket=ticket, token=None,
        ))
        self.assertEqual(principal.subject, 'alice')
        self.assertEqual(principal.auth_type, 'sse_ticket')

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
