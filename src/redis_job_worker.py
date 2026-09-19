"""Execution runtime for Redis-backed job workers."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import os
from threading import Event
from time import monotonic, sleep

try:
    from .execution_semantics import ArtifactTransaction, execution_semantics
    from .job_manager import TERMINAL_STATUSES
except ImportError:
    from execution_semantics import ArtifactTransaction, execution_semantics
    from job_manager import TERMINAL_STATUSES


class RedisJobWorkerRuntime:
    def __init__(self, manager):
        self.manager = manager

    def accepting_new_work(self):
        state_store = getattr(self.manager, 'state_store', None)
        admission = getattr(state_store, 'admission', None)
        if admission is None:
            return True
        try:
            return bool(admission().get('accepting_work'))
        except Exception:
            return False

    def execute_claimed_job(self, record, job_id):
        manager = self.manager
        verifier = getattr(manager, 'verify_execution_identity', None)
        if verifier is not None:
            compatible, mismatches, actual = verifier(record)
            if not compatible:
                deferred = manager.defer_incompatible_execution(
                    record,
                    mismatches,
                    actual,
                )
                return manager._public_record(deferred) if deferred else None
        record = manager._claim(record)
        if (
            record is None
            or record.get('_worker_id') != manager.worker_id
            or record.get('status') != 'running'
        ):
            return manager._public_record(record) if record else None
        fencing_token = record.get('_fencing_token')
        durable_attempt = manager._begin_execution_attempt(
            record['_execution_key'],
            job_id,
            fencing_token,
            int(record.get('_attempts', 0)),
            record.get('execution_semantics', 'pure'),
        )
        if durable_attempt is not None and durable_attempt.get('status') == 'completed':
            manager._metrics.result_cache(record['tool'], 'hit')
            return manager._finish(
                job_id,
                durable_attempt['result'],
                fencing_token=fencing_token,
            )
        if durable_attempt is not None and durable_attempt.get('status') == 'indeterminate':
            return manager._mark_indeterminate(
                job_id,
                'a previous side-effecting attempt ended without a durable result; '
                'external state must be reviewed before manual retry',
                fencing_token=fencing_token,
            )
        cached = manager._load_execution_result(
            record['_execution_key'],
            job_id=job_id,
            fencing_token=fencing_token,
        )
        if cached is not None:
            manager._metrics.result_cache(record['tool'], 'hit')
            return manager._finish(
                job_id,
                cached['result'],
                fencing_token=fencing_token,
            )
        manager._metrics.result_cache(record['tool'], 'miss')
        spec = manager._validate_tool_state(record['tool'])
        semantics = execution_semantics(spec)
        transaction = ArtifactTransaction.prepare(
            record.get('_arguments', {}),
            spec,
            record['_execution_key'],
        )
        cancellation = {'checked_at': 0.0, 'requested': False}
        heartbeat = {'renewed_at': monotonic()}

        def cancelled():
            now = monotonic()
            if now - cancellation['checked_at'] < 0.5:
                return cancellation['requested']
            try:
                current = manager._load(str(job_id))
            except Exception:
                return cancellation['requested']
            cancellation['checked_at'] = now
            cancellation['requested'] = current is None or bool(current.get('_cancel_requested'))
            return cancellation['requested']

        def renew_lease():
            now = monotonic()
            interval = max(min(manager.lease_seconds / 3, 30), 0.2)
            if now - heartbeat['renewed_at'] < interval:
                return
            try:
                def extend(current, server_now):
                    if current.get('status') != 'running':
                        return None
                    if current.get('_worker_id') != manager.worker_id:
                        return None
                    if str(current.get('_fencing_token', '')) != str(fencing_token):
                        return None
                    current['_lease_until'] = server_now + manager.lease_seconds
                    return current

                manager._store.atomic_update(job_id, extend)
            except Exception:
                return
            heartbeat['renewed_at'] = now

        try:
            result = manager._tool_executor.execute(
                record['tool'],
                transaction.arguments,
                cancelled=cancelled,
                heartbeat=renew_lease,
            )
        except Exception as exc:
            transaction.rollback()
            if semantics == 'side_effecting':
                return manager._mark_indeterminate(
                    job_id,
                    'side-effecting execution stopped without a durable result; '
                    'external state must be reviewed before manual retry',
                    fencing_token=fencing_token,
                )
            return manager._finish(
                job_id,
                {'status': 'error', 'error': str(exc)},
                failed=True,
                fencing_token=fencing_token,
            )
        failed = isinstance(result, dict) and result.get('status') == 'error'
        if failed:
            transaction.rollback()
            if semantics == 'side_effecting':
                return manager._mark_indeterminate(
                    job_id,
                    'side-effecting tool reported an error after execution began; '
                    'external state must be reviewed before manual retry',
                    fencing_token=fencing_token,
                )
        if not failed:
            try:
                result = transaction.commit(result)
                result = manager._store_execution_result(
                    record['_execution_key'],
                    result,
                    job_id=job_id,
                    fencing_token=fencing_token,
                )
            except Exception:
                transaction.rollback()
                if semantics == 'side_effecting':
                    return manager._mark_indeterminate(
                        job_id,
                        'side-effecting result or artifact commit was interrupted; '
                        'external state must be reviewed before manual retry',
                        fencing_token=fencing_token,
                    )
                return manager._public_record(manager._load(job_id))
        return manager._finish(
            job_id,
            result,
            failed=failed,
            fencing_token=fencing_token,
        )

    def next_job(self):
        manager = self.manager
        if getattr(manager, 'capability_routing', False):
            return manager._store.next_job(
                route_ids=manager.compatible_route_ids(),
                include_legacy=False,
            )
        return manager._store.next_job()

    def complete_queued_item(self, job_id, poll_timeout):
        manager = self.manager
        outcome = manager.run_job(job_id)
        record = manager._load(job_id)
        if record is None or record.get('status') in TERMINAL_STATUSES:
            manager._ack(job_id)
        elif outcome and outcome.get('status') == 'queued':
            manager._ack(job_id)
            route_id = (
                manager._store.register_route(record)
                if record.get('_capability_routing') else None
            )
            manager._store.enqueue(
                job_id,
                int(record.get('priority', 0)),
                route_id=route_id,
            )
            manager._refresh_queue_metrics()
            sleep(min(max(float(poll_timeout), 0.05), 1.0))

    def run_forever(self, poll_timeout=5, stop_event=None, drain_timeout_seconds=120):
        manager = self.manager
        stop_event = stop_event or Event()
        drain_timeout_seconds = max(float(drain_timeout_seconds), 0.0)
        reconcile = getattr(manager, 'rebuild_durable_queue', None)
        try:
            reconcile_seconds = max(float(
                os.environ.get('JOB_OUTBOX_RECONCILE_SECONDS', '30')
            ), 1.0)
        except (TypeError, ValueError):
            reconcile_seconds = 30.0
        try:
            heartbeat_seconds = max(float(
                os.environ.get('WORKER_HEARTBEAT_SECONDS', '10')
            ), 1.0)
        except (TypeError, ValueError):
            heartbeat_seconds = 10.0
        def reconcile_durable():
            if reconcile is None:
                return []
            try:
                return reconcile()
            except Exception as exc:
                reporter = getattr(manager, '_metrics', None)
                if reporter is not None:
                    reporter.worker_job_handler_failed(manager.worker_id, exc)
                return []

        worker_heartbeat = getattr(manager, 'heartbeat_worker', None)
        health_state = getattr(manager, 'health_state', None)

        if worker_heartbeat is not None:
            worker_heartbeat(active_jobs=0, draining=False)
        if health_state is not None:
            health_state.update(draining=False, active_jobs=0)
        reconcile_durable()
        manager.recover_stale_jobs()
        next_reconcile = monotonic() + reconcile_seconds
        next_heartbeat = monotonic() + heartbeat_seconds
        futures = set()
        drain_deadline = None
        draining = False
        executor = ThreadPoolExecutor(
            max_workers=manager.max_concurrency,
            thread_name_prefix='redis-job',
        )
        manager._worker_executor = executor
        try:
            while True:
                if worker_heartbeat is not None and monotonic() >= next_heartbeat:
                    worker_heartbeat(
                        active_jobs=len(futures),
                        draining=draining,
                    )
                    next_heartbeat = monotonic() + heartbeat_seconds
                if health_state is not None:
                    health_state.update(
                        draining=draining,
                        active_jobs=len(futures),
                    )
                if (
                    not draining
                    and reconcile is not None
                    and monotonic() >= next_reconcile
                ):
                    reconcile_durable()
                    next_reconcile = monotonic() + reconcile_seconds
                finished = {future for future in futures if future.done()}
                for future in finished:
                    futures.remove(future)
                    try:
                        future.result()
                    except Exception as exc:
                        manager._metrics.worker_job_handler_failed(
                            manager.worker_id,
                            exc,
                        )
                if stop_event.is_set():
                    if not draining:
                        draining = True
                        drain_deadline = monotonic() + drain_timeout_seconds
                        manager._metrics.draining(
                            manager.worker_id,
                            True,
                            active_jobs=len(futures),
                        )
                        if worker_heartbeat is not None:
                            worker_heartbeat(
                                active_jobs=len(futures),
                                draining=True,
                            )
                        if health_state is not None:
                            health_state.update(
                                draining=True,
                                active_jobs=len(futures),
                            )
                    if not futures:
                        return True
                    if monotonic() >= drain_deadline:
                        return False
                else:
                    while (
                        len(futures) < manager.max_concurrency
                        and self.accepting_new_work()
                    ):
                        item = self.next_job()
                        if not item:
                            break
                        job_id = item.decode('utf-8') if isinstance(item, bytes) else str(item)
                        futures.add(executor.submit(
                            self.complete_queued_item,
                            job_id,
                            poll_timeout,
                        ))
                if not futures:
                    manager.recover_stale_jobs()
                    sleep(min(max(float(poll_timeout), 0.05), 1.0))
                else:
                    wait(
                        futures,
                        timeout=min(max(float(poll_timeout), 0.05), 1.0),
                        return_when=FIRST_COMPLETED,
                    )
        finally:
            if draining:
                manager._metrics.draining(
                    manager.worker_id,
                    False,
                    active_jobs=len(futures),
                )
            has_active_jobs = any(not future.done() for future in futures)
            executor.shutdown(wait=not has_active_jobs, cancel_futures=True)
            if not has_active_jobs:
                manager._worker_executor = None
