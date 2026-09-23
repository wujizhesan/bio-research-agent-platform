from types import SimpleNamespace
import unittest

from src.dispatcher import _health_check


class FakeConnection:
    def __init__(self):
        self.queries = []
        self.closed = False

    async def fetchval(self, query):
        self.queries.append(query)
        return 1

    async def close(self):
        self.closed = True


class FakeAsyncpg:
    def __init__(self):
        self.connection = FakeConnection()
        self.url = None
        self.timeout = None

    async def connect(self, url, timeout):
        self.url = url
        self.timeout = timeout
        return self.connection


class FakeRedisClient:
    def __init__(self):
        self.closed = False

    def ping(self):
        return True

    def close(self):
        self.closed = True


class FakeRedisFactory:
    def __init__(self):
        self.client = FakeRedisClient()
        self.Redis = self
        self.url = None
        self.options = None

    def from_url(self, url, **options):
        self.url = url
        self.options = options
        return self.client


class DispatcherHealthTests(unittest.TestCase):
    def test_health_check_uses_lightweight_database_and_redis_probes(self):
        settings = SimpleNamespace(
            database_url='postgresql+asyncpg://dispatcher:secret@db/bioagent',
            redis_url='redis://redis:6379/0',
            redis_socket_timeout=3,
            readiness_timeout_seconds=2,
        )
        asyncpg = FakeAsyncpg()
        redis = FakeRedisFactory()
        _health_check(settings, asyncpg_module=asyncpg, redis_module=redis)
        self.assertEqual(
            asyncpg.url,
            'postgresql://dispatcher:secret@db/bioagent',
        )
        self.assertEqual(asyncpg.connection.queries, ['SELECT 1'])
        self.assertTrue(asyncpg.connection.closed)
        self.assertEqual(redis.url, 'redis://redis:6379/0')
        self.assertEqual(redis.options['socket_timeout'], 3)
        self.assertTrue(redis.client.closed)


if __name__ == '__main__':
    unittest.main()
