import time
import threading
import unittest

from src.redis_job_worker import RedisJobWorkerRuntime
from src.redis_job_store import RedisQueueStore


class RecordingMetrics:
    def __init__(self):
        self.transitions = []

    def draining(self, worker_id, enabled, active_jobs=0):
        self.transitions.append((worker_id, enabled, active_jobs))

    def worker_job_handler_failed(self, worker_id, error):
        raise AssertionError((worker_id, error))


class DrainManager:
    def __init__(self, block=False):
        self.max_concurrency = 1
        self.worker_id = 'worker-drain-test'
        self._metrics = RecordingMetrics()
        self._worker_executor = None
        self.next_calls = 0
        self.recovered = 0
        self.started = threading.Event()
        self.release = threading.Event()
        self.block = block

    def recover_stale_jobs(self):
        self.recovered += 1

    def _next_job(self):
        self.next_calls += 1
        return 'job-1' if self.block and self.next_calls == 1 else None

    def _complete_queued_item(self, job_id, poll_timeout):
        self.started.set()
        self.release.wait(timeout=2)


class WakeSubscription:
    def __init__(self):
        self.waiting = threading.Event()
        self.wake = threading.Event()
        self.closed = False
        self.channel = None

    def subscribe(self, channel):
        self.channel = channel

    def get_message(self, timeout):
        self.waiting.set()
        received = self.wake.wait(timeout)
        self.wake.clear()
        return {'type': 'message'} if received else None

    def close(self):
        self.closed = True


class WakeRedis:
    def __init__(self):
        self.subscription = WakeSubscription()
        self.items = []
        self.lock = threading.Lock()
        self.published = []

    def pubsub(self, ignore_subscribe_messages=False):
        assert ignore_subscribe_messages
        return self.subscription

    def lpush(self, key, value):
        with self.lock:
            self.items.append((key, value))

    def publish(self, channel, payload):
        self.published.append((channel, payload))
        self.subscription.wake.set()

    def pop(self):
        with self.lock:
            return self.items.pop(0)[1] if self.items else None


