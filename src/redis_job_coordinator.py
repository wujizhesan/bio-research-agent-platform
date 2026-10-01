"""Execution-state coordination and resource admission for Redis jobs."""

from contextlib import nullcontext
from datetime import datetime, timezone

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
                 resource_capacity, resource_pool, enforce_capacity=False,
                 claim_provider=None):
        self.store = store
        self.metrics = metrics
        self.recovery = recovery
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.resource_capacity = resource_capacity
        self.resource_pool = resource_pool
        self.enforce_capacity = bool(enforce_capacity)
        self.claim_provider = claim_provider

    def claim(self, record):
        job_id = str(record['job_id'])
        distributed_lock = getattr(self.store.redis, 'lock', None)
        guard = (
            distributed_lock(
                f'{self.store.namespace}:jobs:claim:{job_id}',
                timeout=10,
                blocking_timeout=5,
            )
            if distributed_lock else nullcontext()
        )
        with guard:
            current = self.store.load(job_id)
            if current is None or current.get('status') in TERMINAL_STATUSES:
                return current
            if current.get('_cancel_requested'):
                return current
            if float(current.get('_retry_not_before') or 0) > self.store.server_time():
                return current
            lease_until = current.get('_lease_until')
            server_now = self.store.server_time()
            if (
                current.get('status') == 'running'
                and lease_until is not None
                and float(lease_until) > server_now
            ):
                return current
            expected_revision = int(current.get('_revision', 0))
            if self.claim_provider is not None:
                claim = self.claim_provider(
                    job_id,
                    current.get('_execution_key'),
                    self.worker_id,
                    current.get('_claim_ticket'),
                    self.lease_seconds,
                )
                if not claim:
                    return current
                fencing_token = str(claim['fencing_token'])
                claim_attempt = int(claim['attempt'])
            else:
                fencing_token = self.store.next_fencing_token()
                claim_attempt = int(current.get('_attempts', 0)) + 1

            def update(latest, update_time):
                if int(latest.get('_revision', 0)) != expected_revision:
                    return None
                if latest.get('status') in TERMINAL_STATUSES:
                    return None
                if latest.get('_cancel_requested'):
                    return None
                active_until = latest.get('_lease_until')
                if (
                    latest.get('status') == 'running'
                    and active_until is not None
                    and float(active_until) > update_time
                ):
                    return None
                latest.pop('scheduling', None)
                latest.update({
                    'status': 'running',
                    'started_at': _now(),
                    '_worker_id': self.worker_id,
                    '_lease_until': update_time + self.lease_seconds,
                    '_fencing_token': fencing_token,
                    '_attempts': claim_attempt,
                    '_started_epoch': update_time,
                })
                return latest

            claimed, changed = self.store.atomic_update(job_id, update)
        if changed:
            self.metrics.claimed(claimed, self.worker_id)
        return claimed

    def finish(self, job_id, result, failed=False, artifacts=None,
               fencing_token=None):
        update = {
            'status': 'failed' if failed else 'completed',
            'finished_at': _now(),
            'result': result,
        }
        if artifacts:
            update['artifacts'] = [dict(item) for item in artifacts]
        if failed:
            update.update({
                'error': result.get('error', 'tool returned an error'),
                'error_code': result.get('error_code', 'tool_execution_failed'),
                'dead_lettered_at': _now(),
                'dead_letter_reason': 'execution_failed',
            })
        elapsed = None

        def transition(current, server_now):
            nonlocal elapsed
            if current.get('status') in TERMINAL_STATUSES:
                return None
            if current.get('status') != 'running':
                return None
            if current.get('_worker_id') != self.worker_id:
                return None
            if fencing_token is None or str(
                current.get('_fencing_token', '')
            ) != str(fencing_token):
                return None
            final_update = update
            if (
                current.get('_cancel_requested')
                and current.get('execution_semantics') == 'side_effecting'
                and not failed
            ):
                final_update = dict(update)
                final_update['cancellation_too_late'] = True
            elif current.get('_cancel_requested'):
                final_update = {
                    'status': 'cancelled',
                    'finished_at': _now(),
                    'error': 'job cancelled by user',
                }
            current.update(final_update)
            current['execution'] = {
                'worker_id': self.worker_id,
                'fencing_token': str(fencing_token),
                'identity': dict(current.get('execution_identity') or {}),
            }
            started_epoch = current.pop('_started_epoch', None)
            if started_epoch is not None:
                try:
                    elapsed = max(server_now - float(started_epoch), 0)
                except (TypeError, ValueError):
                    pass
            current.pop('_worker_id', None)
            current.pop('_lease_until', None)
            current.pop('_fencing_token', None)
            return current

        current, changed = self.store.atomic_update(job_id, transition)
        if current is None:
            return None
        if not changed:
            return self.store.public_record(current)
        if current['status'] == 'failed':
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

    def mark_indeterminate(self, job_id, reason, fencing_token=None):
        def transition(current, _server_now):
            if current.get('status') in TERMINAL_STATUSES:
                return None
            if current.get('status') != 'running':
                return None
            if current.get('_worker_id') != self.worker_id:
                return None
            if fencing_token is None or str(
                current.get('_fencing_token', '')
            ) != str(fencing_token):
                return None
            current.update({
                'status': 'indeterminate',
                'finished_at': _now(),
                'error_code': 'execution_indeterminate',
                'error': str(reason),
                'indeterminate': {
                    'requires_manual_review': True,
                    'execution_key': current.get('_execution_key'),
                    'attempt': int(current.get('_attempts', 0)),
                    'semantics': current.get('execution_semantics'),
                },
                'execution': {
                    'worker_id': self.worker_id,
                    'fencing_token': str(fencing_token),
                    'identity': dict(current.get('execution_identity') or {}),
                },
            })
            current.pop('_worker_id', None)
            current.pop('_lease_until', None)
            current.pop('_fencing_token', None)
            current.pop('_started_epoch', None)
            return current

        current, changed = self.store.atomic_update(job_id, transition)
        if current is None:
            return None
        if changed:
            self.metrics.finished(current, None, self.worker_id)
            self.metrics.refresh_queue_depths(self.store)
        return self.store.public_record(current)

    def resolve_indeterminate(
        self,
        job_id,
        decision,
        reason,
        reviewer,
        evidence=None,
    ):
        allowed = {'confirm_succeeded', 'confirm_failed', 'approve_retry'}
        if decision not in allowed:
            raise ValueError('invalid indeterminate job resolution decision')

        def transition(current, _server_now):
            if current.get('status') != 'indeterminate':
                raise ValueError('only indeterminate jobs can be resolved')
            if current.get('resolution'):
                raise ValueError('indeterminate job has already been resolved')
            resolution = {
                'decision': decision,
                'reason': str(reason),
                'evidence': dict(evidence or {}),
                'reviewer': str(reviewer),
                'resolved_at': _now(),
            }
            current['resolution'] = resolution
            current['indeterminate'] = {
                **dict(current.get('indeterminate') or {}),
                'requires_manual_review': False,
            }
            if decision == 'confirm_succeeded':
                current['status'] = 'completed'
                current['result'] = {
                    'status': 'confirmed_external_success',
                    'resolution': resolution,
                }
                current.pop('error', None)
            elif decision == 'confirm_failed':
                current['status'] = 'failed'
                current['error'] = str(reason)
            return current

        current, changed = self.store.atomic_update(job_id, transition)
        if current is None:
            raise ValueError(f'job not found: {job_id}')
        if changed:
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
            def attach_context(current, _server_now):
                if current.get('run_context') is not None:
                    return None
                current['run_context'] = run_context
                current['trace_id'] = run_context['trace_id']
                return current

            record, _ = self.store.atomic_update(job_id, attach_context)
            run_context = record.get('run_context', run_context)
        with bind_run_context(run_context):
            return self.execute(job_id, execute_claimed_job)

    def execute(self, job_id, execute_claimed_job):
        record = self.store.load(str(job_id))
        if record is None or record.get('status') in TERMINAL_STATUSES:
            return self.store.public_record(record) if record else None
        if (
            record.get('status') == 'running'
            and self.recovery.lease_active(
                record,
                self.store.server_time(),
            )
        ):
            return self.store.public_record(record)
        if record.get('_cancel_requested'):
            def cancel(current, _server_now):
                if current.get('status') in TERMINAL_STATUSES:
                    return None
                current.update({
                    'status': 'cancelled',
                    'finished_at': _now(),
                    'error': 'job cancelled by user',
                })
                return current

            record, _ = self.store.atomic_update(job_id, cancel)
            return self.store.public_record(record)
        if float(record.get('_retry_not_before') or 0) > self.store.server_time():
            return self.store.public_record(record)
        resources = ResourceRequest.from_mapping(record.get('resources'))
        if (
            self.enforce_capacity
            and not self.resource_capacity.fits(resources)
        ):
            scheduling = {
                'status': 'waiting_for_compatible_worker',
                'worker_id': self.worker_id,
                'reason': self.resource_capacity.rejection_reason(resources),
            }
            record, _ = self.store.atomic_update(
                job_id,
                lambda current, _now: (
                    {**current, 'scheduling': scheduling}
                    if not current.get('_cancel_requested') else None
                ),
            )
            return self.store.public_record(record)
        acquired = (
            not self.enforce_capacity
            or self.resource_pool.try_acquire(resources)
        )
        if not acquired:
            capacity_rejected = getattr(self.metrics, 'capacity_rejected', None)
            if capacity_rejected is not None:
                capacity_rejected(record)
            scheduling = {
                'status': 'waiting_for_worker_capacity',
                'worker_id': self.worker_id,
                'reason': 'compatible worker resources are currently reserved',
            }
            record, _ = self.store.atomic_update(
                job_id,
                lambda current, _now: (
                    {**current, 'scheduling': scheduling}
                    if not current.get('_cancel_requested') else None
                ),
            )
            return self.store.public_record(record)
        try:
            return execute_claimed_job(record, job_id)
        finally:
            if self.enforce_capacity:
                self.resource_pool.release(resources)
