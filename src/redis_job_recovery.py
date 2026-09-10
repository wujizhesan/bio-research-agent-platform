"""Lease expiry recovery and dead-letter transitions for Redis jobs."""

from contextlib import nullcontext
from datetime import datetime, timezone
from time import time

try:
    from .job_manager import TERMINAL_STATUSES
except ImportError:
    from job_manager import TERMINAL_STATUSES


def _now():
    return datetime.now(timezone.utc).isoformat()


class RedisLeaseRecovery:
    def __init__(self, store, metrics, lock, worker_id, max_attempts):
        self.store = store
        self.metrics = metrics
        self.lock = lock
        self.worker_id = worker_id
        self.max_attempts = max_attempts

    @staticmethod
    def lease_active(record, now=None):
        lease_until = record.get('_lease_until')
        if lease_until is None:
            return False
        try:
            return float(lease_until) > (now or time())
        except (TypeError, ValueError):
            return False

    def dead_letter(self, record, reason):
        job_id = str(record['job_id'])
        record.update({
            'status': 'failed',
            'finished_at': _now(),
            'dead_lettered_at': _now(),
            'dead_letter_reason': reason,
            'error': record.get('error') or reason,
        })
        record.pop('_worker_id', None)
        record.pop('_lease_until', None)
        record.pop('_started_epoch', None)
        self.store.save(record)
        self.store.move_to_dead_letter(job_id)
        self.store.acknowledge(job_id)
        self.metrics.dead_lettered(record, reason, self.worker_id)
        self.metrics.refresh_queue_depths(self.store)
        return self.store.public_record(record)

    def recover_stale_jobs(self):
        processing_ids = self.store.redis.lrange(
            self.store.processing_key,
            0,
            -1,
        )
        recovered = []
        now = time()
        for raw_job_id in processing_ids:
            job_id = (
                raw_job_id.decode('utf-8')
                if isinstance(raw_job_id, bytes)
                else str(raw_job_id)
            )
            distributed_lock = getattr(self.store.redis, 'lock', None)
            guard = (
                distributed_lock(
                    f'{self.store.namespace}:jobs:recover:{job_id}',
                    timeout=10,
                    blocking_timeout=1,
                )
                if distributed_lock
                else nullcontext()
            )
            try:
                with self.lock, guard:
                    record = self.store.load(job_id)
                    if record is None or record.get('status') in TERMINAL_STATUSES:
                        self.store.acknowledge(job_id)
                        continue
                    if record.get('status') == 'queued':
                        if not self.store.queue_contains(job_id):
                            record['recovered_at'] = _now()
                            self.store.save(record)
                            self.store.enqueue(
                                job_id,
                                int(record.get('priority', 0)),
                            )
                            recovered.append(job_id)
                        self.store.acknowledge(job_id)
                        continue
                    if self.lease_active(record, now):
                        continue
                    if int(record.get('_attempts', 0)) >= self.max_attempts:
                        self.dead_letter(record, 'max_attempts_exceeded')
                        recovered.append(job_id)
                        continue
                    record.pop('started_at', None)
                    record.pop('error', None)
                    record.pop('_worker_id', None)
                    record.pop('_lease_until', None)
                    record.pop('_started_epoch', None)
                    record.update({
                        'status': 'queued',
                        'recovered_at': _now(),
                    })
                    self.store.save(record)
                    self.store.enqueue(
                        job_id,
                        int(record.get('priority', 0)),
                    )
                    self.store.acknowledge(job_id)
                    recovered.append(job_id)
            except Exception:
                continue
        self.metrics.refresh_queue_depths(self.store)
        return recovered
