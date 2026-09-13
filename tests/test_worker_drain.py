import threading
import time
import unittest

from src.redis_job_worker import RedisJobWorkerRuntime


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


class WorkerDrainTests(unittest.TestCase):
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
