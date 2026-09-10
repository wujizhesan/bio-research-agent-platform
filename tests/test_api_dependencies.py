import unittest

from fastapi import HTTPException

from src.api_dependencies import ApiDependencies
from src.auth import Principal


class FakeAuth:
    jwt_secret = 'test-secret-that-is-long-enough-for-hs256'
    issuer = 'test-issuer'

    def authenticate(self, authorization):
        if authorization != 'Bearer valid':
            raise AssertionError('unexpected token')
        return Principal('alice', ('researcher',), 'jwt')

    def has_permission(self, principal, permission):
        return principal.subject == 'alice' and permission in {
            'jobs:read',
            'jobs:write',
        }


class FakeDatabase:
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
        self.principal = Principal('alice', ('researcher',), 'jwt')

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

    async def test_stream_ticket_is_bound_to_job_and_permission(self):
        ticket = self.dependencies.issue_stream_ticket('job-1', self.principal)
        principal = await self.dependencies.stream_principal(
            'job-1',
            ticket=ticket,
            token=None,
        )
        self.assertEqual(principal.subject, 'alice')
        self.assertEqual(principal.auth_type, 'sse_ticket')

        with self.assertRaises(HTTPException) as caught:
            await self.dependencies.stream_principal(
                'job-2',
                ticket=ticket,
                token=None,
            )
        self.assertEqual(caught.exception.status_code, 401)

    async def test_bearer_authentication_and_permission_dependency(self):
        principal = await self.dependencies.current_principal('valid')
        self.assertEqual(principal.subject, 'alice')
        permission = self.dependencies.require_permission('jobs:write')
        self.assertEqual(await permission(principal), principal)


if __name__ == '__main__':
    unittest.main()
