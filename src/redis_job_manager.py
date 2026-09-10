"""Redis-backed job state and queue for horizontally scalable workers."""

from datetime import datetime, timezone
from contextlib import nullcontext
import os
from threading import Lock
from time import time
from uuid import uuid4

try:
    from .domain_registry import active_tool_specs, run_tool, tool_specs
    from .job_execution import InlineToolExecutor
    from .job_manager import TERMINAL_STATUSES
    from .resource_scheduling import (
        ResourceCapacity,
        ResourcePool,
        ResourceRequest,
        merge_requests,
        normalize_priority,
    )
    from .workflow_checkpoint import resumable_retry_arguments
    from .redis_job_coordinator import RedisExecutionCoordinator
    from .redis_job_metrics import RedisJobMetrics
    from .redis_job_recovery import RedisLeaseRecovery
    from .redis_job_store import RedisQueueStore
    from .redis_job_worker import RedisJobWorkerRuntime
    from .run_context import build_run_context
except ImportError:
    from domain_registry import active_tool_specs, run_tool, tool_specs
    from job_execution import InlineToolExecutor
    from job_manager import TERMINAL_STATUSES
    from resource_scheduling import (
        ResourceCapacity,
        ResourcePool,
        ResourceRequest,
        merge_requests,
        normalize_priority,
    )
    from workflow_checkpoint import resumable_retry_arguments
    from redis_job_coordinator import RedisExecutionCoordinator
    from redis_job_metrics import RedisJobMetrics
    from redis_job_recovery import RedisLeaseRecovery
    from redis_job_store import RedisQueueStore
    from redis_job_worker import RedisJobWorkerRuntime
    from run_context import build_run_context


def _now():
    return datetime.now(timezone.utc).isoformat()


