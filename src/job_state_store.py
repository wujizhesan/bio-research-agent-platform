"""Asynchronous database state writer for standalone workers."""

import asyncio
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from uuid import uuid4

from prometheus_client import Gauge
from sqlalchemy import text

try:
    from .database import Database, database_worker_scope
    from .settings import PlatformSettings
except ImportError:
    from database import Database, database_worker_scope
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


class DatabaseDispatchSource:
    def __init__(self, database_url=None, settings=None, dispatcher_id=None):
        settings = settings or PlatformSettings.from_env()
        self.database_url = database_url or settings.database_url
        self.dispatcher_id = str(dispatcher_id or f'dispatcher-{uuid4().hex}')

    def load_dispatchable(self, limit=1000):
        async def load():
            database = Database(self.database_url)
            try:
                return await database.list_dispatcher_jobs(limit)
            finally:
                await database.close()

        return asyncio.run(load())

    def claim_dispatchable(
        self,
        limit=1000,
        lease_seconds=30,
        claim_ticket_ttl_seconds=900,
    ):
        async def claim():
            database = Database(self.database_url)
            try:
                return await database.claim_dispatch_batch(
                    self.dispatcher_id,
                    limit=limit,
                    lease_seconds=lease_seconds,
                    claim_ticket_ttl_seconds=claim_ticket_ttl_seconds,
                )
            finally:
                await database.close()

        return asyncio.run(claim())

    def complete_claims(
        self,
        outcomes,
        reconcile_seconds=30,
        failure_delay_seconds=2,
    ):
        async def complete():
            database = Database(self.database_url)
            try:
                return await database.complete_dispatch_claims(
                    self.dispatcher_id,
                    outcomes,
                    reconcile_seconds=reconcile_seconds,
                    failure_delay_seconds=failure_delay_seconds,
                )
            finally:
                await database.close()

        return asyncio.run(complete())


