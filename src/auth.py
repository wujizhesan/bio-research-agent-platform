"""JWT authentication and role-based authorization for the API."""

from dataclasses import dataclass
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import time
from ipaddress import ip_address, ip_network
from threading import Lock
from typing import Any
from inspect import isawaitable
from secrets import token_urlsafe

import jwt


ROLE_PERMISSIONS = {
    'admin': frozenset({
        'auth:revoke',
        'catalog:read', 'files:read', 'files:write', 'jobs:read', 'jobs:write',
        'jobs:approve',
        'metrics:read', 'members:write', 'plugins:write', 'projects:read',
        'projects:write', 'runs:read', 'telemetry:write',
    }),
    'researcher': frozenset({
        'catalog:read', 'files:read', 'files:write', 'jobs:read', 'jobs:write',
        'members:write', 'projects:read', 'projects:write', 'runs:read',
        'telemetry:write',
    }),
    'viewer': frozenset({'catalog:read', 'files:read', 'jobs:read', 'projects:read', 'runs:read', 'telemetry:write'}),
    'monitoring': frozenset({'metrics:read'}),
}


class AuthenticationError(ValueError):
    pass


class RateLimiterUnavailable(RuntimeError):
    pass


def _read_secret(name: str, file_name: str) -> str | None:
    direct = os.environ.get(name, '').strip()
    path_value = os.environ.get(file_name, '').strip()
    if direct and path_value:
        raise ValueError(f'configure only one of {name} or {file_name}')
    if not path_value:
        return direct or None
    path = os.path.abspath(path_value)
    try:
        with open(path, 'rb') as handle:
            value = handle.read(65537)
    except OSError as exc:
        raise ValueError(f'unable to read {file_name}') from exc
    if len(value) > 65536:
        raise ValueError(f'{file_name} exceeds 65536 bytes')
    expected_digest = os.environ.get(f'{name}_SHA256', '').strip()
    if expected_digest:
        if re.fullmatch(r'[0-9a-f]{64}', expected_digest) is None or not hmac.compare_digest(
            hashlib.sha256(value).hexdigest(), expected_digest,
        ):
            raise ValueError(f'{file_name} checksum mismatch')
    return value.decode('utf-8').strip() or None


