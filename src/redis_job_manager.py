"""Redis-backed job state and queue for horizontally scalable workers."""

from datetime import datetime, timezone
from contextlib import nullcontext
import math
from threading import RLock
from uuid import uuid4

try:
    from .domain_registry import active_tool_specs, run_tool, tool_specs
    from .execution_semantics import validate_required_artifacts
    from .job_execution import InlineToolExecutor
    from .job_manager import TERMINAL_STATUSES
    from .execution_identity import (
        build_execution_identity,
        external_toolchain_snapshot,
        identity_mismatches,
        routing_descriptor,
    )
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
    from .redis_worker_registry import RedisWorkerRegistry
    from .run_context import build_run_context
    from .settings import PlatformSettings
except ImportError:
    from domain_registry import active_tool_specs, run_tool, tool_specs
    from execution_semantics import validate_required_artifacts
    from job_execution import InlineToolExecutor
    from job_manager import TERMINAL_STATUSES
    from execution_identity import (
        build_execution_identity,
        external_toolchain_snapshot,
        identity_mismatches,
        routing_descriptor,
    )
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
    from redis_worker_registry import RedisWorkerRegistry
    from run_context import build_run_context
    from settings import PlatformSettings


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
        capability_routing=None,
        require_execution_fingerprint=None,
        artifact_store=None,
        settings=None,
    ):
        self.settings = settings or PlatformSettings.from_env()
        if redis_client is None:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError('Redis backend requires the redis package') from exc
            configured_socket_timeout = redis_socket_timeout or self.settings.redis_socket_timeout
            try:
                socket_timeout = max(float(configured_socket_timeout), 6.0)
            except (TypeError, ValueError):
                socket_timeout = 15.0
            redis_client = redis.Redis.from_url(
                redis_url or self.settings.redis_url,
                decode_responses=True,
                socket_timeout=socket_timeout,
            )
        self.redis = redis_client
        self.namespace = namespace or self.settings.redis_namespace
        configured_lease = lease_seconds or self.settings.job_lease_seconds
        try:
            self.lease_seconds = max(int(configured_lease), 1)
        except (TypeError, ValueError):
            self.lease_seconds = 300
        configured_ttl = result_ttl_seconds or self.settings.job_result_ttl_seconds
        try:
            self.result_ttl_seconds = max(int(configured_ttl), 60)
        except (TypeError, ValueError):
            self.result_ttl_seconds = 86400
        self.worker_id = worker_id or f'worker-{uuid4().hex}'
        self.state_store = state_store
        self.artifact_store = artifact_store
        self._tool_executor = tool_executor or InlineToolExecutor(
            lambda tool, arguments: run_tool(tool, arguments)
        )
        self.resource_capacity = resource_capacity or ResourceCapacity.from_env()
        self.enforce_capacity = bool(enforce_capacity)
        self.capability_routing = (
            self.settings.worker_capability_routing
            if capability_routing is None else bool(capability_routing)
        )
        self.require_execution_fingerprint = (
            self.settings.worker_require_execution_fingerprint
            if require_execution_fingerprint is None
            else bool(require_execution_fingerprint)
        )
        self.resource_pool = ResourcePool(self.resource_capacity)
        self._execution_catalog = {}
        self._worker_executor = None
        try:
            self.max_attempts = max(int(
                max_attempts or self.settings.job_max_attempts
            ), 1)
        except (TypeError, ValueError):
            self.max_attempts = 3
        try:
            self.max_concurrency = max(int(
                max_concurrency or self.settings.worker_max_concurrency
            ), 1)
        except (TypeError, ValueError):
            self.max_concurrency = 2
        self._lock = RLock()
        self._store = RedisQueueStore(
            self.redis,
            self.namespace,
            state_store=self.state_store,
            result_ttl_seconds=self.result_ttl_seconds,
        )
        self._metrics = RedisJobMetrics(self.namespace, self.backend)
        self._worker_registry = RedisWorkerRegistry(
            self.redis,
            self.namespace,
            ttl_seconds=self.settings.worker_registry_ttl_seconds,
        )
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
            claim_provider=(
                getattr(self.state_store, 'claim_job', None)
                if getattr(self.state_store, 'require_job_scope', False)
                else None
            ),
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

    def _execution_identity(self, tool, spec=None):
        if tool not in self._execution_catalog:
            selected = spec or self._validate_tool_state(tool)
            self._execution_catalog[tool] = build_execution_identity(selected)
        return dict(self._execution_catalog[tool])

    def execution_catalog(self):
        for spec in active_tool_specs():
            self._execution_identity(spec['name'], spec)
        return {
            tool: dict(identity)
            for tool, identity in self._execution_catalog.items()
        }

    def worker_registry_record(self, active_jobs=0, draining=False):
        snapshot = self.resource_pool.snapshot()
        admission_reader = getattr(self.state_store, 'admission', None)
        admission = admission_reader() if admission_reader else {'accepting_work': True}
        domains = sorted({
            str(spec.get('domain') or 'unknown')
            for spec in active_tool_specs()
        })
        return {
            'worker_id': self.worker_id,
            'release_tag': self.settings.release_tag,
            'git_commit': self.settings.git_sha,
            'image_reference': self.settings.image_reference,
            'configuration': self.settings.public_snapshot(),
            'capacity': self.resource_capacity.as_dict(),
            'available_resources': snapshot['available'],
            'labels': list(self.resource_capacity.labels),
            'active_jobs': int(active_jobs),
            'max_concurrency': self.max_concurrency,
            'draining': bool(draining),
            'accepting_work': bool(admission.get('accepting_work')),
            'state_writer': admission,
            'execution_catalog': self.execution_catalog(),
            'toolchains': {
                domain: external_toolchain_snapshot(domain)
                for domain in domains
            },
        }

    def heartbeat_worker(self, active_jobs=0, draining=False):
        return self._worker_registry.heartbeat(
            self.worker_registry_record(active_jobs, draining),
            now=self._store.server_time(),
        )

    def list_workers(self):
        return self._worker_registry.list_active(
            now=self._store.server_time(),
        )

    def compatible_route_ids(self):
        compatible = []
        catalog = self.execution_catalog()
        for route_id in self._store.registered_route_ids():
            route = self._store.load_route(route_id)
            if not route:
                continue
            identity = catalog.get(route.get('tool')) or {}
            request = ResourceRequest.from_mapping(route.get('resources'))
            if (
                identity.get('fingerprint') == route.get('execution_fingerprint')
                and self.resource_capacity.fits(request)
            ):
                compatible.append(route_id)
        return tuple(compatible)

    def verify_execution_identity(self, record):
        expected = record.get('execution_identity')
        if not isinstance(expected, dict):
            return (
                not self.require_execution_fingerprint,
                ['missing_execution_identity'],
                None,
            )
        actual = self._execution_identity(record['tool'])
        mismatches = identity_mismatches(expected, actual)
        return not mismatches, mismatches, actual

    def defer_incompatible_execution(self, record, mismatches, actual):
        expected = record.get('execution_identity') or {}

        def defer(current, _server_now):
            if current.get('status') in TERMINAL_STATUSES:
                return None
            current['status'] = 'queued'
            current['scheduling'] = {
                'status': 'waiting_for_implementation',
                'worker_id': self.worker_id,
                'mismatches': list(mismatches),
                'expected_fingerprint': expected.get('fingerprint'),
                'actual_fingerprint': (actual or {}).get('fingerprint'),
            }
            return current

        updated, _ = self._store.atomic_update(record['job_id'], defer)
        return updated

    def _create_job(self, tool, arguments, resources, priority, retry_of=None,
                    idempotency_key=None, spec=None, parent_context=None,
                    dispatch=True, execution_identity=None, project_id=None,
                    persist=True, public=True):
        job_id = uuid4().hex
        execution_key = uuid4().hex
        identity = dict(
            execution_identity or self._execution_identity(tool, spec)
        )
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
            execution={
                'execution_key': execution_key,
                'idempotency_key': f'{job_id}:{execution_key}',
                'semantics': str((spec or {}).get('execution_semantics') or 'pure'),
            },
        ).as_dict()
        run_context['plugin'].update({
            'git_commit': identity.get('git_commit'),
            'worker_image_digest': identity.get('worker_image_digest'),
            'implementation_sha256': identity.get(
                'plugin_implementation_sha256'
            ),
            'external_toolchain_fingerprint': identity.get(
                'external_toolchain_fingerprint'
            ),
            'execution_fingerprint': identity.get('fingerprint'),
        })
        routing = routing_descriptor(tool, resources.as_dict(), identity)
        record = {
            'job_id': job_id,
            'tool': tool,
            'status': 'queued',
            'created_at': _now(),
            '_arguments': dict(arguments),
            '_cancel_requested': False,
            '_created_score': self._store.server_time(),
            '_execution_key': execution_key,
            'execution_semantics': str(
                (spec or {}).get('execution_semantics') or 'pure'
            ),
            'resources': resources.as_dict(),
            'priority': priority,
            'trace_id': run_context['trace_id'],
            'run_context': run_context,
            'execution_identity': identity,
            'routing': routing,
            '_capability_routing': self.capability_routing,
            'project_id': project_id,
        }
        if self.capability_routing and not self._worker_registry.compatible_workers(
            record,
            now=self._store.server_time(),
        ):
            record['scheduling'] = {
                'status': 'waiting_for_capability',
                'route_id': routing['route_id'],
                'reason': 'no active worker advertises the pinned implementation and resources',
            }
        if run_context.get('request_id'):
            record['request_id'] = run_context['request_id']
        if retry_of:
            record['retry_of'] = retry_of
        if idempotency_key:
            record['idempotency_key'] = idempotency_key
        if persist and idempotency_key:
            self._store.set_idempotent_job(idempotency_key, job_id)
        if persist:
            self._store.save(record, persist_state=dispatch)
        if dispatch:
            if not persist:
                raise RuntimeError('dispatch requires a persisted Redis job record')
            self.dispatch(job_id)
        return self._public_record(record) if public else dict(record)

    def _submit(self, tool, arguments, idempotency_key=None, resources=None,
                priority=0, dispatch=True, project_id=None, persist=True,
                public=True):
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
        validate_required_artifacts(arguments, spec)
        request = merge_requests(spec.get('resources'), resources)
        priority = normalize_priority(priority)
        distributed_lock = getattr(self.redis, 'lock', None) if persist else None
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
            if persist and idempotency_key:
                existing_id = self._store.get_idempotent_job(idempotency_key)
                if existing_id:
                    existing = self._load(existing_id)
                    if existing is not None:
                        if (
                            existing.get('tool') != tool
                            or existing.get('_arguments') != arguments
                            or existing.get('resources') != request.as_dict()
                            or existing.get('priority', 0) != priority
                            or existing.get('project_id') != project_id
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
                dispatch=dispatch,
                project_id=project_id,
                persist=persist,
                public=public,
            )

    def submit(self, tool, arguments, idempotency_key=None, resources=None,
               priority=0, project_id=None):
        return self._submit(
            tool,
            arguments,
            idempotency_key=idempotency_key,
            resources=resources,
            priority=priority,
            dispatch=True,
            project_id=project_id,
        )

    def prepare(self, tool, arguments, idempotency_key=None, resources=None,
                priority=0, project_id=None):
        return self._submit(
            tool,
            arguments,
            idempotency_key=idempotency_key,
            resources=resources,
            priority=priority,
            dispatch=False,
            project_id=project_id,
        )

    def prepare_durable(self, tool, arguments, idempotency_key=None,
                        resources=None, priority=0, project_id=None):
        return self._submit(
            tool,
            arguments,
            idempotency_key=idempotency_key,
            resources=resources,
            priority=priority,
            dispatch=False,
            project_id=project_id,
            persist=False,
            public=False,
        )

    def dispatch(self, job_id):
        job_id = str(job_id)
        distributed_lock = getattr(self.redis, 'lock', None)
        guard = (
            distributed_lock(
                f'{self.namespace}:jobs:rebuild:{job_id}',
                timeout=10,
                blocking_timeout=1,
            )
            if distributed_lock else nullcontext()
        )
        with self._lock, guard:
            record = self._load(job_id)
            if record is None:
                raise ValueError(f'job not found: {job_id}')
            if record.get('status') != 'queued':
                return self._public_record(record)
            if float(record.get('_retry_not_before') or 0) > self._store.server_time():
                return self._public_record(record)
            route_id = None
            if record.get('_capability_routing'):
                route_id = self._store.register_route(record)
            if (
                not self._store.queue_contains(job_id)
                and not self._store.processing_contains(job_id)
            ):
                self._store.enqueue(
                    job_id,
                    int(record.get('priority', 0)),
                    route_id=route_id,
                )
                self._metrics.queued(record)
        self._refresh_queue_metrics()
        return self._public_record(record)

    def get(self, job_id):
        record = self._load(str(job_id))
        return self._public_record(record) if record else None

    def durable_record(self, job_id):
        record = self._load(str(job_id))
        return dict(record) if record is not None else None

    def discard_prepared(
        self,
        job_id,
        idempotency_key=None,
        replacement_job_id=None,
    ):
        self._store.discard_prepared(
            job_id,
            idempotency_key=idempotency_key,
            replacement_job_id=replacement_job_id,
        )

    def rebuild_durable_queue(self, limit=1000, loader=None):
        loader = loader or getattr(self.state_store, 'load_dispatchable', None)
        if loader is None:
            return []
        rebuilt = []
        for durable in loader(limit=limit):
            job_id = str(durable.get('job_id', ''))
            if not job_id:
                continue
            distributed_lock = getattr(self.redis, 'lock', None)
            guard = (
                distributed_lock(
                    f'{self.namespace}:jobs:rebuild:{job_id}',
                    timeout=10,
                    blocking_timeout=1,
                )
                if distributed_lock else nullcontext()
            )
            with self._lock, guard:
                existing = self._load(job_id)
                if existing is not None:
                    deferred_attempt = durable.get('_deferred_attempt')
                    if (
                        deferred_attempt is not None
                        and durable.get('status') == 'queued'
                        and int(existing.get('_attempts', 0)) <= int(deferred_attempt)
                        and existing.get('status') != 'queued'
                    ):
                        def restore_deferred(current, _server_now):
                            if int(current.get('_attempts', 0)) > int(deferred_attempt):
                                return None
                            current['status'] = 'queued'
                            current['_attempts'] = int(deferred_attempt)
                            current['_revision'] = max(
                                int(current.get('_revision', 0)),
                                int(durable.get('_revision', 0)),
                            )
                            current['_retry_not_before'] = durable['_retry_not_before']
                            current['scheduling'] = dict(durable.get('scheduling') or {})
                            for key in (
                                'started_at', 'finished_at', 'result', 'error',
                                '_worker_id', '_lease_until', '_fencing_token',
                                '_started_epoch',
                            ):
                                current.pop(key, None)
                            return current

                        existing, restored = self._store.atomic_update(
                            job_id, restore_deferred, persist_state=False
                        )
                        if restored:
                            self._metrics.deferred_reconciled(
                                durable, 'stale_cache', self._store.server_time()
                            )
                    claim_ticket = durable.get('_claim_ticket')
                    if claim_ticket and existing.get('status') == 'queued':
                        def refresh_claim_ticket(current, _server_now):
                            if current.get('status') != 'queued':
                                return None
                            current['_claim_ticket'] = str(claim_ticket)
                            current['_revision'] = max(
                                int(current.get('_revision', 0)),
                                int(durable.get('_revision', 0)),
                            )
                            current['_dispatch_generation'] = int(
                                durable.get('_dispatch_generation') or 0
                            )
                            current.pop('_retry_not_before', None)
                            current.pop('_deferred_attempt', None)
                            if (
                                (current.get('scheduling') or {}).get('status')
                                == 'waiting_for_external_service'
                            ):
                                current.pop('scheduling', None)
                            return current

                        refreshed, changed = self._store.atomic_update(
                            job_id,
                            refresh_claim_ticket,
                        )
                        if changed:
                            existing = refreshed
                    saver = getattr(self.state_store, 'save', None)
                    if saver is not None:
                        saver(existing)
                    if (
                        existing.get('status') == 'queued'
                        and float(existing.get('_retry_not_before') or 0)
                        <= self._store.server_time()
                        and not self._store.queue_contains(job_id)
                        and not self._store.processing_contains(job_id)
                    ):
                        route_id = (
                            self._store.register_route(existing)
                            if existing.get('_capability_routing') else None
                        )
                        self._store.enqueue(
                            job_id,
                            int(existing.get('priority', 0)),
                            route_id=route_id,
                        )
                        rebuilt.append(job_id)
                    continue
                record = dict(durable)
                record.update({
                    'status': 'queued',
                    'recovered_at': _now(),
                })
                for key in (
                    'started_at', 'finished_at', 'error', '_worker_id',
                    '_lease_until', '_fencing_token', '_started_epoch',
                ):
                    record.pop(key, None)
                record.setdefault('_execution_key', uuid4().hex)
                record.setdefault('_attempts', 0)
                record.setdefault('_cancel_requested', False)
                record.setdefault('_created_score', self._store.server_time())
                was_deferred = record.get('_deferred_attempt') is not None
                if record.get('_claim_ticket'):
                    record.pop('_retry_not_before', None)
                    record.pop('_deferred_attempt', None)
                    if (
                        (record.get('scheduling') or {}).get('status')
                        == 'waiting_for_external_service'
                    ):
                        record.pop('scheduling', None)
                self._save(record)
                if was_deferred:
                    self._metrics.deferred_reconciled(
                        record, 'redis_loss', self._store.server_time()
                    )
                if float(record.get('_retry_not_before') or 0) > self._store.server_time():
                    continue
                route_id = (
                    self._store.register_route(record)
                    if record.get('_capability_routing') else None
                )
                self._store.enqueue(
                    job_id,
                    int(record.get('priority', 0)),
                    route_id=route_id,
                )
                rebuilt.append(job_id)
        self._refresh_queue_metrics()
        return rebuilt

    def subscribe_job_events(self, job_id):
        return self._store.subscribe_job_events(job_id)

    def read_job_events(self, job_id, last_event_id='0-0', block_ms=1000, count=100):
        return self._store.read_job_events(
            job_id,
            last_event_id=last_event_id,
            block_ms=block_ms,
            count=count,
        )

    def list(self, limit=20):
        return self._store.list_records(limit)

    def cancel(self, job_id):
        job_id = str(job_id)
        record = self._load(job_id)
        if record is None:
            raise ValueError(f'job not found: {job_id}')
        if record.get('status') in TERMINAL_STATUSES:
            return self._public_record(record)

        def request_cancel(current, _server_now):
            if current.get('status') in TERMINAL_STATUSES:
                return None
            if (
                current.get('status') == 'queued'
                and (current.get('scheduling') or {}).get('status')
                == 'waiting_for_external_service'
            ):
                current.update({
                    'status': 'cancelled',
                    'finished_at': _now(),
                    'error': 'job cancelled by user',
                    '_cancel_requested': True,
                })
                current.pop('_retry_not_before', None)
                return current
            current['_cancel_requested'] = True
            return current

        record, _ = self._store.atomic_update(job_id, request_cancel)
        return self._public_record(record)

    def _retry(self, job_id, migrate_implementation=False, dispatch=True,
               persist=True, public=True):
        record = self._load(str(job_id))
        if record is None:
            raise ValueError(f'job not found: {job_id}')
        if record.get('status') not in TERMINAL_STATUSES:
            raise ValueError(
                'only completed, failed, cancelled or indeterminate jobs can be retried'
            )
        if (
            record.get('status') == 'indeterminate'
            and (record.get('resolution') or {}).get('decision') != 'approve_retry'
        ):
            raise ValueError(
                'indeterminate job requires an approve_retry resolution before retry'
            )
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
            execution_identity=(
                None
                if migrate_implementation
                else record.get('execution_identity')
            ),
            dispatch=dispatch,
            project_id=record.get('project_id'),
            persist=persist,
            public=public,
        )

    def retry(self, job_id, migrate_implementation=False):
        return self._retry(
            job_id,
            migrate_implementation=migrate_implementation,
            dispatch=True,
        )

    def prepare_retry(self, job_id, migrate_implementation=False):
        return self._retry(
            job_id,
            migrate_implementation=migrate_implementation,
            dispatch=False,
        )

    def prepare_durable_retry(self, job_id, migrate_implementation=False):
        return self._retry(
            job_id,
            migrate_implementation=migrate_implementation,
            dispatch=False,
            persist=False,
            public=False,
        )

    def resolve_indeterminate(self, job_id, decision, reason, reviewer, evidence=None):
        return self._coordinator.resolve_indeterminate(
            str(job_id),
            decision,
            reason,
            reviewer,
            evidence=evidence,
        )

    def _lease_active(self, record, now=None):
        lease_now = self._store.server_time() if now is None else now
        return RedisLeaseRecovery.lease_active(record, lease_now)

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

    def _load_execution_result(
        self,
        execution_key,
        job_id=None,
        fencing_token=None,
        attempt=None,
    ):
        return self._store.load_execution_result(
            execution_key,
            job_id=job_id,
            fencing_token=fencing_token,
            worker_id=self.worker_id,
            attempt=attempt,
        )

    def _begin_execution_attempt(
        self,
        execution_key,
        job_id,
        fencing_token,
        attempt,
        semantics='pure',
    ):
        begin = getattr(self.state_store, 'begin_execution_attempt', None)
        if begin is None:
            return None
        return self._store._call_with_supported_keywords(
            begin,
            execution_key,
            job_id,
            fencing_token,
            attempt,
            semantics,
            worker_id=self.worker_id,
        )

    def _store_execution_result(
        self,
        execution_key,
        result,
        job_id=None,
        fencing_token=None,
        attempt=None,
        publication_ids=None,
    ):
        return self._store.store_execution_result(
            execution_key,
            result,
            job_id=job_id,
            fencing_token=fencing_token,
            worker_id=self.worker_id,
            attempt=attempt,
            publication_ids=publication_ids,
        )

    def _artifact_state_call(
        self,
        method,
        record,
        fencing_token,
        values,
        *,
        error=None,
    ):
        callback = getattr(self.state_store, method, None)
        if callback is None or not values:
            return None
        arguments = (
            record['job_id'],
            record['_execution_key'],
            fencing_token,
            int(record.get('_attempts', 0)),
            self.worker_id,
            values,
        )
        if method == 'reserve_artifacts':
            arguments = (
                record['job_id'],
                record.get('project_id') or 'system-legacy',
                record['_execution_key'],
                fencing_token,
                int(record.get('_attempts', 0)),
                self.worker_id,
                values,
            )
        if method == 'orphan_artifacts':
            return callback(*arguments, error=error)
        return callback(*arguments)

    def _reserve_artifacts(self, record, fencing_token, artifacts):
        return self._artifact_state_call(
            'reserve_artifacts', record, fencing_token, artifacts
        )

    def _mark_artifacts_uploaded(self, record, fencing_token, artifacts):
        return self._artifact_state_call(
            'mark_artifacts_uploaded', record, fencing_token, artifacts
        )

    def _commit_artifacts(self, record, fencing_token, artifacts):
        publication_ids = [
            item['publication_id'] for item in artifacts
            if isinstance(item, dict) and item.get('publication_id')
        ]
        return self._artifact_state_call(
            'commit_artifacts', record, fencing_token, publication_ids
        )

    def _orphan_artifacts(self, record, fencing_token, artifacts, error=None):
        publication_ids = [
            item['publication_id'] if isinstance(item, dict) else str(item)
            for item in artifacts
            if (isinstance(item, dict) and item.get('publication_id'))
            or (not isinstance(item, dict) and str(item))
        ]
        return self._artifact_state_call(
            'orphan_artifacts',
            record,
            fencing_token,
            publication_ids,
            error=error,
        )

    def _finish(self, job_id, result, failed=False, artifacts=None,
                fencing_token=None):
        return self._coordinator.finish(
            job_id,
            result,
            failed=failed,
            artifacts=artifacts,
            fencing_token=fencing_token,
        )

    def _mark_indeterminate(self, job_id, reason, fencing_token=None):
        return self._coordinator.mark_indeterminate(
            job_id,
            reason,
            fencing_token=fencing_token,
        )

    def defer_external_retry(self, record, delay_seconds):
        callback = getattr(self.state_store, 'defer_pure_job', None)
        if callback is None or record.get('execution_semantics') != 'pure':
            return None
        delay = float(delay_seconds)
        if not math.isfinite(delay) or not 0 < delay <= 604800:
            return None
        attempt = int(record.get('_attempts', 0))
        if attempt >= self.max_attempts:
            return None
        job_id = str(record['job_id'])
        fencing_token = str(record['_fencing_token'])
        deferred = callback(
            record['_execution_key'],
            job_id,
            fencing_token,
            attempt,
            self.worker_id,
            delay,
            self.max_attempts,
            int(record.get('_revision', 0)),
        )
        if not deferred:
            return None
        retry_at = float(deferred['retry_at'])

        def transition(current, _server_now):
            if (
                current.get('status') != 'running'
                or current.get('_worker_id') != self.worker_id
                or str(current.get('_fencing_token')) != fencing_token
            ):
                return None
            current['status'] = 'queued'
            current['_revision'] = max(
                int(current.get('_revision', 0)),
                int(deferred.get('revision') or 0),
            )
            current['_retry_not_before'] = retry_at
            current['_deferred_attempt'] = attempt
            current['scheduling'] = {
                'status': 'waiting_for_external_service',
                'retry_at': retry_at,
            }
            for key in (
                'started_at', '_worker_id', '_lease_until',
                '_fencing_token', '_started_epoch',
            ):
                current.pop(key, None)
            return current

        try:
            current, _ = self._store.atomic_update(
                job_id, transition, persist_state=False
            )
        except Exception as exc:
            self._metrics.deferred_cache_sync_failed(job_id, exc)
            return {
                **self._public_record(record),
                'status': 'queued',
                'scheduling': {
                    'status': 'waiting_for_external_service',
                    'retry_at': retry_at,
                },
            }
        return self._public_record(current) if current else None

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

    def run_forever(self, poll_timeout=5, stop_event=None, drain_timeout_seconds=120):
        return self._worker_runtime.run_forever(
            poll_timeout,
            stop_event=stop_event,
            drain_timeout_seconds=drain_timeout_seconds,
        )

    def resource_status(self):
        return {
            'capacity': self.resource_capacity.as_dict(),
            'enforced': self.enforce_capacity,
            'max_concurrency': self.max_concurrency,
            'max_attempts': self.max_attempts,
            'resources': self.resource_pool.snapshot(),
            'queues': self._store.priority_depths(),
        }

    def ping(self):
        if not self.redis.ping():
            raise RuntimeError('Redis is unavailable')

    def shutdown(self):
        try:
            self._worker_registry.unregister(self.worker_id)
        except Exception:
            pass
        shutdown = getattr(self._tool_executor, 'shutdown', None)
        if shutdown:
            shutdown()
        if self._worker_executor is not None:
            self._worker_executor.shutdown(wait=True, cancel_futures=True)
            self._worker_executor = None
        close = getattr(self.redis, 'close', None)
        if close:
            close()