class DatabaseStateWriter:
    def __init__(self, database_url=None, settings=None, require_job_scope=False):
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
        self.storage_quota_bytes = settings.upload_total_quota_bytes
        self.require_job_scope = bool(require_job_scope)
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
                            if self.require_job_scope:
                                for item in batch:
                                    claim = self._worker_claim(item)
                                    if claim is None:
                                        continue
                                    with database_worker_scope(**claim):
                                        try:
                                            await database.upsert_worker_job(
                                                item,
                                                claim['capability'],
                                                claim['worker_id'],
                                                claim['fencing_token'],
                                                claim['attempt'],
                                            )
                                        except PermissionError as exc:
                                            if 'worker claim is invalid or stale' not in str(exc):
                                                raise
                            else:
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

    @staticmethod
    def _worker_claim(record):
        execution = dict(record.get('execution') or {})
        capability = str(record.get('_execution_key') or '')
        worker_id = str(
            record.get('_worker_id') or execution.get('worker_id') or ''
        )
        fencing_token = str(
            record.get('_fencing_token')
            or execution.get('fencing_token')
            or ''
        )
        attempt = int(record.get('_attempts', 0) or 0)
        if not capability or not worker_id or not fencing_token or attempt < 1:
            return None
        return {
            'job_id': str(record.get('job_id') or ''),
            'capability': capability,
            'worker_id': worker_id,
            'fencing_token': fencing_token,
            'attempt': attempt,
        }

    def load_dispatchable(self, limit=1000):
        if self._closed:
            raise RuntimeError('database state writer is closed')
        if self._error:
            raise RuntimeError('database state writer failed') from self._error

        async def load():
            database = Database(self.database_url)
            try:
                if self.require_job_scope:
                    return await database.list_worker_dispatchable_jobs(limit)
                return await database.list_dispatchable_jobs(limit)
            finally:
                await database.close()

        return asyncio.run(load())

    def claim_job(self, job_id, capability, worker_id, claim_ticket, lease_seconds):
        if not self.require_job_scope:
            return None

        async def claim():
            database = Database(self.database_url)
            try:
                return await database.claim_worker_job(
                    job_id,
                    capability,
                    worker_id,
                    claim_ticket,
                    lease_seconds,
                )
            finally:
                await database.close()

        return asyncio.run(claim())

    def load_execution_result(
        self,
        execution_key,
        job_id=None,
        fencing_token=None,
        worker_id=None,
        attempt=None,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        return self._database_call(
            'get_execution_result', execution_key, scope=scope
        )

    def begin_execution_attempt(
        self,
        execution_key,
        job_id,
        fencing_token,
        attempt,
        semantics='pure',
        worker_id=None,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        return self._database_call(
            'begin_execution_attempt',
            execution_key,
            job_id,
            fencing_token,
            attempt,
            semantics,
            scope=scope,
        )

    def defer_pure_job(
        self,
        execution_key,
        job_id,
        fencing_token,
        attempt,
        worker_id,
        delay_seconds,
        max_attempts,
        record_revision=0,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        return self._database_call(
            'defer_pure_job',
            execution_key,
            job_id,
            fencing_token,
            attempt,
            worker_id,
            delay_seconds,
            max_attempts,
            record_revision,
            scope=scope,
        )

    def store_execution_result(
        self,
        execution_key,
        job_id,
        result,
        fencing_token=None,
        worker_id=None,
        attempt=None,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        return self._database_call(
            'store_execution_result',
            execution_key,
            job_id,
            result,
            fencing_token,
            scope=scope,
        )

    def store_execution_result_with_artifacts(
        self,
        execution_key,
        job_id,
        result,
        publication_ids,
        fencing_token=None,
        worker_id=None,
        attempt=None,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        return self._database_call(
            'store_execution_result_with_artifacts',
            execution_key,
            job_id,
            result,
            list(publication_ids),
            fencing_token,
            scope=scope,
        )

    def reserve_artifacts(
        self,
        job_id,
        project_id,
        execution_key,
        fencing_token,
        attempt,
        worker_id,
        artifacts,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        records = [{
            **dict(item),
            'job_id': str(job_id),
            'project_id': str(project_id),
            'execution_key': str(execution_key),
            'fencing_token': str(fencing_token),
            'attempt': int(attempt),
            'quota_bytes': self.storage_quota_bytes,
        } for item in artifacts]
        return self._database_call(
            'reserve_job_artifacts',
            records,
            scope=scope,
        )

    def mark_artifacts_uploaded(
        self,
        job_id,
        execution_key,
        fencing_token,
        attempt,
        worker_id,
        artifacts,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        return self._database_call(
            'mark_job_artifacts_uploaded',
            [dict(item) for item in artifacts],
            scope=scope,
        )

    def commit_artifacts(
        self,
        job_id,
        execution_key,
        fencing_token,
        attempt,
        worker_id,
        publication_ids,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        return self._database_call(
            'commit_job_artifacts',
            list(publication_ids),
            scope=scope,
        )

    def orphan_artifacts(
        self,
        job_id,
        execution_key,
        fencing_token,
        attempt,
        worker_id,
        publication_ids,
        error=None,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        return self._database_call(
            'orphan_job_artifacts',
            list(publication_ids),
            error,
            scope=scope,
        )

    def list_job_artifacts(
        self,
        job_id,
        execution_key,
        fencing_token,
        attempt,
        worker_id,
        statuses=None,
    ):
        scope = self._execution_scope(
            execution_key,
            job_id,
            fencing_token,
            worker_id,
            attempt,
        )
        return self._database_call(
            'list_job_artifacts',
            job_id,
            statuses,
            scope=scope,
        )

    def _execution_scope(
        self,
        execution_key,
        job_id,
        fencing_token,
        worker_id,
        attempt,
    ):
        if not self.require_job_scope:
            return None
        scope = {
            'job_id': str(job_id or ''),
            'capability': str(execution_key or ''),
            'worker_id': str(worker_id or ''),
            'fencing_token': str(fencing_token or ''),
            'attempt': int(attempt or 0),
        }
        if (
            not scope['job_id']
            or not scope['capability']
            or not scope['worker_id']
            or not scope['fencing_token']
            or scope['attempt'] < 1
        ):
            raise RuntimeError('database operation requires a complete worker claim')
        return scope

    def _database_call(self, method, *args, scope=None):
        last_error = None
        for attempt in range(self.max_retries):
            async def invoke():
                database = Database(self.database_url)
                try:
                    if scope is None:
                        return await getattr(database, method)(*args)
                    with database_worker_scope(**scope):
                        if database.url.startswith('postgresql'):
                            async with database.sessions() as session:
                                claimed = await session.scalar(
                                    text(
                                        'SELECT bioagent_bind_worker_claim('
                                        ':job_id, :capability, :worker_id, '
                                        ':fencing_token, :attempt)'
                                    ),
                                    scope,
                                )
                                if not claimed:
                                    raise PermissionError(
                                        'worker claim is invalid or stale'
                                    )
                                await session.commit()
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
        raise RuntimeError(
            f'database operation failed: {method}: {last_error}'
        ) from last_error

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