class LoginRateLimiter:
    _CONSUME_SCRIPT = """
local allowed = 1
local retry_after = 0
for index, key in ipairs(KEYS) do
    local count = redis.call('INCR', key)
    if count == 1 then
        redis.call('PEXPIRE', key, ARGV[1])
    end
    local ttl = redis.call('PTTL', key)
    if count > tonumber(ARGV[index + 1]) then
        allowed = 0
        if ttl > retry_after then retry_after = ttl end
    end
end
return {allowed, retry_after}
"""

    def __init__(
        self,
        max_attempts=5,
        window_seconds=60,
        *,
        client_max_attempts=None,
        redis_client=None,
        namespace='bioagent',
        fail_closed=False,
        operation_timeout_seconds=0.5,
    ):
        self.max_attempts = max(int(max_attempts), 1)
        self.client_max_attempts = max(
            int(client_max_attempts or self.max_attempts), 1
        )
        self.window_seconds = max(int(window_seconds), 1)
        self.redis_client = redis_client
        self.namespace = str(namespace).strip() or 'bioagent'
        self.fail_closed = bool(fail_closed)
        self.operation_timeout_seconds = min(
            max(float(operation_timeout_seconds), 0.05),
            5.0,
        )
        self._attempts = {}
        self._lock = Lock()

    @classmethod
    def from_env(cls, *, redis_url=None, namespace=None, production=None):
        production = (
            os.environ.get('APP_ENV', 'development').strip().lower()
            in {'production', 'prod'}
            if production is None else bool(production)
        )
        redis_client = None
        if redis_url:
            try:
                import redis.asyncio as redis
                timeout = min(max(float(os.environ.get(
                    'AUTH_RATE_LIMIT_REDIS_TIMEOUT_SECONDS', '0.5'
                )), 0.05), 5.0)
                redis_client = redis.Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_timeout=timeout,
                    socket_connect_timeout=timeout,
                )
            except (ImportError, TypeError, ValueError) as exc:
                if production:
                    raise ValueError(
                        'production login rate limiting requires Redis'
                    ) from exc
        return cls(
            max_attempts=os.environ.get('AUTH_LOGIN_RATE_LIMIT', '5'),
            client_max_attempts=os.environ.get(
                'AUTH_LOGIN_CLIENT_RATE_LIMIT', '20'
            ),
            window_seconds=os.environ.get('AUTH_LOGIN_RATE_WINDOW_SECONDS', '60'),
            redis_client=redis_client,
            namespace=namespace or os.environ.get('REDIS_NAMESPACE', 'bioagent'),
            fail_closed=production,
            operation_timeout_seconds=os.environ.get(
                'AUTH_RATE_LIMIT_REDIS_TIMEOUT_SECONDS', '0.5'
            ),
        )

    def _redis_key(self, scope, value):
        digest = hashlib.sha256(str(value).encode('utf-8')).hexdigest()
        return f'{self.namespace}:auth:login:{scope}:{digest}'

    async def allow(self, key, account_key=None):
        if self.redis_client is not None:
            keys = [self._redis_key('client', key)]
            limits = [self.client_max_attempts]
            if account_key is not None:
                keys.append(self._redis_key('account', account_key))
                limits.append(self.max_attempts)
            try:
                result = await asyncio.wait_for(
                    self.redis_client.eval(
                        self._CONSUME_SCRIPT,
                        len(keys),
                        *keys,
                        self.window_seconds * 1000,
                        *limits,
                    ),
                    timeout=self.operation_timeout_seconds,
                )
                return bool(int(result[0]))
            except Exception as exc:
                if self.fail_closed:
                    raise RateLimiterUnavailable(
                        'login rate limiter is unavailable'
                    ) from exc
        now = time.monotonic()
        cutoff = now - self.window_seconds
        memory_key = (key, account_key)
        with self._lock:
            attempts = [
                value for value in self._attempts.get(memory_key, [])
                if value > cutoff
            ]
            if len(attempts) >= self.max_attempts:
                self._attempts[memory_key] = attempts
                return False
            attempts.append(now)
            self._attempts[memory_key] = attempts
            return True

    async def reset(self, key, account_key=None):
        if self.redis_client is not None:
            keys = []
            if account_key is None:
                keys.append(self._redis_key('client', key))
            else:
                keys.append(self._redis_key('account', account_key))
            try:
                await asyncio.wait_for(
                    self.redis_client.delete(*keys),
                    timeout=self.operation_timeout_seconds,
                )
                return
            except Exception as exc:
                if self.fail_closed:
                    raise RateLimiterUnavailable(
                        'login rate limiter is unavailable'
                    ) from exc
        with self._lock:
            self._attempts.pop((key, account_key), None)

    async def close(self):
        if self.redis_client is None:
            return
        closer = getattr(self.redis_client, 'aclose', None)
        if closer is None:
            closer = getattr(self.redis_client, 'close', None)
        if closer is None:
            return
        result = closer()
        if isawaitable(result):
            await result


def resolve_client_address(peer_host, forwarded_for, trusted_proxy_cidrs):
    peer = str(peer_host or '').strip()
    try:
        current = ip_address(peer)
        trusted = tuple(ip_network(value, strict=False) for value in trusted_proxy_cidrs)
    except ValueError:
        return peer or 'unknown'
    if not any(current in network for network in trusted):
        return str(current)
    chain = [part.strip() for part in str(forwarded_for or '').split(',') if part.strip()]
    chain.append(str(current))
    for candidate in reversed(chain):
        try:
            address = ip_address(candidate)
        except ValueError:
            continue
        if any(address in network for network in trusted):
            continue
        return str(address)
    return str(current)


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: tuple[str, ...]
    auth_type: str
    session_id: str = ''
    token_version: int = 0
    key_id: str = ''

    def as_dict(self) -> dict[str, Any]:
        return {'sub': self.subject, 'roles': list(self.roles), 'auth_type': self.auth_type}


