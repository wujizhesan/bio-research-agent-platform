from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from time import time
import unittest
from unittest.mock import patch
from prometheus_client import generate_latest

from src.redis_job_manager import RedisJobManager
from src.resource_scheduling import ResourceCapacity


class InMemoryRedis:
    def __init__(self):
        self.values = {}
        self.sorted_sets = defaultdict(dict)
        self.lists = defaultdict(list)

    def ping(self):
        return True

    def set(self, key, value, **options):
        if options.get('nx') and key in self.values:
            return None
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def zadd(self, key, mapping):
        self.sorted_sets[key].update(mapping)

    def zrevrange(self, key, start, end):
        ordered = sorted(self.sorted_sets[key], key=self.sorted_sets[key].get, reverse=True)
        return ordered[start:end + 1]

    def rpush(self, key, value):
        self.lists[key].append(value)

    def lpush(self, key, value):
        self.lists[key].insert(0, value)

    def brpop(self, key, timeout=0):
        if not self.lists[key]:
            return None
        return key, self.lists[key].pop(0)

    def brpoplpush(self, source, destination, timeout=0):
        if not self.lists[source]:
            return None
        value = self.lists[source].pop()
        self.lists[destination].insert(0, value)
        return value

    def lrange(self, key, start, end):
        values = self.lists[key]
        if end == -1:
            end = len(values) - 1
        return values[start:end + 1]

    def lrem(self, key, count, value):
        values = self.lists[key]
        removed = 0
        kept = []
        for item in values:
            if item == value and (count == 0 or removed < abs(count)):
                removed += 1
                continue
            kept.append(item)
        self.lists[key] = kept
        return removed

    def llen(self, key):
        return len(self.lists[key])

    def close(self):
        return None


