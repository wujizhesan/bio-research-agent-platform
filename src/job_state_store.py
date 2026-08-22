"""Asynchronous database state writer for standalone workers."""

import asyncio
import os
from queue import Empty, Queue
from threading import Event, Thread

try:
    from .database import Database
except ImportError:
    from database import Database


_STOP = object()


class DatabaseStateWriter:
    def __init__(self, database_url=None):
        self.database_url = database_url or os.environ.get('DATABASE_URL')
        try:
            self.batch_size = max(int(os.environ.get('STATE_WRITER_BATCH_SIZE', '50')), 1)
        except (TypeError, ValueError):
            self.batch_size = 50
        try:
            self.batch_wait_seconds = max(float(os.environ.get('STATE_WRITER_BATCH_WAIT_MS', '20')) / 1000, 0)
        except (TypeError, ValueError):
            self.batch_wait_seconds = 0.02
        self._queue = Queue()
        self._ready = Event()
        self._error = None
        self._closed = False
        self._thread = Thread(target=self._run, name='bio-agent-db-state-writer', daemon=True)
        self._thread.start()
        self._ready.wait()
        if self._error:
            raise RuntimeError('database state writer failed to start') from self._error

    def _run(self):
        asyncio.run(self._consume())

    async def _consume(self):
        database = Database(self.database_url)
        try:
            await database.init_schema()
            self._ready.set()
            while True:
                record = await asyncio.to_thread(self._queue.get)
                if record is _STOP:
                    self._queue.task_done()
                    break
                batch = [record]
                stop_requested = False
                deadline = asyncio.get_running_loop().time() + self.batch_wait_seconds
                while len(batch) < self.batch_size:
                    timeout = deadline - asyncio.get_running_loop().time()
                    if timeout <= 0:
                        break
                    try:
                        next_record = await asyncio.to_thread(self._queue.get, True, timeout)
                    except Empty:
                        break
                    if next_record is _STOP:
                        self._queue.task_done()
                        stop_requested = True
                        break
                    batch.append(next_record)
                try:
                    await database.upsert_jobs(batch)
                except Exception as exc:
                    self._error = exc
                finally:
                    for _ in batch:
                        self._queue.task_done()
                if stop_requested:
                    break
        except Exception as exc:
            self._error = exc
            self._ready.set()
        finally:
            await database.close()

    def save(self, record):
        if self._closed:
            raise RuntimeError('database state writer is closed')
        if self._error:
            raise RuntimeError('database state writer failed') from self._error
        self._queue.put(dict(record))

    def flush(self):
        self._queue.join()
        if self._error:
            raise RuntimeError('database state writer failed') from self._error

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            self.flush()
        finally:
            self._queue.put(_STOP)
            self._thread.join(timeout=30)
