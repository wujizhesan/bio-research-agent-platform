"""Asynchronous database state writer for standalone workers."""

import asyncio
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic

from prometheus_client import Gauge

try:
    from .database import Database
    from .settings import PlatformSettings
except ImportError:
    from database import Database
    from settings import PlatformSettings


_STOP = object()
STATE_WRITER_QUEUE_UTILIZATION = Gauge(
    'bio_agent_state_writer_queue_utilization_ratio',
    'State writer queued and in-flight records divided by configured capacity.',
)
STATE_WRITER_ACCEPTING_WORK = Gauge(
    'bio_agent_state_writer_accepting_work',
    'Whether the worker may claim new jobs based on state writer pressure.',
)


class DatabaseStateWriter:
    def __init__(self, database_url=None, settings=None):
        settings = settings or PlatformSettings.from_env()
        self.database_url = database_url or settings.database_url
        self.batch_size = settings.state_writer_batch_size
        self.batch_wait_seconds = settings.state_writer_batch_wait_ms / 1000
        self.queue_maxsize = settings.state_writer_queue_maxsize
        self.enqueue_timeout_seconds = settings.state_writer_enqueue_timeout_seconds
        self.max_retries = settings.state_writer_max_retries
        self.retry_base_seconds = settings.state_writer_retry_base_seconds
        self.pause_threshold = settings.state_writer_pause_threshold
        self.resume_threshold = settings.state_writer_resume_threshold
        self._queue = Queue(maxsize=self.queue_maxsize)
        self._ready = Event()
        self._error = None
        self._last_error = None
        self._closed = False
        self._inflight = 0
        self._pressure_lock = Lock()
        self._admission_paused = False
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
                with self._pressure_lock:
                    self._inflight += len(batch)
                try:
                    attempt = 0
                    while True:
                        try:
                            await database.upsert_jobs(batch)
                            self._last_error = None
                            break
                        except Exception as exc:
                            attempt += 1
                            if attempt >= self.max_retries:
                                self._last_error = exc
                            await database.close()
                            await asyncio.sleep(min(
                                self.retry_base_seconds * (2 ** min(attempt - 1, 8)),
                                5.0,
                            ))
                            database = Database(self.database_url)
                            try:
                                await database.init_schema()
                            except Exception:
                                continue
                finally:
                    with self._pressure_lock:
                        self._inflight = max(self._inflight - len(batch), 0)
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
        try:
            self._queue.put(
                dict(record),
                timeout=self.enqueue_timeout_seconds,
            )
        except Full as exc:
            raise RuntimeError('database state writer queue is full') from exc

    def load_dispatchable(self, limit=1000):
        if self._closed:
            raise RuntimeError('database state writer is closed')
        if self._error:
            raise RuntimeError('database state writer failed') from self._error

        async def load():
            database = Database(self.database_url)
            try:
                return await database.list_dispatchable_jobs(limit)
            finally:
                await database.close()

        return asyncio.run(load())

    def load_execution_result(self, execution_key):
        return self._database_call('get_execution_result', execution_key)

    def begin_execution_attempt(
        self,
        execution_key,
        job_id,
        fencing_token,
        attempt,
        semantics='pure',
    ):
        return self._database_call(
            'begin_execution_attempt',
            execution_key,
            job_id,
            fencing_token,
            attempt,
            semantics,
        )

    def store_execution_result(self, execution_key, job_id, result, fencing_token=None):
        return self._database_call(
            'store_execution_result',
            execution_key,
            job_id,
            result,
            fencing_token,
        )

    def _database_call(self, method, *args):
        last_error = None
        for attempt in range(self.max_retries):
            async def invoke():
                database = Database(self.database_url)
                try:
                    return await getattr(database, method)(*args)
                finally:
                    await database.close()

            try:
                value = asyncio.run(invoke())
                self._last_error = None
                return value
            except Exception as exc:
                last_error = exc
                if attempt + 1 < self.max_retries:
                    from time import sleep
                    sleep(min(
                        self.retry_base_seconds * (2 ** min(attempt, 8)),
                        5.0,
                    ))
        self._last_error = last_error
        raise RuntimeError(f'database operation failed: {method}') from last_error

    def pending(self):
        with self._pressure_lock:
            return self._queue.qsize() + self._inflight

    def admission(self):
        pending = self.pending()
        utilization = pending / self.queue_maxsize
        healthy = (
            not self._closed
            and self._error is None
            and self._thread.is_alive()
        )
        with self._pressure_lock:
            if (
                not healthy
                or getattr(self, '_last_error', None) is not None
                or utilization >= self.pause_threshold
            ):
                self._admission_paused = True
            elif self._admission_paused and utilization <= self.resume_threshold:
                self._admission_paused = False
            accepting = healthy and not self._admission_paused
        STATE_WRITER_QUEUE_UTILIZATION.set(utilization)
        STATE_WRITER_ACCEPTING_WORK.set(1 if accepting else 0)
        return {
            'accepting_work': accepting,
            'paused': not accepting,
            'pending': pending,
            'capacity': self.queue_maxsize,
            'utilization': utilization,
            'pause_threshold': self.pause_threshold,
            'resume_threshold': self.resume_threshold,
            'last_error': (
                type(self._last_error).__name__
                if getattr(self, '_last_error', None) else None
            ),
        }

    def health(self):
        admission = self.admission()
        return {
            'healthy': (
                not self._closed
                and self._error is None
                and self._thread.is_alive()
            ),
            **admission,
        }

    def flush(self):
        self._queue.join()
        if self._error:
            raise RuntimeError('database state writer failed') from self._error

    def close(self):
        if self._closed:
            return
        self._closed = True
        deadline = monotonic() + 30
        while self._thread.is_alive() and monotonic() < deadline:
            try:
                self._queue.put(_STOP, timeout=min(0.5, deadline - monotonic()))
                break
            except Full:
                continue
        self._thread.join(timeout=max(deadline - monotonic(), 0))