def roles_sha256(roles) -> str:
    encoded = json.dumps(
        sorted(str(role) for role in roles),
        ensure_ascii=True,
        separators=(',', ':'),
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def hash_password(password: str, iterations: int = 310000) -> str:
    salt = os.urandom(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode('utf-8'), salt, iterations)
    def encode(value):
        return base64.urlsafe_b64encode(value).decode('ascii').rstrip('=')

    return f'pbkdf2_sha256${iterations}${encode(salt)}${encode(digest)}'


def _verify_password(password: str, stored: str) -> bool:
    try:
        algorithm, iterations, salt_value, digest_value = stored.split('$', 3)
        if algorithm != 'pbkdf2_sha256':
            return False
        def decode(value):
            return base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))

        expected = decode(digest_value)
        actual = hashlib.pbkdf2_hmac(
            'sha256', password.encode('utf-8'), decode(salt_value), int(iterations),
        )
        return hmac.compare_digest(actual, expected)
    except (TypeError, ValueError):
        return False


class AuthService:
    def __init__(
        self,
        legacy_token=None,
        jwt_secret=None,
        users=None,
        ttl_seconds=3600,
        issuer='bio-research-agent',
        production=False,
        jwt_keys=None,
        jwt_key_id='primary',
        jwt_retire_at=None,
    ):
        self.legacy_token = legacy_token or None
        if jwt_keys is not None and jwt_secret:
            raise ValueError('configure JWT key ring or one secret, not both')
        if not isinstance(jwt_key_id, str):
            raise ValueError('JWT active kid must be a string')
        if jwt_keys is not None and not isinstance(jwt_keys, dict):
            raise ValueError('JWT key ring keys must be an object')
        if jwt_retire_at is not None and not isinstance(jwt_retire_at, dict):
            raise ValueError('JWT key retirement deadlines must be an object')
        self.jwt_key_id = jwt_key_id
        self.jwt_keys = dict(jwt_keys) if jwt_keys is not None else (
            {self.jwt_key_id: jwt_secret} if jwt_secret else {}
        )
        self.jwt_retire_at = dict(jwt_retire_at or {})
        if (
            (jwt_keys is not None and not self.jwt_keys)
            or (self.jwt_keys and self.jwt_key_id not in self.jwt_keys)
            or len(self.jwt_keys) > 2
            or any(
                not isinstance(key_id, str)
                or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}', key_id) is None
                or not isinstance(secret, str)
                or len(secret) < 32
                for key_id, secret in self.jwt_keys.items()
            )
            or len(set(self.jwt_keys.values())) != len(self.jwt_keys)
            or set(self.jwt_retire_at) != (set(self.jwt_keys) - {self.jwt_key_id})
            or any(
                type(deadline) is not int
                or deadline < 1
                or deadline > int(time.time()) + 86400
                for deadline in self.jwt_retire_at.values()
            )
        ):
            raise ValueError('JWT key ring requires distinct strong keys and bounded retirement deadlines')
        self.jwt_secret = self.jwt_keys.get(self.jwt_key_id)
        self.users = users or {}
        try:
            self.ttl_seconds = int(ttl_seconds)
        except (TypeError, ValueError) as exc:
            raise ValueError('AUTH_TOKEN_TTL_SECONDS must be between 60 and 86400') from exc
        if not 60 <= self.ttl_seconds <= 86400:
            raise ValueError('AUTH_TOKEN_TTL_SECONDS must be between 60 and 86400')
        self.issuer = issuer
        self.production = bool(production)
        self.session_cookie_name = (
            '__Host-bioagent_session'
            if self.production else 'bioagent_session'
        )
        self.csrf_cookie_name = (
            '__Host-bioagent_csrf'
            if self.production else 'bioagent_csrf'
        )

    @classmethod
    def from_env(cls):
        production = os.environ.get('APP_ENV', 'development').strip().lower() in {
            'production', 'prod'
        }
        legacy_token = os.environ.get('CADD_API_TOKEN', '').strip() or None
        if production and legacy_token:
            raise ValueError('CADD_API_TOKEN is forbidden in production')
        jwt_value = _read_secret('CADD_JWT_SECRET', 'CADD_JWT_SECRET_FILE')
        if production and not jwt_value:
            raise ValueError('JWT secret is required in production')
        jwt_keys = None
        jwt_key_id = 'primary'
        jwt_retire_at = None
        if jwt_value and jwt_value.startswith('{'):
            try:
                key_ring = json.loads(jwt_value)
            except json.JSONDecodeError as exc:
                raise ValueError('invalid JWT key ring JSON') from exc
            if not isinstance(key_ring, dict) or not {'active_kid', 'keys'} <= set(key_ring) or set(key_ring) - {'active_kid', 'keys', 'retire_at'}:
                raise ValueError('JWT key ring requires active_kid, keys and optional retire_at')
            jwt_key_id = key_ring['active_kid']
            jwt_keys = key_ring['keys']
            jwt_retire_at = key_ring.get('retire_at')
            if not isinstance(jwt_keys, dict):
                raise ValueError('JWT key ring keys must be an object')
        context_secret = _read_secret(
            'RLS_CONTEXT_SIGNING_KEY',
            'RLS_CONTEXT_SIGNING_KEY_FILE',
        )
        if production and (
            not context_secret or len(context_secret) < 32
        ):
            raise ValueError(
                'RLS context signing key of at least 32 characters is required '
                'in production'
            )
        raw_users = _read_secret(
            'CADD_AUTH_USERS_JSON', 'CADD_AUTH_USERS_FILE'
        ) or ''
        users = json.loads(raw_users) if raw_users else {}
        if not isinstance(users, dict):
            raise ValueError('CADD_AUTH_USERS_JSON must be a JSON object')
        if production and any(
            isinstance(record, dict) and record.get('password')
            for record in users.values()
        ):
            raise ValueError('plaintext passwords are forbidden in production')
        if production:
            direct_secrets = (
                'CADD_JWT_SECRET', 'RLS_CONTEXT_SIGNING_KEY',
                'CADD_AUTH_USERS_JSON',
            )
            configured = [
                name for name in direct_secrets
                if os.environ.get(name, '').strip()
            ]
            if configured:
                raise ValueError(
                    'production secrets must use *_FILE: ' + ', '.join(configured)
                )
        return cls(
            legacy_token=None if production else legacy_token,
            jwt_secret=jwt_value if jwt_keys is None else None,
            jwt_keys=jwt_keys,
            jwt_key_id=jwt_key_id,
            jwt_retire_at=jwt_retire_at,
            users=users,
            ttl_seconds=int(os.environ.get('AUTH_TOKEN_TTL_SECONDS', '3600')),
            issuer=os.environ.get('CADD_JWT_ISSUER', 'bio-research-agent'),
            production=production,
        )

    @property
    def enabled(self):
        return bool(self.legacy_token or self.jwt_secret)

    def sign_jwt(self, payload):
        if not self.jwt_secret:
            raise AuthenticationError('JWT authentication is not configured')
        return jwt.encode(
            payload,
            self.jwt_secret,
            algorithm='HS256',
            headers={'kid': self.jwt_key_id},
        )

    def decode_jwt(self, token, *, issuer, required):
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'HS256':
            raise jwt.InvalidAlgorithmError('invalid token algorithm')
        key_id = header.get('kid')
        if key_id is not None:
            if not isinstance(key_id, str) or key_id not in self.jwt_keys:
                raise jwt.InvalidTokenError('unknown token key ID')
            candidates = ((key_id, self.jwt_keys[key_id]),)
        else:
            candidates = tuple(self.jwt_keys.items())
        now = int(time.time())
        candidates = tuple(
            (candidate_id, secret)
            for candidate_id, secret in candidates
            if self.jwt_retire_at.get(candidate_id, now + 1) > now
        )
        last_error = None
        for candidate_id, secret in candidates:
            try:
                payload = jwt.decode(
                    token,
                    secret,
                    algorithms=['HS256'],
                    issuer=issuer,
                    options={'require': required},
                )
                return payload, candidate_id
            except jwt.PyJWTError as exc:
                last_error = exc
        raise jwt.InvalidTokenError('invalid token signature or claims') from last_error

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
            payload, key_id = self.decode_jwt(
                token,
                issuer=self.issuer,
                required=['exp', 'iat', 'iss', 'sub', 'jti', 'ver'],
            )
        except jwt.PyJWTError as exc:
            raise AuthenticationError('invalid bearer token') from exc
        roles = payload.get('roles', [])
        if not isinstance(roles, list) or not roles or any(role not in ROLE_PERMISSIONS for role in roles):
            raise AuthenticationError('token has invalid roles')
        jti = payload.get('jti')
        version = payload.get('ver')
        if (
            not isinstance(jti, str)
            or not 20 <= len(jti) <= 80
            or not isinstance(version, int)
            or version < 0
        ):
            raise AuthenticationError('token has invalid session claims')
        return Principal(
            str(payload['sub']),
            tuple(roles),
            'jwt',
            jti,
            version,
            key_id,
        )

    def has_permission(self, principal: Principal, permission: str) -> bool:
        permissions = set()
        for role in principal.roles:
            permissions.update(ROLE_PERMISSIONS.get(role, ()))
        return permission in permissions

    def csrf_token(self, session_id: str, key_id: str = '') -> str:
        secret = self.jwt_keys.get(key_id or self.jwt_key_id)
        if not secret or not session_id:
            raise AuthenticationError('CSRF protection is unavailable')
        return hmac.new(
            secret.encode('utf-8'),
            f'bioagent-csrf-v1:{session_id}'.encode('utf-8'),
            hashlib.sha256,
        ).hexdigest()

    def validate_csrf(self, principal, cookie_token, header_token) -> bool:
        if principal.auth_type != 'jwt_cookie':
            return True
        if not cookie_token or not header_token:
            return False
        expected = self.csrf_token(principal.session_id, principal.key_id)
        return (
            hmac.compare_digest(str(cookie_token), expected)
            and hmac.compare_digest(str(header_token), expected)
        )

    def authenticate_credentials(self, username: str, password: str) -> Principal:
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
        return Principal(username, roles, 'jwt')

    def issue_token_for_principal(self, principal, session) -> dict[str, Any]:
        if not self.jwt_secret:
            raise AuthenticationError('JWT authentication is not configured')
        if str(session.get('subject') or '') != principal.subject:
            raise AuthenticationError('authentication session subject mismatch')
        issued_at = int(session['issued_at'])
        expires_at = int(session['expires_at'])
        session_id = str(session['jti'])
        token_version = int(session['token_version'])
        payload = {
            'sub': principal.subject,
            'roles': list(principal.roles),
            'iat': issued_at,
            'exp': expires_at,
            'iss': self.issuer,
            'jti': session_id,
            'ver': token_version,
        }
        return {
            'access_token': self.sign_jwt(payload),
            'token_type': 'bearer',
            'expires_in': max(expires_at - issued_at, 0),
            'principal': Principal(
                principal.subject,
                principal.roles,
                'jwt',
                session_id,
                token_version,
                self.jwt_key_id,
            ).as_dict(),
        }

    def issue_token(self, username: str, password: str) -> dict[str, Any]:
        principal = self.authenticate_credentials(username, password)
        now = int(time.time())
        return self.issue_token_for_principal(principal, {
            'jti': token_urlsafe(32),
            'subject': principal.subject,
            'token_version': 0,
            'issued_at': now,
            'expires_at': now + self.ttl_seconds,
        })