class RedisJobManager:
    backend = 'redis'

    def __init__(
        self,
        redis_url=None,
        namespace=None,
        redis_client=None,
        lease_seconds=None,
        worker_id=None,
        result_ttl_seconds=None,
        state_store=None,
        redis_socket_timeout=None,
        tool_executor=None,
        resource_capacity=None,
        enforce_capacity=False,
        max_attempts=None,
        max_concurrency=None,
    ):
        if redis_client is None:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError('Redis backend requires the redis package') from exc
            configured_socket_timeout = (
                redis_socket_timeout
                or os.environ.get('REDIS_SOCKET_TIMEOUT', '15')
            )
            try:
                socket_timeout = max(float(configured_socket_timeout), 6.0)
            except (TypeError, ValueError):
                socket_timeout = 15.0
            redis_client = redis.Redis.from_url(
                redis_url or os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'),
                decode_responses=True,
                socket_timeout=socket_timeout,
            )
        self.redis = redis_client
        self.namespace = namespace or os.environ.get('REDIS_NAMESPACE', 'bioagent')
        configured_lease = lease_seconds or os.environ.get('JOB_LEASE_SECONDS', '300')
        try:
            self.lease_seconds = max(int(configured_lease), 1)
        except (TypeError, ValueError):
            self.lease_seconds = 300
        configured_ttl = result_ttl_seconds or os.environ.get('JOB_RESULT_TTL_SECONDS', '86400')
        try:
            self.result_ttl_seconds = max(int(configured_ttl), 60)
        except (TypeError, ValueError):
            self.result_ttl_seconds = 86400
        self.worker_id = worker_id or f'worker-{uuid4().hex}'
        self.state_store = state_store
        self._tool_executor = tool_executor or InlineToolExecutor(
            lambda tool, arguments: run_tool(tool, arguments)
        )
        self.resource_capacity = resource_capacity or ResourceCapacity.from_env()
        self.enforce_capacity = bool(enforce_capacity)
        self.resource_pool = ResourcePool(self.resource_capacity)
        self._worker_executor = None
        try:
            self.max_attempts = max(int(
                max_attempts or os.environ.get('JOB_MAX_ATTEMPTS', '3')
            ), 1)
        except (TypeError, ValueError):
            self.max_attempts = 3
        try:
            self.max_concurrency = max(int(
                max_concurrency or os.environ.get('WORKER_MAX_CONCURRENCY', '2')
            ), 1)
        except (TypeError, ValueError):
            self.max_concurrency = 2
        self._lock = Lock()
        self._store = RedisQueueStore(
            self.redis,
            self.namespace,
            state_store=self.state_store,
            result_ttl_seconds=self.result_ttl_seconds,
        )
        self._metrics = RedisJobMetrics(self.namespace, self.backend)
        self._recovery = RedisLeaseRecovery(
            self._store,
            self._metrics,
            self._lock,
            self.worker_id,
            self.max_attempts,
        )
        self._coordinator = RedisExecutionCoordinator(
            self._store,
            self._metrics,
            self._recovery,
            self.worker_id,
            self.lease_seconds,
            self.resource_capacity,
            self.resource_pool,
            self.enforce_capacity,
        )
        self._worker_runtime = RedisJobWorkerRuntime(self)
        self.redis.ping()

    def _key(self, job_id):
        return self._store.key(job_id)

    @property
    def _index_key(self):
        return self._store.index_key

    @property
    def _queue_key(self):
        return self._store.queue_key

    @property
    def _high_queue_key(self):
        return self._store.high_queue_key

    @property
    def _low_queue_key(self):
        return self._store.low_queue_key

    @property
    def _queue_keys(self):
        return self._store.queue_keys

    def _queue_for_priority(self, priority):
        return self._store.queue_for_priority(priority)

    @property
    def _processing_key(self):
        return self._store.processing_key

    @property
    def _dead_letter_key(self):
        return self._store.dead_letter_key

    def _idempotency_key(self, value):
        return self._store.idempotency_key(value)

    def _execution_result_key(self, value):
        return self._store.execution_result_key(value)

    def _event_key(self, job_id):
        return self._store.event_key(job_id)

    @staticmethod
    def _public_record(record):
        return RedisQueueStore.public_record(record)

    def _save(self, record):
        self._store.save(record)

    def _load(self, job_id):
        return self._store.load(job_id)

    @staticmethod
    def _validate_tool_state(tool):
        known = {spec['name']: spec for spec in tool_specs()}
        if tool not in known:
            raise ValueError(f'unknown tool: {tool}')
        active = {spec['name']: spec for spec in active_tool_specs()}
        if tool not in active:
            raise ValueError(f'plugin domain is disabled for tool: {tool}')
        return active[tool]

    def _create_job(self, tool, arguments, resources, priority, retry_of=None,
                    idempotency_key=None, spec=None, parent_context=None):
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
            '_created_score': time(),
            '_execution_key': uuid4().hex,
            'resources': resources.as_dict(),
            'priority': priority,
            'trace_id': run_context['trace_id'],
            'run_context': run_context,
        }
        if run_context.get('request_id'):
            record['request_id'] = run_context['request_id']
        if retry_of:
            record['retry_of'] = retry_of
        if idempotency_key:
            record['idempotency_key'] = idempotency_key
            self._store.set_idempotent_job(idempotency_key, job_id)
        self._save(record)
        self._store.enqueue(job_id, priority)
        self._refresh_queue_metrics()
        self._metrics.queued(record)
        return self._public_record(record)

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
        distributed_lock = getattr(self.redis, 'lock', None)
        guard = (
            distributed_lock(
                f'{self.namespace}:jobs:submit-lock',
                timeout=10,
                blocking_timeout=10,
            )
            if distributed_lock
            else nullcontext()
        )
        with self._lock, guard:
            if idempotency_key:
                existing_id = self._store.get_idempotent_job(idempotency_key)
                if existing_id:
                    existing = self._load(existing_id)
                    if existing is not None:
                        if (
                            existing.get('tool') != tool
                            or existing.get('_arguments') != arguments
                            or existing.get('resources') != request.as_dict()
                            or existing.get('priority', 0) != priority
                        ):
                            raise ValueError(
                                'idempotency key already used with different job payload'
                            )
                        output = self._public_record(existing)
                        output['deduplicated'] = True
                        return output
            return self._create_job(
                tool,
                arguments,
                request,
                priority,
                idempotency_key=idempotency_key,
                spec=spec,
            )

    def get(self, job_id):
        record = self._load(str(job_id))
        return self._public_record(record) if record else None

    def subscribe_job_events(self, job_id):
        return self._store.subscribe_job_events(job_id)

    def list(self, limit=20):
        return self._store.list_records(limit)

    def cancel(self, job_id):
        record = self._load(str(job_id))
        if record is None:
            raise ValueError(f'job not found: {job_id}')
        if record.get('status') in TERMINAL_STATUSES:
            return self._public_record(record)
        record['_cancel_requested'] = True
        self._save(record)
        return self._public_record(record)

    def retry(self, job_id):
        record = self._load(str(job_id))
        if record is None:
            raise ValueError(f'job not found: {job_id}')
        if record.get('status') not in TERMINAL_STATUSES:
            raise ValueError('only completed, failed or cancelled jobs can be retried')
        arguments = record.get('_arguments')
        if arguments is None:
            raise ValueError('job arguments are unavailable')
        spec = self._validate_tool_state(record['tool'])
        arguments = resumable_retry_arguments(arguments, spec)
        return self._create_job(
            record['tool'],
            arguments,
            ResourceRequest.from_mapping(record.get('resources')),
            int(record.get('priority', 0)),
            retry_of=record['job_id'],
            spec=spec,
            parent_context=record.get('run_context'),
        )

    @staticmethod
    def _lease_active(record, now=None):
        return RedisLeaseRecovery.lease_active(record, now)

    def _claim(self, record):
        return self._coordinator.claim(record)

    def _ack(self, job_id):
        self._store.acknowledge(job_id)
        self._refresh_queue_metrics()

    def _queue_contains(self, job_id):
        return self._store.queue_contains(job_id)

    def _refresh_queue_metrics(self):
        return self._metrics.refresh_queue_depths(self._store)

    def _dead_letter(self, record, reason):
        return self._recovery.dead_letter(record, reason)

    def _load_execution_result(self, execution_key):
        return self._store.load_execution_result(execution_key)

    def _store_execution_result(self, execution_key, result):
        return self._store.store_execution_result(execution_key, result)

    def _finish(self, job_id, result, failed=False):
        return self._coordinator.finish(job_id, result, failed=failed)

    def recover_stale_jobs(self):
        return self._recovery.recover_stale_jobs()

    def run_job(self, job_id):
        return self._coordinator.run_job(
            job_id,
            self._validate_tool_state,
            self._execute_claimed_job,
        )

    def _run_job(self, job_id):
        return self._coordinator.execute(job_id, self._execute_claimed_job)

    def _execute_claimed_job(self, record, job_id):
        return self._worker_runtime.execute_claimed_job(record, job_id)

    def _next_job(self):
        return self._worker_runtime.next_job()

    def _complete_queued_item(self, job_id, poll_timeout):
        return self._worker_runtime.complete_queued_item(job_id, poll_timeout)

    def run_forever(self, poll_timeout=5):
        return self._worker_runtime.run_forever(poll_timeout)

    def resource_status(self):
        return {
            'capacity': self.resource_capacity.as_dict(),
            'enforced': self.enforce_capacity,
            'max_concurrency': self.max_concurrency,
            'max_attempts': self.max_attempts,
            'resources': self.resource_pool.snapshot(),
            'queues': self._store.priority_depths(),
        }

    def shutdown(self):
        shutdown = getattr(self._tool_executor, 'shutdown', None)
        if shutdown:
            shutdown()
        if self._worker_executor is not None:
            self._worker_executor.shutdown(wait=True, cancel_futures=True)
            self._worker_executor = None
        close = getattr(self.redis, 'close', None)
        if close:
            close()
