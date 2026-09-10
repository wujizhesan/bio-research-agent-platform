"""Asynchronous execution and durable state for registry tools."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import heapq
from itertools import count
import json
from pathlib import Path
import sqlite3
from threading import Condition, Lock, Thread
from time import perf_counter
from uuid import uuid4

try:
    from .domain_registry import run_tool, active_tool_specs, tool_specs
    from .job_execution import InlineToolExecutor
    from .resource_scheduling import (
        ResourceCapacity,
        ResourcePool,
        ResourceRequest,
        merge_requests,
        normalize_priority,
    )
    from .workflow_checkpoint import resumable_retry_arguments
    from .run_context import build_run_context, bind_run_context, RunContext
    from .observability import (
        JOB_ACTIVE, JOB_DURATION, JOB_EXECUTIONS, JOB_QUEUE_DURATION,
        JOB_TRANSITIONS, log_event, trace_id as make_trace_id,
    )
except ImportError:
    from domain_registry import run_tool, active_tool_specs, tool_specs
    from job_execution import InlineToolExecutor
    from resource_scheduling import (
        ResourceCapacity,
        ResourcePool,
        ResourceRequest,
        merge_requests,
        normalize_priority,
    )
    from workflow_checkpoint import resumable_retry_arguments
    from run_context import build_run_context, bind_run_context, RunContext
    from observability import (
        JOB_ACTIVE, JOB_DURATION, JOB_EXECUTIONS, JOB_QUEUE_DURATION,
        JOB_TRANSITIONS, log_event, trace_id as make_trace_id,
    )


TERMINAL_STATUSES = frozenset({'completed', 'failed', 'cancelled'})


def _now():
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    backend = 'local'

    def __init__(self, max_workers=2, store_path=None, tool_executor=None,
                 resource_capacity=None):
        self._max_workers = max_workers
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='bio-agent-job')
        self._tool_executor = tool_executor or InlineToolExecutor(
            lambda tool, arguments: run_tool(tool, arguments)
        )
        self._lock = Lock()
        self._condition = Condition(self._lock)
        self._jobs = {}
        self._futures = {}
        self._pending = []
        self._sequence = count()
        self._stopping = False
        self._capacity = resource_capacity or ResourceCapacity.from_env()
        self._resource_pool = ResourcePool(self._capacity)
        self._store_path = Path(store_path) if store_path else None
        if self._store_path:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_store()
            self._load_store()
        self._scheduler = Thread(
            target=self._schedule_forever,
            name='bio-agent-resource-scheduler',
            daemon=True,
        )
        self._scheduler.start()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self._store_path), timeout=30)
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _init_store(self):
        with self._connection() as connection:
            connection.execute(
                'CREATE TABLE IF NOT EXISTS jobs ('
                'job_id TEXT PRIMARY KEY, tool TEXT NOT NULL, status TEXT NOT NULL, '
                'created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, '
                'arguments_json TEXT, result_json TEXT, error TEXT, retry_of TEXT, '
                'idempotency_key TEXT, cancel_requested INTEGER DEFAULT 0, '
                'resources_json TEXT, priority INTEGER DEFAULT 0, '
                'trace_id TEXT, request_id TEXT, run_context_json TEXT)'
            )
            columns = {row[1] for row in connection.execute('PRAGMA table_info(jobs)').fetchall()}
            if 'arguments_json' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN arguments_json TEXT')
            if 'retry_of' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN retry_of TEXT')
            if 'idempotency_key' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN idempotency_key TEXT')
            if 'cancel_requested' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER DEFAULT 0')
            if 'resources_json' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN resources_json TEXT')
            if 'priority' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN priority INTEGER DEFAULT 0')
            if 'trace_id' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN trace_id TEXT')
            if 'request_id' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN request_id TEXT')
            if 'run_context_json' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN run_context_json TEXT')

    def _persist(self, record):
        if not self._store_path:
            return
        with self._connection() as connection:
            connection.execute(
                'INSERT OR REPLACE INTO jobs '
                '(job_id, tool, status, created_at, started_at, finished_at, '
                'arguments_json, result_json, error, retry_of, idempotency_key, '
                'cancel_requested, resources_json, priority, trace_id, request_id, '
                'run_context_json) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ',
                (
                    record['job_id'],
                    record['tool'],
                    record['status'],
                    record['created_at'],
                    record.get('started_at'),
                    record.get('finished_at'),
                    json.dumps(record.get('_arguments', {}), ensure_ascii=False, default=str),
                    json.dumps(record.get('result'), ensure_ascii=False, default=str)
                    if 'result' in record else None,
                    record.get('error'),
                    record.get('retry_of'),
                    record.get('idempotency_key'),
                    int(bool(record.get('_cancel_requested'))),
                    json.dumps(record.get('resources', {}), ensure_ascii=False),
                    int(record.get('priority', 0)),
                    record.get('trace_id'),
                    record.get('request_id'),
                    json.dumps(record.get('run_context'), ensure_ascii=False, default=str)
                    if record.get('run_context') else None,
                ),
            )

    def _load_store(self):
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT job_id, tool, status, created_at, started_at, finished_at, '
                'arguments_json, result_json, error, retry_of, idempotency_key, '
                'cancel_requested, resources_json, priority, trace_id, request_id, '
                'run_context_json FROM jobs'
            ).fetchall()
        interrupted_at = _now()
        resumable = []
        for row in rows:
            job_id, tool, status, created_at, started_at, finished_at, arguments_json, result_json, error, retry_of, idempotency_key, cancel_requested, resources_json, priority, trace_value, request_value, run_context_json = row
            record = {
                'job_id': job_id,
                'tool': tool,
                'status': status,
                'created_at': created_at,
                'resources': json.loads(resources_json) if resources_json else ResourceRequest().as_dict(),
                'priority': int(priority or 0),
                'trace_id': trace_value or make_trace_id(),
            }
            if request_value:
                record['request_id'] = request_value
            if run_context_json:
                try:
                    record['run_context'] = RunContext.from_dict(
                        json.loads(run_context_json)
                    ).as_dict()
                except (TypeError, ValueError, json.JSONDecodeError):
                    record.pop('run_context', None)
            if arguments_json:
                try:
                    record['_arguments'] = json.loads(arguments_json)
                except json.JSONDecodeError:
                    record['_arguments'] = {}
            if started_at:
                record['started_at'] = started_at
            if finished_at:
                record['finished_at'] = finished_at
            if result_json:
                record['result'] = json.loads(result_json)
            if error:
                record['error'] = error
            if retry_of:
                record['retry_of'] = retry_of
            if idempotency_key:
                record['idempotency_key'] = idempotency_key
            record['_cancel_requested'] = bool(cancel_requested)
            if status == 'running':
                record.update({
                    'status': 'failed',
                    'finished_at': interrupted_at,
                    'error': 'job interrupted by process restart',
                })
            elif status == 'queued':
                if record.get('_cancel_requested'):
                    record.update({
                        'status': 'cancelled',
                        'finished_at': interrupted_at,
                        'error': 'job cancelled by user',
                    })
                elif record.get('_arguments') is None:
                    record.update({
                        'status': 'failed',
                        'finished_at': interrupted_at,
                        'error': 'queued job arguments are unavailable',
                    })
                else:
                    request = ResourceRequest.from_mapping(record['resources'])
                    if self._capacity.fits(request):
                        resumable.append((job_id, tool, dict(record['_arguments']), request, record['priority']))
                    else:
                        record.update({
                            'status': 'failed',
                            'finished_at': interrupted_at,
                            'error': 'resource request exceeds current scheduler capacity: '
                            + self._capacity.rejection_reason(request),
                        })
            if 'run_context' not in record:
                try:
                    spec = self._validate_tool_state(tool)
                except ValueError:
                    spec = {'domain': 'unknown'}
                record['run_context'] = build_run_context(
                    tool,
                    record.get('_arguments', {}),
                    spec=spec,
                    resources=record['resources'],
                    priority=record['priority'],
                    job_id=job_id,
                    trace_id=record['trace_id'],
                    request_id=record.get('request_id'),
                ).as_dict()
            self._jobs[job_id] = record
        for record in self._jobs.values():
            self._persist(record)
        with self._condition:
            for job_id, tool, arguments, request, priority in resumable:
                self._enqueue_locked(job_id, tool, arguments, request, priority)

    def _public_record(self, record):
        output = dict(record)
        output.pop('_arguments', None)
        output.pop('_cancel_requested', None)
        output.pop('idempotency_key', None)
        if record.get('_cancel_requested'):
            output['cancel_requested'] = True
        return output

    def _enqueue_locked(self, job_id, tool, arguments, resources, priority):
        heapq.heappush(
            self._pending,
            (-priority, next(self._sequence), job_id, tool, dict(arguments), resources),
        )
        self._condition.notify_all()

    def _create_job_locked(self, tool, arguments, resources, priority,
                           retry_of=None, idempotency_key=None, spec=None,
                           parent_context=None):
        job_id = uuid4().hex
        run_context = build_run_context(
            tool,
            arguments,
            spec=spec,
            resources=resources.as_dict(),
            priority=priority,
            job_id=job_id,
            retry_of=retry_of,
            parent=parent_context,
            run_id=uuid4().hex if parent_context is not None else None,
        ).as_dict()
        record = {
            'job_id': job_id,
            'tool': tool,
            'status': 'queued',
            'created_at': _now(),
            '_arguments': dict(arguments),
            '_cancel_requested': False,
            'resources': resources.as_dict(),
            'priority': priority,
            'trace_id': run_context['trace_id'],
            'run_context': run_context,
        }
        if run_context.get('request_id'):
            record['request_id'] = run_context['request_id']
        if idempotency_key:
            record['idempotency_key'] = idempotency_key
        if retry_of:
            record['retry_of'] = retry_of
        self._jobs[job_id] = record
        self._persist(record)
        self._enqueue_locked(job_id, tool, arguments, resources, priority)
        JOB_TRANSITIONS.labels(self.backend, tool, 'queued').inc()
        log_event(
            'job.queued',
            backend=self.backend,
            job_id=job_id,
            tool=tool,
            priority=priority,
        )
        return self._public_record(record)

    def _validate_tool_state(self, tool):
        known = {spec['name']: spec for spec in tool_specs()}
        if tool not in known:
            raise ValueError(f'unknown tool: {tool}')
        active = {spec['name']: spec for spec in active_tool_specs()}
        if tool not in active:
            raise ValueError(f'plugin domain is disabled for tool: {tool}')
        return active[tool]

    def submit(self, tool, arguments, idempotency_key=None, resources=None,
               priority=0):
        if not isinstance(tool, str) or not tool:
            raise ValueError('tool is required')
        if not isinstance(arguments, dict):
            raise ValueError('arguments must be an object')
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ValueError('idempotency key must be a non-empty string')
            idempotency_key = idempotency_key.strip()
            if len(idempotency_key) > 128:
                raise ValueError('idempotency key is too long')
        spec = self._validate_tool_state(tool)
        request = merge_requests(spec.get('resources'), resources)
        priority = normalize_priority(priority)
        if not self._capacity.fits(request):
            raise ValueError(
                'resource request exceeds scheduler capacity: '
                + self._capacity.rejection_reason(request)
            )
        with self._lock:
            if idempotency_key:
                for existing in self._jobs.values():
                    if existing.get('idempotency_key') != idempotency_key:
                        continue
                    if (
                        existing.get('tool') != tool
                        or existing.get('_arguments') != arguments
                        or existing.get('resources') != request.as_dict()
                        or existing.get('priority', 0) != priority
                    ):
                        raise ValueError('idempotency key already used with different job payload')
                    output = self._public_record(existing)
                    output['deduplicated'] = True
                    return output
            return self._create_job_locked(
                tool,
                arguments,
                request,
                priority,
                idempotency_key=idempotency_key,
                spec=spec,
            )

    def retry(self, job_id):
        with self._lock:
            original = self._jobs.get(str(job_id))
            if original is None:
                raise ValueError(f'job not found: {job_id}')
            if original.get('status') not in TERMINAL_STATUSES:
                raise ValueError('only completed, failed or cancelled jobs can be retried')
            arguments = original.get('_arguments')
            if arguments is None:
                raise ValueError('job arguments are unavailable')
            spec = self._validate_tool_state(original['tool'])
            arguments = resumable_retry_arguments(arguments, spec)
            resources = ResourceRequest.from_mapping(original.get('resources'))
            if not self._capacity.fits(resources):
                raise ValueError(
                    'resource request exceeds scheduler capacity: '
                    + self._capacity.rejection_reason(resources)
                )
            return self._create_job_locked(
                original['tool'],
                arguments,
                resources,
                int(original.get('priority', 0)),
                retry_of=original['job_id'],
                spec=spec,
                parent_context=original.get('run_context'),
            )

    def _schedule_forever(self):
        while True:
            with self._condition:
                if self._stopping:
                    return
                if len(self._futures) >= self._max_workers:
                    self._condition.wait(timeout=0.5)
                    continue
                selected = None
                for entry in sorted(self._pending):
                    job_id = entry[2]
                    record = self._jobs.get(job_id)
                    if record is None or record.get('status') != 'queued':
                        self._pending.remove(entry)
                        heapq.heapify(self._pending)
                        continue
                    resources = entry[5]
                    if self._resource_pool.try_acquire(resources):
                        selected = entry
                        break
                if selected is None:
                    self._condition.wait(timeout=0.5)
                    continue
                self._pending.remove(selected)
                heapq.heapify(self._pending)
                _, _, job_id, tool, arguments, resources = selected
                try:
                    self._futures[job_id] = self._executor.submit(
                        self._run,
                        job_id,
                        tool,
                        arguments,
                        resources,
                    )
                except Exception as exc:
                    self._resource_pool.release(resources)
                    record = self._jobs.get(job_id)
                    if record is not None:
                        record.update({
                            'status': 'failed',
                            'finished_at': _now(),
                            'error': f'scheduler dispatch failed: {exc}',
                        })
                        self._persist(record)

    def _run(self, job_id, tool, arguments, resources):
        with self._condition:
            record = self._jobs.get(job_id)
            if record is None:
                self._resource_pool.release(resources)
                self._futures.pop(job_id, None)
                self._condition.notify_all()
                return
            if record.get('_cancel_requested'):
                record.update({
                    'status': 'cancelled',
                    'finished_at': _now(),
                    'error': 'job cancelled by user',
                })
                self._persist(record)
                self._futures.pop(job_id, None)
                self._resource_pool.release(resources)
                self._condition.notify_all()
                return
            record.update({'status': 'running', 'started_at': _now()})
            self._persist(record)
            created_at = record.get('created_at')
        run_context = record.get('run_context') or build_run_context(
            tool,
            arguments,
            spec=self._validate_tool_state(tool),
            resources=resources.as_dict(),
            priority=record.get('priority', 0),
            job_id=job_id,
        ).as_dict()
        with bind_run_context(run_context):
            started = perf_counter()
            try:
                queued_seconds = (
                    datetime.now(timezone.utc)
                    - datetime.fromisoformat(created_at)
                ).total_seconds()
                JOB_QUEUE_DURATION.labels(self.backend, tool).observe(
                    max(queued_seconds, 0)
                )
            except (TypeError, ValueError):
                pass
            JOB_ACTIVE.labels(self.backend, tool).inc()
            JOB_TRANSITIONS.labels(self.backend, tool, 'running').inc()
            log_event('job.started', backend=self.backend)
            error_type = None
            try:
                result = self._tool_executor.execute(
                    tool,
                    arguments,
                    cancelled=lambda: self._is_cancel_requested(job_id),
                )
                failed = isinstance(result, dict) and result.get('status') == 'error'
                update = {
                    'status': 'failed' if failed else 'completed',
                    'finished_at': _now(),
                    'result': result,
                }
                if failed:
                    update['error'] = result.get('error', 'tool returned an error')
            except Exception as exc:
                error_type = type(exc).__name__
                update = {
                    'status': 'failed',
                    'finished_at': _now(),
                    'error': str(exc),
                }
            with self._condition:
                record = self._jobs.get(job_id)
                if record is not None:
                    if record.get('_cancel_requested'):
                        update = {
                            'status': 'cancelled',
                            'finished_at': _now(),
                            'error': 'job cancelled by user',
                        }
                    record.update(update)
                    self._persist(record)
                self._futures.pop(job_id, None)
                self._resource_pool.release(resources)
                self._condition.notify_all()
            elapsed = perf_counter() - started
            outcome = update['status']
            JOB_ACTIVE.labels(self.backend, tool).dec()
            JOB_DURATION.labels(self.backend, tool).observe(elapsed)
            JOB_EXECUTIONS.labels(self.backend, tool, outcome).inc()
            JOB_TRANSITIONS.labels(self.backend, tool, outcome).inc()
            log_event(
                'job.completed',
                backend=self.backend,
                status=outcome,
                duration_seconds=elapsed,
                error_type=error_type,
            )

    def _is_cancel_requested(self, job_id):
        with self._condition:
            record = self._jobs.get(str(job_id))
            return record is None or bool(record.get('_cancel_requested'))

    def cancel(self, job_id):
        with self._lock:
            record = self._jobs.get(str(job_id))
            if record is None:
                raise ValueError(f'job not found: {job_id}')
            if record.get('status') in TERMINAL_STATUSES:
                return self._public_record(record)
            record['_cancel_requested'] = True
            future = self._futures.get(str(job_id))
            if record.get('status') == 'queued':
                released = future is not None and future.cancel()
                if released:
                    self._resource_pool.release(
                        ResourceRequest.from_mapping(record.get('resources'))
                    )
                record.update({
                    'status': 'cancelled',
                    'finished_at': _now(),
                    'error': 'job cancelled by user',
                })
                self._futures.pop(str(job_id), None)
            self._persist(record)
            self._condition.notify_all()
            return self._public_record(record)

    def get(self, job_id):
        with self._lock:
            record = self._jobs.get(str(job_id))
            return self._public_record(record) if record else None

    def list(self, limit=20):
        try:
            size = min(max(int(limit), 1), 100)
        except (TypeError, ValueError):
            size = 20
        with self._lock:
            records = [self._public_record(record) for record in self._jobs.values()]
        return sorted(records, key=lambda item: item['created_at'], reverse=True)[:size]

    def resource_status(self):
        with self._lock:
            queued = sum(
                1 for record in self._jobs.values() if record.get('status') == 'queued'
            )
            running = sum(
                1 for record in self._jobs.values() if record.get('status') == 'running'
            )
        snapshot = self._resource_pool.snapshot()
        snapshot['queued'] = queued
        snapshot['running'] = running
        return snapshot

    def shutdown(self):
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
        self._scheduler.join(timeout=5)
        shutdown = getattr(self._tool_executor, 'shutdown', None)
        if shutdown:
            shutdown()
        self._executor.shutdown(wait=True, cancel_futures=True)
