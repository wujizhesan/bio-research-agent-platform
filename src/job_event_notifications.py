"""Wake SSE readers when PostgreSQL job events commit."""

import asyncio
from contextlib import suppress
import hashlib
import logging


JOB_EVENT_CHANNEL = 'bioagent_job_events'


def job_event_key(job_id):
    return hashlib.sha256(str(job_id).encode('utf-8')).hexdigest()


class JobEventListener:
    def __init__(self, database_url, connect_timeout=5, connect=None):
        self.database_url = database_url.replace(
            'postgresql+asyncpg://', 'postgresql://', 1
        )
        self.connect_timeout = connect_timeout
        self._connect = connect
        self.ready = asyncio.Event()
        self._subscribers = {}
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._listen())

    def subscribe(self, job_id):
        key = job_event_key(job_id)
        wake = asyncio.Event()
        self._subscribers.setdefault(key, set()).add(wake)
        return wake

    def unsubscribe(self, job_id, wake):
        key = job_event_key(job_id)
        subscribers = self._subscribers.get(key)
        if subscribers is not None:
            subscribers.discard(wake)
            if not subscribers:
                del self._subscribers[key]

    def _notify(self, _connection, _pid, _channel, key):
        for wake in self._subscribers.get(key, ()):
            wake.set()

    def _wake_all(self):
        for subscribers in self._subscribers.values():
            for wake in subscribers:
                wake.set()

    async def _listen(self):
        connect = self._connect
        if connect is None:
            from asyncpg import connect
        while True:
            connection = None
            terminated = asyncio.Event()
            try:
                connection = await connect(
                    self.database_url, timeout=self.connect_timeout
                )
                connection.add_termination_listener(
                    lambda _connection: terminated.set()
                )
                await connection.add_listener(JOB_EVENT_CHANNEL, self._notify)
                self.ready.set()
                self._wake_all()
                await terminated.wait()
            except Exception as exc:
                logging.getLogger(__name__).warning(
                    'job event listener unavailable: %s', type(exc).__name__
                )
            finally:
                self.ready.clear()
                self._wake_all()
                if connection is not None and not connection.is_closed():
                    with suppress(Exception):
                        await connection.close(timeout=1)
            await asyncio.sleep(1)

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        self._subscribers.clear()
