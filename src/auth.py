"""JWT authentication and role-based authorization for the API."""

from dataclasses import dataclass
import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any

import jwt


ROLE_PERMISSIONS = {
    'admin': frozenset({
        'catalog:read', 'files:read', 'files:write', 'jobs:read', 'jobs:write',
        'metrics:read', 'plugins:write', 'runs:read',
    }),
    'researcher': frozenset({
        'catalog:read', 'files:read', 'files:write', 'jobs:read', 'jobs:write',
        'runs:read',
    }),
    'viewer': frozenset({'catalog:read', 'files:read', 'jobs:read', 'runs:read'}),
}


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: tuple[str, ...]
    auth_type: str

    def as_dict(self) -> dict[str, Any]:
        return {'sub': self.subject, 'roles': list(self.roles), 'auth_type': self.auth_type}


def hash_password(password: str, iterations: int = 310000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    encode = lambda value: base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')
    return f'pbkdf2_sha256${iterations}${encode(salt)}${encode(digest)}'


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_value, digest_value = stored.split('$', 3)
        if algorithm != 'pbkdf2_sha256':
            return False
        decode = lambda value: base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
        expected = decode(digest_value)
        actual = hashlib.pbkdf2_hmac(
            'sha256', password.encode('utf-8'), decode(salt_value), int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


class AuthService:
    def __init__(self, legacy_token=None, jwt_secret=None, users=None, ttl_seconds=3600, issuer='bio-research-agent'):
        self.legacy_token = legacy_token or None
        self.jwt_secret = jwt_secret or None
        if self.jwt_secret and len(self.jwt_secret) < 32:
            raise ValueError('CADD_JWT_SECRET must be at least 32 characters')
        self.users = users or {}
        self.ttl_seconds = max(int(ttl_seconds), 60)
        self.issuer = issuer

    @classmethod
    def from_env(cls):
        raw_users = os.environ.get('CADD_AUTH_USERS_JSON', '')
        users = json.loads(raw_users) if raw_users else {}
        if not isinstance(users, dict):
            raise ValueError('CADD_AUTH_USERS_JSON must be a JSON object')
        return cls(
            legacy_token=os.environ.get('CADD_API_TOKEN'),
            jwt_secret=os.environ.get('CADD_JWT_SECRET'),
            users=users,
            ttl_seconds=int(os.environ.get('AUTH_TOKEN_TTL_SECONDS', '3600')),
            issuer=os.environ.get('CADD_JWT_ISSUER', 'bio-research-agent'),
        )

    @property
    def enabled(self):
        return bool(self.legacy_token or self.jwt_secret)

    def authenticate(self, authorization: str | None) -> Principal:
        if not self.enabled:
            return Principal('local-dev', ('admin',), 'development')
        scheme, _, supplied = (authorization or '').partition(' ')
        if scheme.lower() != 'bearer' or not supplied.strip():
            raise AuthenticationError('authentication required')
        token = supplied.strip()
        if self.legacy_token and hmac.compare_digest(token, self.legacy_token):
            return Principal('legacy-token', ('admin',), 'legacy_token')
        if not self.jwt_secret:
            raise AuthenticationError('invalid bearer token')
        try:
            payload = jwt.decode(
                token,
                self.jwt_secret,
                algorithms=['HS256'],
                issuer=self.issuer,
                options={'require': ['exp', 'iat', 'iss', 'sub']},
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError('invalid bearer token') from exc
        roles = payload.get('roles', [])
        if not isinstance(roles, list) or not roles or any(role not in ROLE_PERMISSIONS for role in roles):
            raise AuthenticationError('token has invalid roles')
        return Principal(str(payload['sub']), tuple(roles), 'jwt')

    def has_permission(self, principal: Principal, permission: str) -> bool:
        permissions = set()
        for role in principal.roles:
            permissions.update(ROLE_PERMISSIONS.get(role, ()))
        return permission in permissions

    def issue_token(self, username: str, password: str) -> dict[str, Any]:
        if not self.jwt_secret:
            raise AuthenticationError('JWT authentication is not configured')
        record = self.users.get(username)
        if not isinstance(record, dict):
            raise AuthenticationError('invalid username or password')
        password_hash = record.get('password_hash')
        password_matches = _verify_password(password, password_hash) if password_hash else (
            isinstance(record.get('password'), str) and hmac.compare_digest(password, record['password'])
        )
        if not password_matches:
            raise AuthenticationError('invalid username or password')
        roles = tuple(record.get('roles', ['researcher']))
        if not roles or any(role not in ROLE_PERMISSIONS for role in roles):
            raise AuthenticationError('user has invalid roles')
        now = int(time.time())
        payload = {
            'sub': username,
            'roles': list(roles),
            'iat': now,
            'exp': now + self.ttl_seconds,
            'iss': self.issuer,
        }
        return {
            'access_token': jwt.encode(payload, self.jwt_secret, algorithm='HS256'),
            'token_type': 'bearer',
            'expires_in': self.ttl_seconds,
            'principal': Principal(username, roles, 'jwt').as_dict(),
        }
