"""Request-scoped authentication and access dependencies for the HTTP API."""

import os
import secrets
from time import time

import jwt
from fastapi import Depends, HTTPException, Query, status
from fastapi.security import OAuth2PasswordBearer

try:
    from .auth import AuthenticationError, Principal
except ImportError:
    from auth import AuthenticationError, Principal


oauth2_scheme = OAuth2PasswordBearer(
    tokenUrl='/api/v1/auth/token',
    auto_error=False,
)


class ApiDependencies:
    def __init__(self, auth, database):
        self.auth = auth
        self.database = database
        self.stream_ticket_secret = auth.jwt_secret or secrets.token_urlsafe(32)
        self.stream_ticket_issuer = f'{auth.issuer}:sse'
        try:
            configured_ttl = int(os.environ.get('SSE_TICKET_TTL_SECONDS', '60'))
        except ValueError:
            configured_ttl = 60
        self.stream_ticket_ttl = max(min(configured_ttl, 300), 10)

    async def current_principal(
        self,
        token: str | None = Depends(oauth2_scheme),
    ):
        authorization = f'Bearer {token}' if token else None
        try:
            return self.auth.authenticate(authorization)
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
        if project_id:
            await self.project_access(project_id, principal, allowed_roles)
        return project_id

    async def expose_job(self, record):
        if record is None:
            return None
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
        }
        return jwt.encode(
            payload,
            self.stream_ticket_secret,
            algorithm='HS256',
        )

    async def stream_principal(
        self,
        job_id: str,
        ticket: str | None = Query(default=None, min_length=1),
        token: str | None = Depends(oauth2_scheme),
    ):
        if ticket:
            try:
                payload = jwt.decode(
                    ticket,
                    self.stream_ticket_secret,
                    algorithms=['HS256'],
                    issuer=self.stream_ticket_issuer,
                    options={
                        'require': [
                            'exp',
                            'iat',
                            'iss',
                            'sub',
                            'job_id',
                            'purpose',
                        ]
                    },
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
            )
        else:
            principal = await self.current_principal(token)
        if not self.auth.has_permission(principal, 'jobs:read'):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail='insufficient permissions',
            )
        return principal
