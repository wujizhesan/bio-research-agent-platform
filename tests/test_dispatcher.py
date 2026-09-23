from types import SimpleNamespace
from threading import Event
import unittest

from src.dispatcher import _DispatchWakeListener, _health_check


class FakeConnection:
    def __init__(self):
        self.queries = []
        self.closed = False

    async def fetchval(self, query):
        self.queries.append(query)
        return 1

    async def close(self):
        self.closed = True


class NotifyingConnection(FakeConnection):
    def add_termination_listener(self, callback):
        self.termination = callback

    async def add_listener(self, channel, callback):
        self.channel = channel
        self.notification = callback

    def is_closed(self):
        return self.closed

    async def close(self, timeout=None):
        self.closed = True
        self.termination(self)


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

    def test_listener_wakes_for_notifications_and_closes(self):
        asyncpg = FakeAsyncpg()
        asyncpg.connection = NotifyingConnection()
        listener = _DispatchWakeListener(
            'postgresql+asyncpg://dispatcher:secret@db/bioagent',
            connect_timeout=1,
            asyncpg_module=asyncpg,
        )
        try:
            listener.start()
            self.assertTrue(listener.ready.wait(2))
            self.assertEqual(asyncpg.connection.channel, 'bioagent_dispatch_outbox')
            listener.wake.clear()
            asyncpg.connection.notification(
                asyncpg.connection, 1, asyncpg.connection.channel, 'job-1'
            )
            self.assertTrue(listener.wake.wait(1))
        finally:
            listener.close()
        self.assertTrue(asyncpg.connection.closed)

    def test_listener_reconnects_after_connection_closes(self):
        class ReconnectingAsyncpg:
            def __init__(self):
                self.connections = []
                self.reconnected = Event()

            async def connect(self, _url, timeout):
                connection = NotifyingConnection()
                self.connections.append(connection)
                if len(self.connections) > 1:
                    self.reconnected.set()
                return connection

        asyncpg = ReconnectingAsyncpg()
        listener = _DispatchWakeListener(
            'postgresql+asyncpg://dispatcher:secret@db/bioagent',
            connect_timeout=1,
            asyncpg_module=asyncpg,
        )
        try:
            listener.start()
            self.assertTrue(listener.ready.wait(2))
            listener.wake.clear()
            asyncpg.connections[0].closed = True
            self.assertTrue(asyncpg.reconnected.wait(3))
            self.assertTrue(listener.wake.wait(1))
        finally:
            listener.close()


if __name__ == '__main__':
    unittest.main()
