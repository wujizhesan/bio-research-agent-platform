"""Execution runtime for Redis-backed job workers."""

from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from time import sleep, time

try:
    from .job_manager import TERMINAL_STATUSES
    from .observability import REDIS_RESULT_CACHE, log_event
except ImportError:
    from job_manager import TERMINAL_STATUSES
    from observability import REDIS_RESULT_CACHE, log_event


class RedisJobWorkerRuntime:
    def __init__(self, manager):
        self.manager = manager

    def execute_claimed_job(self, record, job_id):
        manager = self.manager
        manager._claim(record)
        cached = manager._load_execution_result(record['_execution_key'])
        if cached is not None:
            REDIS_RESULT_CACHE.labels(record['tool'], 'hit').inc()
            return manager._finish(job_id, cached['result'])
        REDIS_RESULT_CACHE.labels(record['tool'], 'miss').inc()
        cancellation = {'checked_at': 0.0, 'requested': False}
        heartbeat = {'renewed_at': time()}

        def cancelled():
            now = time()
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
            now = time()
            interval = max(min(manager.lease_seconds / 3, 30), 0.2)
            if now - heartbeat['renewed_at'] < interval:
                return
            try:
                current = manager._load(str(job_id))
                if (
                    current is not None
                    and current.get('status') == 'running'
                    and current.get('_worker_id') == manager.worker_id
                ):
                    current['_lease_until'] = now + manager.lease_seconds
                    manager._save(current)
            except Exception:
                return
            heartbeat['renewed_at'] = now

        try:
            result = manager._tool_executor.execute(
                record['tool'],
                record.get('_arguments', {}),
                cancelled=cancelled,
                heartbeat=renew_lease,
            )
            failed = isinstance(result, dict) and result.get('status') == 'error'
            if not failed:
                result = manager._store_execution_result(record['_execution_key'], result)
        except Exception as exc:
            return manager._finish(job_id, {'status': 'error', 'error': str(exc)}, failed=True)
        return manager._finish(job_id, result, failed=failed)

    def next_job(self):
        manager = self.manager
        move = getattr(manager.redis, 'rpoplpush', None)
        for queue_key in manager._queue_keys:
            if move is not None:
                item = move(queue_key, manager._processing_key)
            else:
                item = manager.redis.brpoplpush(
                    queue_key,
                    manager._processing_key,
                    timeout=0,
                )
            if item:
                return item
        return None

    def complete_queued_item(self, job_id, poll_timeout):
        manager = self.manager
        outcome = manager.run_job(job_id)
        record = manager._load(job_id)
        if record is None or record.get('status') in TERMINAL_STATUSES:
            manager._ack(job_id)
        elif outcome and outcome.get('status') == 'queued':
            manager._ack(job_id)
            manager.redis.lpush(
                manager._queue_for_priority(int(record.get('priority', 0))),
                job_id,
            )
            sleep(min(max(float(poll_timeout), 0.05), 1.0))

    def run_forever(self, poll_timeout=5):
        manager = self.manager
        manager.recover_stale_jobs()
        futures = set()
        executor = ThreadPoolExecutor(
            max_workers=manager.max_concurrency,
            thread_name_prefix='redis-job',
        )
        manager._worker_executor = executor
        try:
            while True:
                finished = {future for future in futures if future.done()}
                for future in finished:
                    futures.remove(future)
                    try:
                        future.result()
                    except Exception as exc:
                        log_event(
                            'worker.job_handler_failed',
                            worker_id=manager.worker_id,
                            error_type=type(exc).__name__,
                        )
                while len(futures) < manager.max_concurrency:
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
            executor.shutdown(wait=False, cancel_futures=True)
            manager._worker_executor = None
