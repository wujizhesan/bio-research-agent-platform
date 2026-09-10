import os
from threading import Event, Lock
import time
import unittest
from unittest.mock import patch

from src.job_execution import InlineToolExecutor
from src.job_manager import JobManager
from src.resource_scheduling import (
    ResourceCapacity,
    ResourcePool,
    ResourceRequest,
    merge_requests,
    normalize_priority,
)


class ResourceSchedulingTests(unittest.TestCase):
    def test_request_validation_and_merge_preserve_tool_minimums(self):
        merged = merge_requests(
            {
                'cpu_cores': 2,
                'memory_mb': 2048,
                'gpu_count': 0,
                'labels': ['avx2'],
            },
            {
                'cpu_cores': 1,
                'memory_mb': 512,
                'gpu_count': 1,
                'labels': ['cuda'],
            },
        )
        self.assertEqual(merged.cpu_cores, 2)
        self.assertEqual(merged.memory_mb, 2048)
        self.assertEqual(merged.gpu_count, 1)
        self.assertEqual(merged.labels, ('avx2', 'cuda'))
        with self.assertRaisesRegex(ValueError, 'unknown resource fields'):
            ResourceRequest.from_mapping({'disk': 10})
        with self.assertRaisesRegex(ValueError, 'priority'):
            normalize_priority(101)
        with self.assertRaisesRegex(ValueError, 'integer'):
            ResourceRequest.from_mapping({'gpu_count': 1.5})
        with self.assertRaisesRegex(ValueError, 'requires gpu_count'):
            ResourceRequest.from_mapping({'gpu_memory_mb': 1024})

    def test_resource_pool_accounts_for_cpu_memory_gpu_and_labels(self):
        capacity = ResourceCapacity(4, 8192, 1, 16384, ('cuda', 'avx2'))
        pool = ResourcePool(capacity)
        request = ResourceRequest(2, 4096, 1, 12000, ('cuda',))
        self.assertTrue(pool.try_acquire(request))
        self.assertFalse(pool.try_acquire(request))
        self.assertEqual(pool.snapshot()['available']['gpu_count'], 0)
        pool.release(request)
        self.assertEqual(pool.snapshot()['available'], capacity.as_dict())

    def test_capacity_reads_worker_environment(self):
        values = {
            'JOB_TOTAL_CPU_CORES': '6',
            'JOB_TOTAL_MEMORY_MB': '12000',
            'JOB_TOTAL_GPUS': '2',
            'JOB_TOTAL_GPU_MEMORY_MB': '48000',
            'JOB_WORKER_LABELS': 'cuda,avx2',
        }
        with patch.dict(os.environ, values, clear=False):
            capacity = ResourceCapacity.from_env()
        self.assertEqual(capacity.cpu_cores, 6)
        self.assertEqual(capacity.memory_mb, 12000)
        self.assertEqual(capacity.gpu_count, 2)
        self.assertEqual(capacity.labels, ('avx2', 'cuda'))

    def test_local_scheduler_prioritizes_waiting_jobs(self):
        blocker_started = Event()
        release = Event()
        order = []
        order_lock = Lock()

        def run(_tool, arguments):
            name = arguments['name']
            if name == 'blocker':
                blocker_started.set()
                release.wait(3)
            with order_lock:
                order.append(name)
            return {'status': 'ok'}

        manager = JobManager(
            max_workers=1,
            tool_executor=InlineToolExecutor(run),
            resource_capacity=ResourceCapacity(1, 1024),
        )
        try:
            manager.submit('research_catalog', {'name': 'blocker'})
            self.assertTrue(blocker_started.wait(1))
            low = manager.submit('research_catalog', {'name': 'low'}, priority=-10)
            high = manager.submit('research_catalog', {'name': 'high'}, priority=50)
            self.assertEqual(manager.get(low['job_id'])['status'], 'queued')
            self.assertEqual(manager.get(high['job_id'])['status'], 'queued')
            release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and len(order) < 3:
                time.sleep(0.01)
            self.assertEqual(order, ['blocker', 'high', 'low'])
        finally:
            release.set()
            manager.shutdown()

    def test_local_scheduler_holds_job_until_resources_are_released(self):
        started = Event()
        release = Event()

        def run(_tool, arguments):
            if arguments['name'] == 'first':
                started.set()
                release.wait(3)
            return {'status': 'ok'}

        manager = JobManager(
            max_workers=2,
            tool_executor=InlineToolExecutor(run),
            resource_capacity=ResourceCapacity(1, 1024),
        )
        try:
            manager.submit('research_catalog', {'name': 'first'})
            self.assertTrue(started.wait(1))
            second = manager.submit('research_catalog', {'name': 'second'})
            time.sleep(0.1)
            self.assertEqual(manager.get(second['job_id'])['status'], 'queued')
            self.assertEqual(manager.resource_status()['available']['cpu_cores'], 0)
            release.set()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if manager.get(second['job_id'])['status'] == 'completed':
                    break
                time.sleep(0.01)
            self.assertEqual(manager.get(second['job_id'])['status'], 'completed')
        finally:
            release.set()
            manager.shutdown()

    def test_local_scheduler_rejects_request_larger_than_capacity(self):
        manager = JobManager(
            max_workers=1,
            resource_capacity=ResourceCapacity(2, 2048),
        )
        try:
            with self.assertRaisesRegex(ValueError, 'gpu_count'):
                manager.submit(
                    'research_catalog',
                    {},
                    resources={'gpu_count': 1},
                )
        finally:
            manager.shutdown()


if __name__ == '__main__':
    unittest.main()
