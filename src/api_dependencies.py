"""Request-scoped authentication and access dependencies for the HTTP API."""

import os
import secrets
from time import time

import jwt
from fastapi import Depends, HTTPException, Query, Request, status
from fastapi.security import OAuth2PasswordBearer

try:
    from .auth import AuthenticationError, Principal, roles_sha256
    from .database import set_database_principal
except ImportError:
    from auth import AuthenticationError, Principal, roles_sha256
    from database import set_database_principal


oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl='/api/v1/auth/token',
    auto_error=False,
)


class ApiDependencies:
    def __init__(self, auth, database, metrics_scrape_token=''):
        self.auth = auth
        self.database = database
        self.metrics_scrape_token = str(metrics_scrape_token or '')
        self.stream_ticket_secret = auth.jwt_secret or secrets.token_urlsafe(32)
        self.stream_ticket_issuer = f'{auth.issuer}:sse'
        try:
            configured_ttl = int(os.environ.get('SSE_TICKET_TTL_SECONDS', '60'))
        except ValueError:
            configured_ttl = 60
        self.stream_ticket_ttl = max(min(configured_ttl, 300), 10)

    async def _validate_session(self, principal):
        if principal.auth_type not in {'jwt', 'jwt_cookie', 'sse_ticket'}:
            return
        if principal.auth_type == 'sse_ticket' and not principal.session_id:
            return
        validator = getattr(self.database, 'validate_auth_session', None)
        if validator is None or not principal.session_id:
            raise AuthenticationError('authentication session is unavailable')
        valid = await validator(
            principal.session_id,
            principal.subject,
            principal.token_version,
            roles_sha256(principal.roles),
        )
        if not valid:
            raise AuthenticationError('authentication session is revoked')

    async def current_principal(
        self,
        request: Request,
        token: str | None = Depends(oauth2_scheme),
    ):
        cookie_token = request.cookies.get(self.auth.session_cookie_name)
        selected_token = token or cookie_token
        authorization = f'Bearer {selected_token}' if selected_token else None
        try:
            principal = self.auth.authenticate(authorization)
            if not token and cookie_token and principal.auth_type == 'jwt':
                principal = Principal(
                    principal.subject,
                    principal.roles,
                    'jwt_cookie',
                    principal.session_id,
                    principal.token_version,
                    principal.key_id,
                )
            await self._validate_session(principal)
            if (
                request.method.upper() not in {'GET', 'HEAD', 'OPTIONS'}
                and not self.auth.validate_csrf(
                    principal,
                    request.cookies.get(self.auth.csrf_cookie_name),
                    request.headers.get('X-CSRF-Token'),
                )
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail='invalid CSRF token',
                )
            set_database_principal(principal)
            return principal
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={'WWW-Authenticate': 'Bearer'},
            ) from exc

    async def project_access(self, project_id, principal, allowed_roles):
        project = await self.database.get_project(project_id)
        if project is None:
            raise HTTPException(
                status_code=404,
                detail=f'project not found: {project_id}',
            )
        if (
            'admin' in principal.roles
            or project['owner_subject'] == principal.subject
        ):
            return project
        member = await self.database.get_project_member(
            project_id,
            principal.subject,
        )
        if member is None or member['role'] not in allowed_roles:
            raise HTTPException(status_code=403, detail='project access denied')
        return project

    async def job_access(self, job_id, principal, allowed_roles):
        project_id = await self.database.get_job_project(job_id)
        if not project_id:
            if 'admin' not in principal.roles:
                raise HTTPException(
                    status_code=403,
                    detail='unscoped job access denied',
                )
            return None
        await self.project_access(project_id, principal, allowed_roles)
        return project_id

    async def expose_job(self, record):
        if record is None:
            return None
        if record.get('project_id'):
            return record
        project_id = await self.database.get_job_project(record['job_id'])
        if not project_id:
            return record
        return {**record, 'project_id': project_id}

    def require_permission(self, permission):
        async def dependency(
            principal: Principal = Depends(self.current_principal),
        ):
            if not self.auth.has_permission(principal, permission):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail='insufficient permissions',
                )
            return principal

        return dependency

    async def metrics_principal(
        self,
        request: Request,
        token: str | None = Depends(oauth2_scheme),
    ):
        if (
            self.metrics_scrape_token
            and token
            and secrets.compare_digest(token, self.metrics_scrape_token)
        ):
            principal = Principal(
                'metrics-scraper',
                ('monitoring',),
                'metrics_token',
            )
            set_database_principal(
                Principal('metrics-scraper', ('admin',), 'metrics_token')
            )
            return principal
        principal = await self.current_principal(request, token)
        if not self.auth.has_permission(principal, 'metrics:read'):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='insufficient permissions',
            )
        return principal

    def issue_stream_ticket(self, job_id, principal):
        now = int(time())
        payload = {
            'sub': principal.subject,
            'roles': list(principal.roles),
            'job_id': job_id,
            'purpose': 'job-events',
            'iat': now,
            'exp': now + self.stream_ticket_ttl,
            'iss': self.stream_ticket_issuer,
            'sid': principal.session_id,
            'ver': principal.token_version,
        }
        if self.auth.jwt_secret:
            return self.auth.sign_jwt(payload)
        return jwt.encode(payload, self.stream_ticket_secret, algorithm='HS256')

    async def stream_principal(
        self,
        request: Request,
        job_id: str,
        ticket: str | None = Query(default=None, min_length=1),
        token: str | None = Depends(oauth2_scheme),
    ):
        if ticket:
            try:
                required = [
                    'exp', 'iat', 'iss', 'sub', 'job_id', 'purpose', 'sid', 'ver',
                ]
                if self.auth.jwt_secret:
                    payload, _ = self.auth.decode_jwt(
                        ticket,
                        issuer=self.stream_ticket_issuer,
                        required=required,
                    )
                else:
                    payload = jwt.decode(
                        ticket,
                        self.stream_ticket_secret,
                        algorithms=['HS256'],
                        issuer=self.stream_ticket_issuer,
                        options={'require': required},
                    )
            except jwt.PyJWTError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail='invalid stream ticket',
                ) from exc
            roles = payload.get('roles', [])
            valid_ticket = (
                payload.get('purpose') == 'job-events'
                and payload.get('job_id') == job_id
                and isinstance(roles, list)
            )
            if not valid_ticket:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail='invalid stream ticket',
                )
            principal = Principal(
                str(payload['sub']),
                tuple(roles),
                'sse_ticket',
                str(payload['sid']),
                int(payload['ver']),
            )
            try:
                await self._validate_session(principal)
            except AuthenticationError as exc:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail=str(exc),
                ) from exc
            set_database_principal(principal)
        else:
            principal = await self.current_principal(request, token)
        if not self.auth.has_permission(principal, 'jobs:read'):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='insufficient permissions',
            )
        return principal
