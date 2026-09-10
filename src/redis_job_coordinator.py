"""Execution-state coordination and resource admission for Redis jobs."""

from datetime import datetime, timezone
from time import time

try:
    from .job_manager import TERMINAL_STATUSES
    from .resource_scheduling import ResourceRequest
    from .run_context import bind_run_context, build_run_context
except ImportError:
    from job_manager import TERMINAL_STATUSES
    from resource_scheduling import ResourceRequest
    from run_context import bind_run_context, build_run_context


def _now():
    return datetime.now(timezone.utc).isoformat()


class RedisExecutionCoordinator:
    def __init__(self, store, metrics, recovery, worker_id, lease_seconds,
                 resource_capacity, resource_pool, enforce_capacity=False):
        self.store = store
        self.metrics = metrics
        self.recovery = recovery
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.resource_capacity = resource_capacity
        self.resource_pool = resource_pool
        self.enforce_capacity = bool(enforce_capacity)

    def claim(self, record):
        record.pop('scheduling', None)
        record.update({
            'status': 'running',
            'started_at': _now(),
            '_worker_id': self.worker_id,
            '_lease_until': time() + self.lease_seconds,
            '_attempts': int(record.get('_attempts', 0)) + 1,
            '_started_epoch': time(),
        })
        self.store.save(record)
        self.metrics.claimed(record, self.worker_id)

    def finish(self, job_id, result, failed=False):
        update = {
            'status': 'failed' if failed else 'completed',
            'finished_at': _now(),
            'result': result,
        }
        if failed:
            update['error'] = result.get('error', 'tool returned an error')
        current = self.store.load(str(job_id))
        if current is None:
            return None
        if (
            current.get('status') == 'running'
            and current.get('_worker_id') not in (None, self.worker_id)
            and self.recovery.lease_active(current)
        ):
            return self.store.public_record(current)
        if current.get('_cancel_requested'):
            update = {
                'status': 'cancelled',
                'finished_at': _now(),
                'error': 'job cancelled by user',
            }
        current.update(update)
        started_epoch = current.pop('_started_epoch', None)
        elapsed = None
        if started_epoch is not None:
            try:
                elapsed = max(time() - float(started_epoch), 0)
            except (TypeError, ValueError):
                pass
        current.pop('_worker_id', None)
        current.pop('_lease_until', None)
        self.store.save(current)
        if current['status'] == 'failed':
            current['dead_lettered_at'] = _now()
            current['dead_letter_reason'] = 'execution_failed'
            self.store.save(current)
            self.store.move_to_dead_letter(job_id)
            self.metrics.dead_lettered(
                current,
                'execution_failed',
                self.worker_id,
                transition=False,
            )
        self.metrics.finished(current, elapsed, self.worker_id)
        self.metrics.refresh_queue_depths(self.store)
        return self.store.public_record(current)

    def run_job(self, job_id, validate_tool_state, execute_claimed_job):
        record = self.store.load(str(job_id))
        if record is None:
            return None
        run_context = record.get('run_context')
        if run_context is None:
            spec = validate_tool_state(record['tool'])
            run_context = build_run_context(
                record['tool'],
                record.get('_arguments', {}),
                spec=spec,
                resources=record.get('resources', {}),
                priority=record.get('priority', 0),
                job_id=record.get('job_id'),
                trace_id=record.get('trace_id'),
                request_id=record.get('request_id'),
            ).as_dict()
            record['run_context'] = run_context
            record['trace_id'] = run_context['trace_id']
            self.store.save(record)
        with bind_run_context(run_context):
            return self.execute(job_id, execute_claimed_job)

    def execute(self, job_id, execute_claimed_job):
        record = self.store.load(str(job_id))
        if record is None or record.get('status') in TERMINAL_STATUSES:
            return self.store.public_record(record) if record else None
        if (
            record.get('status') == 'running'
            and self.recovery.lease_active(record)
        ):
            return self.store.public_record(record)
        if record.get('_cancel_requested'):
            record.update({
                'status': 'cancelled',
                'finished_at': _now(),
                'error': 'job cancelled by user',
            })
            self.store.save(record)
            return self.store.public_record(record)
        resources = ResourceRequest.from_mapping(record.get('resources'))
        if (
            self.enforce_capacity
            and not self.resource_capacity.fits(resources)
        ):
            record['scheduling'] = {
                'status': 'waiting_for_compatible_worker',
                'worker_id': self.worker_id,
                'reason': self.resource_capacity.rejection_reason(resources),
            }
            self.store.save(record)
            return self.store.public_record(record)
        acquired = (
            not self.enforce_capacity
            or self.resource_pool.try_acquire(resources)
        )
        if not acquired:
            record['scheduling'] = {
                'status': 'waiting_for_worker_capacity',
                'worker_id': self.worker_id,
                'reason': 'compatible worker resources are currently reserved',
            }
            self.store.save(record)
            return self.store.public_record(record)
        try:
            return execute_claimed_job(record, job_id)
        finally:
            if self.enforce_capacity:
                self.resource_pool.release(resources)
