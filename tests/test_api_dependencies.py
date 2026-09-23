import unittest

from fastapi import HTTPException
from starlette.requests import Request

from src.api_dependencies import ApiDependencies
from src.auth import Principal


class FakeAuth:
    jwt_secret = 'test-secret-that-is-long-enough-for-hs256'
    issuer = 'test-issuer'
    session_cookie_name = 'bioagent_session'
    csrf_cookie_name = 'bioagent_csrf'

    def authenticate(self, authorization):
        if authorization != 'Bearer valid':
            raise AssertionError('unexpected token')
        return Principal('alice', ('researcher',), 'jwt', 'session-alice-valid-001')

    def has_permission(self, principal, permission):
        return principal.subject == 'alice' and permission in {
            'jobs:read',
            'jobs:write',
        }

    def validate_csrf(self, principal, cookie_token, header_token):
        return (
            principal.auth_type != 'jwt_cookie'
            or cookie_token == header_token == 'valid-csrf'
        )


class FakeDatabase:
    async def validate_auth_session(self, jti, subject, token_version, roles_sha256):
        return jti == 'session-alice-valid-001' and subject == 'alice'

    async def get_project(self, project_id):
        if project_id == 'missing':
            return None
        return {'project_id': project_id, 'owner_subject': 'owner'}

    async def get_project_member(self, project_id, subject):
        if subject == 'alice':
            return {'project_id': project_id, 'subject': subject, 'role': 'editor'}
        return None

    async def get_job_project(self, job_id):
        return 'project-1' if job_id == 'scoped-job' else None


class ApiDependenciesTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.dependencies = ApiDependencies(FakeAuth(), FakeDatabase())
        self.principal = Principal(
            'alice',
            ('researcher',),
            'jwt',
            'session-alice-valid-001',
        )

    @staticmethod
    def request(method='GET', headers=None):
        encoded_headers = [
            (str(name).lower().encode(), str(value).encode())
            for name, value in (headers or {}).items()
        ]
        return Request({
            'type': 'http',
            'method': method,
            'path': '/',
            'query_string': b'',
            'headers': encoded_headers,
            'scheme': 'http',
            'server': ('testserver', 80),
            'client': ('testclient', 50000),
        })

    async def test_project_and_job_access_share_membership_policy(self):
        project = await self.dependencies.project_access(
            'project-1',
            self.principal,
            {'editor'},
        )
        self.assertEqual(project['project_id'], 'project-1')
        project_id = await self.dependencies.job_access(
            'scoped-job',
            self.principal,
            {'editor'},
        )
        self.assertEqual(project_id, 'project-1')

        with self.assertRaises(HTTPException) as caught:
            await self.dependencies.project_access(
                'missing',
                self.principal,
                {'editor'},
            )
        self.assertEqual(caught.exception.status_code, 404)

    async def test_unscoped_job_access_is_admin_only(self):
        with self.assertRaises(HTTPException) as caught:
            await self.dependencies.job_access(
                'unscoped-job',
                self.principal,
                {'editor'},
            )
        self.assertEqual(caught.exception.status_code, 403)

        project_id = await self.dependencies.job_access(
            'unscoped-job',
            Principal('root', ('admin',), 'jwt'),
            {'editor'},
        )
        self.assertIsNone(project_id)

    async def test_stream_ticket_is_bound_to_job_and_permission(self):
        ticket = self.dependencies.issue_stream_ticket('job-1', self.principal)
        principal = await self.dependencies.stream_principal(
            self.request(),
            'job-1',
            ticket=ticket,
            token=None,
        )
        self.assertEqual(principal.subject, 'alice')
        self.assertEqual(principal.auth_type, 'sse_ticket')

        with self.assertRaises(HTTPException) as caught:
            await self.dependencies.stream_principal(
                self.request(),
                'job-2',
                ticket=ticket,
                token=None,
            )
        self.assertEqual(caught.exception.status_code, 401)

    async def test_bearer_authentication_and_permission_dependency(self):
        principal = await self.dependencies.current_principal(
            self.request(),
            'valid',
        )
        self.assertEqual(principal.subject, 'alice')
        permission = self.dependencies.require_permission('jobs:write')
        self.assertEqual(await permission(principal), principal)

    async def test_metrics_token_is_scoped_to_metrics_dependency(self):
        dependencies = ApiDependencies(
            FakeAuth(),
            FakeDatabase(),
            metrics_scrape_token='metrics-secret-' * 3,
        )
        principal = await dependencies.metrics_principal(
            self.request(headers={'Authorization': 'Bearer metrics-secret-token'}),
            'metrics-secret-' * 3,
        )
        self.assertEqual(principal.subject, 'metrics-scraper')
        self.assertEqual(principal.roles, ('monitoring',))
        with self.assertRaises(AssertionError):
            await dependencies.current_principal(
                self.request(),
                'metrics-secret-' * 3,
            )

    async def test_cookie_authentication_requires_csrf_on_writes(self):
        with self.assertRaises(HTTPException) as caught:
            await self.dependencies.current_principal(
                self.request('POST', {
                    'Cookie': 'bioagent_session=valid',
                }),
                None,
            )
        self.assertEqual(caught.exception.status_code, 403)

        principal = await self.dependencies.current_principal(
            self.request('POST', {
                'Cookie': (
                    'bioagent_session=valid; '
                    'bioagent_csrf=valid-csrf'
                ),
                'X-CSRF-Token': 'valid-csrf',
            }),
            None,
        )
        self.assertEqual(principal.auth_type, 'jwt_cookie')


if __name__ == '__main__':
    unittest.main()
