"""Metrics and structured lifecycle events for Redis jobs."""

from datetime import datetime, timezone
import logging
from prometheus_client import Counter, Histogram

try:
    from .observability import (
        JOB_ACTIVE,
        JOB_DURATION,
        JOB_EXECUTIONS,
        JOB_QUEUE_PHASE_DURATION,
        JOB_QUEUE_DURATION,
        JOB_TRANSITIONS,
        REDIS_DEAD_LETTER_DEPTH,
        REDIS_DEAD_LETTERS,
        REDIS_JOB_DURATION,
        REDIS_JOB_EXECUTIONS,
        REDIS_JOB_RETRIES,
        REDIS_PROCESSING_DEPTH,
        REDIS_QUEUE_DEPTH,
        REDIS_RESULT_CACHE,
        REDIS_WORKER_ACTIVE,
        REDIS_WORKER_DRAINING,
        log_event,
    )
    from .queue_timing import queue_phase_seconds
except ImportError:
    from observability import (
        JOB_ACTIVE,
        JOB_DURATION,
        JOB_EXECUTIONS,
        JOB_QUEUE_PHASE_DURATION,
        JOB_QUEUE_DURATION,
        JOB_TRANSITIONS,
        REDIS_DEAD_LETTER_DEPTH,
        REDIS_DEAD_LETTERS,
        REDIS_JOB_DURATION,
        REDIS_JOB_EXECUTIONS,
        REDIS_JOB_RETRIES,
        REDIS_PROCESSING_DEPTH,
        REDIS_QUEUE_DEPTH,
        REDIS_RESULT_CACHE,
        REDIS_WORKER_ACTIVE,
        REDIS_WORKER_DRAINING,
        log_event,
    )
    from queue_timing import queue_phase_seconds


REDIS_DEFERRED_CACHE_SYNC_FAILURES = Counter(
    'bio_agent_redis_deferred_cache_sync_failures_total',
    'Durable deferrals committed in PostgreSQL but not synchronized to Redis.',
    ['namespace'],
)
REDIS_DEFERRED_RECONCILIATIONS = Counter(
    'bio_agent_redis_deferred_reconciliations_total',
    'Deferred jobs reconciled from the durable outbox.',
    ['namespace', 'mode'],
)
REDIS_DEFERRED_RECONCILE_LAG = Histogram(
    'bio_agent_redis_deferred_reconcile_lag_seconds',
    'Seconds elapsed after a deferred retry became due before Redis reconciliation.',
    ['namespace', 'mode'],
)
REDIS_WORKER_CAPACITY_REJECTIONS = Counter(
    'bio_agent_redis_worker_capacity_rejections_total',
    'Jobs rejected by a worker because its advertised resources are occupied.',
    ['namespace', 'tool'],
)


class RedisJobMetrics:
    def __init__(self, namespace, backend='redis'):
        self.namespace = namespace
        self.backend = backend

    def refresh_queue_depths(self, store):
        try:
            depths = store.queue_depths()
        except Exception:
            return
        REDIS_QUEUE_DEPTH.labels(self.namespace).set(depths['queue'])
        REDIS_PROCESSING_DEPTH.labels(self.namespace).set(depths['processing'])
        REDIS_DEAD_LETTER_DEPTH.labels(self.namespace).set(
            depths['dead_letter']
        )

    def queued(self, record):
        tool = record['tool']
        JOB_TRANSITIONS.labels(self.backend, tool, 'queued').inc()
        log_event(
            'job.queued',
            backend=self.backend,
            job_id=record['job_id'],
            tool=tool,
            priority=record.get('priority', 0),
        )

    def capacity_rejected(self, record):
        REDIS_WORKER_CAPACITY_REJECTIONS.labels(
            self.namespace, record.get('tool') or 'unknown'
        ).inc()

    def deferred_cache_sync_failed(self, job_id, error):
        REDIS_DEFERRED_CACHE_SYNC_FAILURES.labels(self.namespace).inc()
        log_event(
            'job.external_retry_cache_sync_failed',
            level=logging.ERROR,
            backend=self.backend,
            job_id=job_id,
            error_type=type(error).__name__,
        )

    def deferred_reconciled(self, record, mode, now):
        REDIS_DEFERRED_RECONCILIATIONS.labels(self.namespace, mode).inc()
        retry_at = float(record.get('_retry_not_before') or 0)
        REDIS_DEFERRED_RECONCILE_LAG.labels(self.namespace, mode).observe(
            max(float(now) - retry_at, 0)
        )

    @staticmethod
    def result_cache(tool, outcome):
        REDIS_RESULT_CACHE.labels(tool, outcome).inc()

    @staticmethod
    def worker_job_handler_failed(worker_id, error):
        log_event(
            'worker.job_handler_failed',
            worker_id=worker_id,
            error_type=type(error).__name__,
        )

    def draining(self, worker_id, enabled, active_jobs=0):
        REDIS_WORKER_DRAINING.labels(self.namespace, worker_id).set(1 if enabled else 0)
        log_event(
            'worker.draining' if enabled else 'worker.drain_finished',
            worker_id=worker_id,
            active_jobs=active_jobs,
        )

    def claimed(self, record, worker_id):
        tool = record['tool']
        phases = queue_phase_seconds(record)
        if phases is not None:
            for phase, duration in phases.items():
                JOB_QUEUE_PHASE_DURATION.labels(
                    self.backend, tool, phase
                ).observe(duration)
        if record.get('_attempts', 0) > 1 or record.get('retry_of'):
            REDIS_JOB_RETRIES.labels(tool).inc()
        REDIS_WORKER_ACTIVE.labels(self.namespace).inc()
        try:
            queued_seconds = (
                datetime.now(timezone.utc)
                - datetime.fromisoformat(record['created_at'])
            ).total_seconds()
            JOB_QUEUE_DURATION.labels(self.backend, tool).observe(
                max(queued_seconds, 0)
            )
        except (KeyError, TypeError, ValueError):
            pass
        JOB_ACTIVE.labels(self.backend, tool).inc()
        JOB_TRANSITIONS.labels(self.backend, tool, 'running').inc()
        log_event(
            'job.started',
            backend=self.backend,
            worker_id=worker_id,
        )

    def dead_lettered(self, record, reason, worker_id, transition=True):
        REDIS_DEAD_LETTERS.labels(self.namespace, reason).inc()
        if transition:
            JOB_TRANSITIONS.labels(self.backend, record['tool'], 'failed').inc()
        log_event(
            'job.dead_lettered',
            backend=self.backend,
            worker_id=worker_id,
            reason=reason,
            attempts=int(record.get('_attempts', 0)),
        )

    def finished(self, record, elapsed, worker_id):
        tool = record['tool']
        status = record['status']
        if elapsed is not None:
            REDIS_JOB_DURATION.labels(tool).observe(elapsed)
        REDIS_JOB_EXECUTIONS.labels(tool, status).inc()
        REDIS_WORKER_ACTIVE.labels(self.namespace).dec()
        if elapsed is not None:
            JOB_DURATION.labels(self.backend, tool).observe(elapsed)
        JOB_EXECUTIONS.labels(self.backend, tool, status).inc()
        JOB_TRANSITIONS.labels(self.backend, tool, status).inc()
        JOB_ACTIVE.labels(self.backend, tool).dec()
        log_event(
            'job.completed',
            backend=self.backend,
            worker_id=worker_id,
            status=status,
            duration_seconds=elapsed,
        )