class WorkerDrainTests(unittest.TestCase):
    def test_heavy_job_uses_one_slot_while_light_jobs_keep_running(self):
        manager = DrainManager()
        manager.max_concurrency = 4
        manager.max_heavy_concurrency = 1
        manager.workload_class_routing = True
        jobs = {
            'heavy': ['heavy-1', 'heavy-2'],
            'light': ['light-1', 'light-2', 'light-3'],
        }
        started = {job: threading.Event() for lane in jobs.values() for job in lane}
        release_heavy = threading.Event()
        release_light = threading.Event()
        lock = threading.Lock()
        runtime = RedisJobWorkerRuntime(manager)

        def next_job(lane=None):
            with lock:
                queue = jobs[lane]
                return queue.pop(0) if queue else None

        def complete(job_id, _poll_timeout):
            started[job_id].set()
            release_heavy.wait(3) if job_id.startswith('heavy') else release_light.wait(3)

        runtime.next_job = next_job
        runtime.complete_queued_item = complete
        stop_event = threading.Event()
        thread = threading.Thread(target=lambda: runtime.run_forever(
            poll_timeout=0.01,
            stop_event=stop_event,
            drain_timeout_seconds=3,
        ))
        thread.start()
        try:
            self.assertTrue(started['heavy-1'].wait(1))
            for job_id in ('light-1', 'light-2', 'light-3'):
                self.assertTrue(started[job_id].wait(1))
            self.assertFalse(started['heavy-2'].is_set())
            release_heavy.set()
            self.assertTrue(started['heavy-2'].wait(1))
        finally:
            stop_event.set()
            release_heavy.set()
            release_light.set()
            thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_queue_notification_wakes_idle_worker(self):
        redis = WakeRedis()
        store = RedisQueueStore(redis, 'test')
        manager = DrainManager()
        manager._store = store
        stop_event = threading.Event()
        runtime = RedisJobWorkerRuntime(manager)
        runtime.next_job = redis.pop
        runtime.complete_queued_item = manager._complete_queued_item
        thread = threading.Thread(target=lambda: runtime.run_forever(
            poll_timeout=5,
            stop_event=stop_event,
            drain_timeout_seconds=1,
        ))
        thread.start()
        try:
            self.assertTrue(redis.subscription.waiting.wait(timeout=1))
            store.enqueue('job-1')
            started = manager.started.wait(timeout=0.7)
        finally:
            stop_event.set()
            manager.release.set()
            thread.join(timeout=2)
        self.assertTrue(started)
        self.assertFalse(thread.is_alive())
        self.assertEqual(redis.published, [('test:jobs:wakeup', '1')])
        self.assertEqual(redis.subscription.channel, 'test:jobs:wakeup')
        self.assertTrue(redis.subscription.closed)

    def test_publish_failure_keeps_enqueued_job(self):
        class FailingWakeRedis(WakeRedis):
            def publish(self, channel, payload):
                raise ConnectionError('wakeup unavailable')

        redis = FailingWakeRedis()
        store = RedisQueueStore(redis, 'test')
        store.enqueue('job-1')
        self.assertEqual(redis.pop(), 'job-1')

    def test_backpressure_pauses_claiming_new_jobs(self):
        class PausedStateStore:
            @staticmethod
            def admission():
                return {'accepting_work': False}

        manager = DrainManager(block=True)
        manager.state_store = PausedStateStore()
        stop_event = threading.Event()
        runtime = RedisJobWorkerRuntime(manager)
        runtime.next_job = manager._next_job
        thread = threading.Thread(target=lambda: runtime.run_forever(
            poll_timeout=0.01,
            stop_event=stop_event,
            drain_timeout_seconds=1,
        ))
        thread.start()
        time.sleep(0.05)
        stop_event.set()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(manager.next_calls, 0)

    def test_pre_stopped_worker_claims_no_jobs(self):
        manager = DrainManager()
        stop_event = threading.Event()
        stop_event.set()
        drained = RedisJobWorkerRuntime(manager).run_forever(
            poll_timeout=0.01,
            stop_event=stop_event,
            drain_timeout_seconds=1,
        )
        self.assertTrue(drained)
        self.assertEqual(manager.next_calls, 0)
        self.assertEqual(
            manager._metrics.transitions,
            [('worker-drain-test', True, 0), ('worker-drain-test', False, 0)],
        )

    def test_signal_stops_claiming_and_times_out_active_job(self):
        manager = DrainManager(block=True)
        stop_event = threading.Event()
        result = {}
        runtime = RedisJobWorkerRuntime(manager)
        runtime.next_job = manager._next_job
        runtime.complete_queued_item = manager._complete_queued_item

        def run():
            result['drained'] = runtime.run_forever(
                poll_timeout=0.01,
                stop_event=stop_event,
                drain_timeout_seconds=0.05,
            )

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(manager.started.wait(timeout=1))
        stop_event.set()
        thread.join(timeout=1)
        manager.release.set()
        self.assertFalse(thread.is_alive())
        self.assertFalse(result['drained'])
        self.assertEqual(manager.next_calls, 1)
        self.assertEqual(manager._metrics.transitions[0][1:], (True, 1))
        self.assertEqual(manager._metrics.transitions[-1][1], False)

    def test_worker_waits_for_active_job_within_grace_period(self):
        manager = DrainManager(block=True)
        stop_event = threading.Event()
        result = {}
        runtime = RedisJobWorkerRuntime(manager)
        runtime.next_job = manager._next_job
        runtime.complete_queued_item = manager._complete_queued_item

        def run():
            result['drained'] = runtime.run_forever(
                poll_timeout=0.01,
                stop_event=stop_event,
                drain_timeout_seconds=1,
            )

        thread = threading.Thread(target=run)
        thread.start()
        self.assertTrue(manager.started.wait(timeout=1))
        stop_event.set()
        time.sleep(0.03)
        manager.release.set()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertTrue(result['drained'])
        self.assertEqual(manager.next_calls, 1)


if __name__ == '__main__':
    unittest.main()
