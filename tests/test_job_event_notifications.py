import asyncio
import unittest

from src.job_event_notifications import (
    JOB_EVENT_CHANNEL, JobEventListener, job_event_key,
)


class NotifyingConnection:
    def __init__(self):
        self.closed = False

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


class JobEventListenerTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_connection_routes_notifications_to_all_matching_readers(self):
        connection = NotifyingConnection()
        connections = []

        async def connect(url, timeout):
            connections.append((url, timeout))
            return connection

        listener = JobEventListener(
            'postgresql+asyncpg://api:password@db/research', connect=connect
        )
        first = listener.subscribe('job-a')
        second = listener.subscribe('job-a')
        other = listener.subscribe('job-b')
        listener.start()
        listener.start()
        try:
            await asyncio.wait_for(listener.ready.wait(), 1)
            for wake in (first, second, other):
                self.assertTrue(wake.is_set())
                wake.clear()
            connection.notification(connection, 1, JOB_EVENT_CHANNEL, job_event_key('job-a'))
            self.assertTrue(first.is_set())
            self.assertTrue(second.is_set())
            self.assertFalse(other.is_set())
            first.clear()
            listener.unsubscribe('job-a', first)
            connection.notification(connection, 1, JOB_EVENT_CHANNEL, job_event_key('job-a'))
            self.assertFalse(first.is_set())
            listener.unsubscribe('job-a', second)
            listener.unsubscribe('job-b', other)
            self.assertEqual(listener._subscribers, {})
            self.assertEqual(connections, [('postgresql://api:password@db/research', 5)])
        finally:
            await listener.close()
        self.assertTrue(connection.closed)
        self.assertFalse(listener.ready.is_set())

    async def test_disconnect_wakes_readers_and_reconnect_checks_for_missed_events(self):
        connections = []
        reconnected = asyncio.Event()

        async def connect(_url, timeout):
            connection = NotifyingConnection()
            connections.append(connection)
            if len(connections) > 1:
                reconnected.set()
            return connection

        listener = JobEventListener('postgresql://api@db/research', connect=connect)
        wake = listener.subscribe('job-a')
        listener.start()
        try:
            await asyncio.wait_for(listener.ready.wait(), 1)
            wake.clear()
            await connections[0].close()
            await asyncio.wait_for(wake.wait(), 1)
            self.assertFalse(listener.ready.is_set())
            wake.clear()
            await asyncio.wait_for(reconnected.wait(), 2)
            await asyncio.wait_for(wake.wait(), 1)
            self.assertTrue(listener.ready.is_set())
        finally:
            await listener.close()
        self.assertTrue(all(connection.closed for connection in connections))

    async def test_failed_connect_retries_without_exposing_connection_details(self):
        attempts = 0
        connection = NotifyingConnection()

        async def connect(_url, timeout):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError('secret connection details')
            return connection

        listener = JobEventListener('postgresql://api@db/research', connect=connect)
        listener.start()
        try:
            with self.assertLogs('src.job_event_notifications', level='WARNING') as logs:
                await asyncio.wait_for(listener.ready.wait(), 2)
            self.assertEqual(attempts, 2)
            self.assertNotIn('secret connection details', str(logs.output))
        finally:
            await listener.close()

    async def test_shutdown_cancels_a_pending_connect(self):
        connecting = asyncio.Event()
        cancelled = asyncio.Event()

        async def connect(_url, timeout):
            connecting.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        listener = JobEventListener('postgresql://api@db/research', connect=connect)
        listener.start()
        await connecting.wait()
        await asyncio.wait_for(listener.close(), 1)
        self.assertTrue(cancelled.is_set())
        await listener.close()