class RedisJobManagerTests(unittest.TestCase):
    def test_submit_idempotency_and_worker_execution(self):
        redis = InMemoryRedis()
        manager = RedisJobManager(redis_client=redis, namespace='test')
        try:
            first = manager.submit('research_catalog', {}, idempotency_key='request-1')
            duplicate = manager.submit('research_catalog', {}, idempotency_key='request-1')
            self.assertEqual(first['job_id'], duplicate['job_id'])
            self.assertTrue(duplicate['deduplicated'])
            self.assertEqual(manager.get(first['job_id'])['status'], 'queued')

            completed = manager.run_job(first['job_id'])
            self.assertEqual(completed['status'], 'completed')
            self.assertEqual(completed['result']['status'], 'ok')
            self.assertEqual(manager.list(1)[0]['job_id'], first['job_id'])
            stored = json.loads(redis.get(f'test:job:{first["job_id"]}'))
            self.assertEqual(stored['status'], 'completed')
            self.assertEqual(stored['run_context']['job_id'], first['job_id'])
            self.assertEqual(stored['run_context']['trace_id'], stored['trace_id'])
            self.assertNotIn('_created_score', completed)
            self.assertEqual(completed['attempts'], 1)
            metrics = generate_latest().decode('utf-8')
            self.assertIn('bio_agent_redis_job_executions_total', metrics)
            self.assertIn('bio_agent_redis_result_cache_total', metrics)
        finally:
            manager.shutdown()

    def test_expired_processing_job_is_requeued(self):
        redis = InMemoryRedis()
        manager = RedisJobManager(redis_client=redis, namespace='test', worker_id='recovery-worker')
        try:
            submitted = manager.submit('research_catalog', {})
            job_id = redis.brpoplpush('test:jobs:queue', 'test:jobs:processing')
            record = manager._load(job_id)
            record.update({
                'status': 'running',
                '_worker_id': 'dead-worker',
                '_lease_until': time() - 1,
            })
            manager._save(record)

            self.assertEqual(manager.recover_stale_jobs(), [submitted['job_id']])
            self.assertEqual(redis.lists['test:jobs:processing'], [])
            self.assertEqual(redis.lists['test:jobs:queue'], [submitted['job_id']])
            completed = manager.run_job(submitted['job_id'])
            self.assertEqual(completed['status'], 'completed')
            self.assertEqual(completed['attempts'], 1)
        finally:
            manager.shutdown()

    def test_recovered_job_reuses_successful_execution_result(self):
        redis = InMemoryRedis()
        manager = RedisJobManager(redis_client=redis, namespace='test', worker_id='first-worker')
        try:
            submitted = manager.submit('research_catalog', {})
            first = manager.run_job(submitted['job_id'])
            record = manager._load(submitted['job_id'])
            record.update({'status': 'queued', 'finished_at': None})
            manager._save(record)

            with patch('src.redis_job_manager.run_tool') as run_tool:
                recovered = manager.run_job(submitted['job_id'])

            self.assertEqual(recovered['status'], 'completed')
            self.assertEqual(recovered['result'], first['result'])
            run_tool.assert_not_called()
            self.assertTrue(redis.get(f"test:jobs:execution:{record['_execution_key']}"))
        finally:
            manager.shutdown()

    def test_priority_queues_select_high_before_normal_and_low(self):
        redis = InMemoryRedis()
        manager = RedisJobManager(redis_client=redis, namespace='test')
        try:
            low = manager.submit('research_catalog', {}, priority=-5)
            normal = manager.submit('research_catalog', {}, priority=0)
            high = manager.submit('research_catalog', {}, priority=50)
            self.assertEqual(manager._next_job(), high['job_id'])
            manager._ack(high['job_id'])
            self.assertEqual(manager._next_job(), normal['job_id'])
            manager._ack(normal['job_id'])
            self.assertEqual(manager._next_job(), low['job_id'])
        finally:
            manager.shutdown()

    def test_worker_defers_job_requiring_incompatible_resources(self):
        redis = InMemoryRedis()
        manager = RedisJobManager(
            redis_client=redis,
            namespace='test',
            resource_capacity=ResourceCapacity(4, 8192, 0, 0, ('cpu',)),
            enforce_capacity=True,
        )
        try:
            submitted = manager.submit(
                'research_catalog',
                {},
                resources={'gpu_count': 1, 'labels': ['cuda']},
            )
            deferred = manager.run_job(submitted['job_id'])
            self.assertEqual(deferred['status'], 'queued')
            self.assertEqual(
                deferred['scheduling']['status'],
                'waiting_for_compatible_worker',
            )
            self.assertIn('gpu_count', deferred['scheduling']['reason'])
        finally:
            manager.shutdown()

    def test_failed_execution_is_moved_to_dead_letter_queue(self):
        class FailingExecutor:
            def execute(self, *_args, **_kwargs):
                return {'status': 'error', 'error': 'tool failed'}

        redis = InMemoryRedis()
        manager = RedisJobManager(
            redis_client=redis,
            namespace='test',
            tool_executor=FailingExecutor(),
        )
        try:
            submitted = manager.submit('research_catalog', {})
            failed = manager.run_job(submitted['job_id'])
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(failed['dead_letter_reason'], 'execution_failed')
            self.assertEqual(
                redis.lists['test:jobs:dead-letter'], [submitted['job_id']]
            )
        finally:
            manager.shutdown()

    def test_stale_job_exceeding_attempt_limit_is_dead_lettered(self):
        redis = InMemoryRedis()
        manager = RedisJobManager(
            redis_client=redis,
            namespace='test',
            max_attempts=2,
        )
        try:
            submitted = manager.submit('research_catalog', {})
            job_id = redis.brpoplpush(
                'test:jobs:queue', 'test:jobs:processing'
            )
            record = manager._load(job_id)
            record.update({
                'status': 'running',
                '_attempts': 2,
                '_worker_id': 'dead-worker',
                '_lease_until': time() - 1,
            })
            manager._save(record)
            manager.recover_stale_jobs()
            failed = manager.get(submitted['job_id'])
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(
                failed['dead_letter_reason'], 'max_attempts_exceeded'
            )
            self.assertEqual(redis.lists['test:jobs:queue'], [])
        finally:
            manager.shutdown()

    def test_concurrent_jobs_reserve_worker_resources(self):
        started = Event()
        release = Event()

        class BlockingExecutor:
            def execute(self, *_args, **_kwargs):
                started.set()
                release.wait(5)
                return {'status': 'ok'}

        redis = InMemoryRedis()
        manager = RedisJobManager(
            redis_client=redis,
            namespace='test',
            tool_executor=BlockingExecutor(),
            resource_capacity=ResourceCapacity(1, 1024, 0, 0, ()),
            enforce_capacity=True,
            max_concurrency=2,
        )
        try:
            first = manager.submit(
                'research_catalog', {}, resources={'cpu_cores': 1}
            )
            second = manager.submit(
                'research_catalog', {}, resources={'cpu_cores': 1}
            )
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(manager.run_job, first['job_id'])
                self.assertTrue(started.wait(2))
                deferred = manager.run_job(second['job_id'])
                self.assertEqual(deferred['status'], 'queued')
                self.assertEqual(
                    deferred['scheduling']['status'],
                    'waiting_for_worker_capacity',
                )
                release.set()
                self.assertEqual(future.result()['status'], 'completed')
            self.assertEqual(manager.max_concurrency, 2)
            self.assertEqual(
                manager.resource_status()['resources']['available']['cpu_cores'],
                1,
            )
        finally:
            release.set()
            manager.shutdown()


if __name__ == '__main__':
    unittest.main()
