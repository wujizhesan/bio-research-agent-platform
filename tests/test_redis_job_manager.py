from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from threading import Event
from time import time
import unittest
from unittest.mock import patch
from prometheus_client import generate_latest

from src.redis_job_coordinator import RedisExecutionCoordinator
from src.redis_job_manager import RedisJobManager
from src.redis_job_metrics import RedisJobMetrics
from src.redis_job_recovery import RedisLeaseRecovery
from src.redis_job_store import RedisQueueStore
from src.redis_worker_registry import RedisWorkerRegistry
from src.resource_scheduling import ResourceCapacity


class InMemoryRedis:
    def __init__(self):
        self.values = {}
        self.sorted_sets = defaultdict(dict)
        self.lists = defaultdict(list)
        self.sets = defaultdict(set)

    def ping(self):
        return True

    def set(self, key, value, **options):
        if options.get('nx') and key in self.values:
            return None
        self.values[key] = value
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        return int(self.values.pop(key, None) is not None)

    def incr(self, key):
        value = int(self.values.get(key, 0)) + 1
        self.values[key] = value
        return value

    def zadd(self, key, mapping):
        self.sorted_sets[key].update(mapping)

    def zrevrange(self, key, start, end):
        ordered = sorted(self.sorted_sets[key], key=self.sorted_sets[key].get, reverse=True)
        if end == -1:
            end = len(ordered) - 1
        return ordered[start:end + 1]

    def zrangebyscore(self, key, minimum, maximum):
        lower = float(minimum)
        upper = float('inf') if maximum == '+inf' else float(maximum)
        return [
            item
            for item, score in sorted(
                self.sorted_sets[key].items(),
                key=lambda pair: pair[1],
            )
            if lower <= score <= upper
        ]

    def zrem(self, key, value):
        return int(self.sorted_sets[key].pop(value, None) is not None)

    def sadd(self, key, value):
        before = len(self.sets[key])
        self.sets[key].add(value)
        return int(len(self.sets[key]) > before)

    def smembers(self, key):
        return set(self.sets[key])

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
    def test_worker_registry_expires_and_filters_capabilities(self):
        redis = InMemoryRedis()
        registry = RedisWorkerRegistry(redis, 'registry', ttl_seconds=5)
        record = {
            'worker_id': 'gpu-worker',
            'capacity': {
                'cpu_cores': 8,
                'memory_mb': 32768,
                'gpu_count': 1,
                'gpu_memory_mb': 16384,
                'labels': ['cuda12'],
            },
            'draining': False,
            'execution_catalog': {
                'omics_run_analysis': {'fingerprint': 'implementation-v1'},
            },
        }
        registry.heartbeat(record, now=100)
        job = {
            'tool': 'omics_run_analysis',
            'resources': {'gpu_count': 1, 'labels': ['cuda12']},
            'execution_identity': {'fingerprint': 'implementation-v1'},
        }
        self.assertEqual(
            [item['worker_id'] for item in registry.compatible_workers(job, now=101)],
            ['gpu-worker'],
        )
        self.assertEqual(registry.list_active(now=106), [])

    def test_versioned_route_is_only_consumed_by_matching_worker(self):
        redis = InMemoryRedis()
        api = RedisJobManager(
            redis_client=redis,
            namespace='versions',
            capability_routing=True,
        )
        old = RedisJobManager(
            redis_client=redis,
            namespace='versions',
            worker_id='old-worker',
            capability_routing=True,
            enforce_capacity=True,
        )
        new = RedisJobManager(
            redis_client=redis,
            namespace='versions',
            worker_id='new-worker',
            capability_routing=True,
            enforce_capacity=True,
        )
        try:
            expected = api._execution_identity('research_catalog')
            old_identity = dict(expected)
            old_identity['fingerprint'] = 'old-implementation'
            old._execution_catalog['research_catalog'] = old_identity
            new._execution_catalog['research_catalog'] = dict(expected)
            old.heartbeat_worker()
            submitted = api.submit('research_catalog', {})
            self.assertEqual(
                submitted['scheduling']['status'],
                'waiting_for_capability',
            )
            self.assertIsNone(old._next_job())
            new.heartbeat_worker()
            self.assertEqual(new._next_job(), submitted['job_id'])
        finally:
            api.shutdown()
            old.shutdown()
            new.shutdown()

    def test_worker_rejects_pinned_implementation_mismatch_before_claim(self):
        manager = RedisJobManager(
            redis_client=InMemoryRedis(),
            namespace='identity-check',
            require_execution_fingerprint=True,
        )
        try:
            submitted = manager.submit('research_catalog', {})
            current = manager._execution_identity('research_catalog')
            current['fingerprint'] = 'different-worker-implementation'
            manager._execution_catalog['research_catalog'] = current
            deferred = manager.run_job(submitted['job_id'])
            stored = manager._load(submitted['job_id'])
            self.assertEqual(deferred['status'], 'queued')
            self.assertEqual(
                deferred['scheduling']['status'],
                'waiting_for_implementation',
            )
            self.assertEqual(stored.get('_attempts', 0), 0)
            self.assertNotIn('_worker_id', stored)

        finally:
            manager.shutdown()

    def test_durable_queue_is_rebuilt_after_complete_redis_loss(self):
        class DurableStateStore:
            def __init__(self):
                self.records = {}

            def save(self, record):
                self.records[record['job_id']] = dict(record)

            def load_dispatchable(self, limit=1000):
                return [
                    dict(record)
                    for record in list(self.records.values())[:limit]
                    if record.get('status') not in {'completed', 'failed', 'cancelled'}
                ]

        durable = DurableStateStore()
        original = RedisJobManager(
            redis_client=InMemoryRedis(),
            namespace='loss',
            state_store=durable,
        )
        try:
            submitted = original.submit('research_catalog', {})
        finally:
            original.shutdown()
        recovered = RedisJobManager(
            redis_client=InMemoryRedis(),
            namespace='loss',
            state_store=durable,
        )
        try:
            self.assertEqual(recovered.rebuild_durable_queue(), [submitted['job_id']])
            self.assertEqual(recovered._next_job(), submitted['job_id'])
            completed = recovered.run_job(submitted['job_id'])
            self.assertEqual(completed['status'], 'completed')
        finally:
            recovered.shutdown()

    def test_reconciliation_persists_existing_redis_terminal_state(self):
        class DurableStateStore:
            def __init__(self):
                self.records = {}

            def save(self, record):
                self.records[record['job_id']] = dict(record)

            def load_dispatchable(self, limit=1000):
                return [
                    dict(record)
                    for record in list(self.records.values())[:limit]
                    if record.get('status') not in {'completed', 'failed', 'cancelled'}
                ]

        redis = InMemoryRedis()
        durable = DurableStateStore()
        original = RedisJobManager(
            redis_client=redis,
            namespace='terminal-sync',
            state_store=durable,
        )
        try:
            submitted = original.submit('research_catalog', {})
            original._store.state_store = None
            completed = original.run_job(submitted['job_id'])
            self.assertEqual(completed['status'], 'completed')
            self.assertEqual(durable.records[submitted['job_id']]['status'], 'queued')
        finally:
            original.shutdown()

        recovered = RedisJobManager(
            redis_client=redis,
            namespace='terminal-sync',
            state_store=durable,
        )
        try:
            self.assertEqual(recovered.rebuild_durable_queue(), [])
            self.assertEqual(durable.records[submitted['job_id']]['status'], 'completed')
        finally:
            recovered.shutdown()

    def test_stale_worker_cannot_finish_after_new_fencing_token(self):
        redis = InMemoryRedis()
        first = RedisJobManager(
            redis_client=redis,
            namespace='fencing',
            worker_id='worker-old',
            lease_seconds=1,
        )
        second = RedisJobManager(
            redis_client=redis,
            namespace='fencing',
            worker_id='worker-new',
            lease_seconds=1,
        )
        try:
            submitted = first.submit('research_catalog', {})
            claimed_old = first._claim(first._load(submitted['job_id']))
            old_token = claimed_old['_fencing_token']
            expired = first._load(submitted['job_id'])
            expired['_lease_until'] = first._store.server_time() - 1
            first._save(expired)
            first.redis.lpush(first._processing_key, submitted['job_id'])
            self.assertEqual(second.recover_stale_jobs(), [submitted['job_id']])
            claimed_new = second._claim(second._load(submitted['job_id']))
            new_token = claimed_new['_fencing_token']
            self.assertNotEqual(new_token, old_token)
            rejected = first._finish(
                submitted['job_id'],
                {'status': 'ok', 'source': 'old'},
                fencing_token=old_token,
            )
            self.assertEqual(rejected['status'], 'running')
            accepted = second._finish(
                submitted['job_id'],
                {'status': 'ok', 'source': 'new'},
                fencing_token=new_token,
            )
            self.assertEqual(accepted['status'], 'completed')
            self.assertEqual(accepted['result']['source'], 'new')
        finally:
            first.shutdown()
            second.shutdown()

    def test_heartbeat_cannot_overwrite_concurrent_cancellation(self):
        redis = InMemoryRedis()
        manager = RedisJobManager(
            redis_client=redis,
            namespace='cancel-race',
            worker_id='worker-1',
        )
        try:
            submitted = manager.submit('research_catalog', {})
            claimed = manager._claim(manager._load(submitted['job_id']))
            token = claimed['_fencing_token']
            manager.cancel(submitted['job_id'])

            def heartbeat(current, server_now):
                if current.get('_worker_id') != manager.worker_id:
                    return None
                if current.get('_fencing_token') != token:
                    return None
                current['_lease_until'] = server_now + manager.lease_seconds
                return current

            manager._store.atomic_update(submitted['job_id'], heartbeat)
            current = manager._load(submitted['job_id'])
            self.assertTrue(current['_cancel_requested'])
            finished = manager._finish(
                submitted['job_id'],
                {'status': 'ok'},
                fencing_token=token,
            )
            self.assertEqual(finished['status'], 'cancelled')
        finally:
            manager.shutdown()

    def test_manager_composes_four_redis_layers(self):
        manager = RedisJobManager(
            redis_client=InMemoryRedis(),
            namespace='layers',
        )
        try:
            self.assertIsInstance(manager._store, RedisQueueStore)
            self.assertIsInstance(manager._recovery, RedisLeaseRecovery)
            self.assertIsInstance(manager._coordinator, RedisExecutionCoordinator)
            self.assertIsInstance(manager._metrics, RedisJobMetrics)
            self.assertIs(manager._recovery.store, manager._store)
            self.assertIs(manager._coordinator.recovery, manager._recovery)
            self.assertIs(manager._coordinator.metrics, manager._metrics)
        finally:
            manager.shutdown()

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

    def test_prepared_job_is_not_visible_to_worker_until_dispatched(self):
        redis = InMemoryRedis()
        manager = RedisJobManager(redis_client=redis, namespace='outbox-order')
        try:
            prepared = manager.prepare('research_catalog', {})

            self.assertEqual(
                redis.lists.get('outbox-order:jobs:queue', []),
                [],
            )
            manager.dispatch(prepared['job_id'])
            self.assertEqual(
                redis.lists['outbox-order:jobs:queue'],
                [prepared['job_id']],
            )
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

    def test_recovered_job_reuses_postgres_execution_result_after_redis_loss(self):
        class DurableStateStore:
            def __init__(self):
                self.results = {}

            def save(self, _record):
                return None

            def load_execution_result(self, execution_key):
                return self.results.get(execution_key)

            def store_execution_result(self, execution_key, job_id, result, fencing_token=None):
                self.results.setdefault(execution_key, {
                    'execution_key': execution_key,
                    'job_id': job_id,
                    'fencing_token': fencing_token,
                    'status': 'completed',
                    'result': result,
                })
                return self.results[execution_key]

        state_store = DurableStateStore()
        first_redis = InMemoryRedis()
        first_manager = RedisJobManager(
            redis_client=first_redis,
            namespace='test',
            worker_id='first-worker',
            state_store=state_store,
        )
        try:
            submitted = first_manager.submit('research_catalog', {})
            first = first_manager.run_job(submitted['job_id'])
            recovered_record = first_manager._load(submitted['job_id'])
            recovered_record.update({'status': 'queued', 'finished_at': None})
            recovered_record.pop('result', None)
            recovered_record.pop('execution', None)
        finally:
            first_manager.shutdown()

        second_manager = RedisJobManager(
            redis_client=InMemoryRedis(),
            namespace='test',
            worker_id='replacement-worker',
            state_store=state_store,
        )
        try:
            second_manager._save(recovered_record)
            with patch('src.redis_job_manager.run_tool') as run_tool:
                recovered = second_manager.run_job(submitted['job_id'])
            self.assertEqual(recovered['status'], 'completed')
            self.assertEqual(recovered['result'], first['result'])
            run_tool.assert_not_called()
        finally:
            second_manager.shutdown()

    def test_side_effecting_takeover_becomes_indeterminate_without_execution(self):
        class AttemptStateStore:
            def save(self, _record):
                return None

            def begin_execution_attempt(
                self,
                execution_key,
                job_id,
                fencing_token,
                attempt,
                semantics='pure',
            ):
                return {
                    'execution_key': execution_key,
                    'job_id': job_id,
                    'fencing_token': fencing_token,
                    'attempt': attempt,
                    'execution_semantics': semantics,
                    'status': 'indeterminate' if attempt > 1 else 'running',
                    'result': None,
                }

            def load_execution_result(self, _execution_key):
                return None

        executions = []
        manager = RedisJobManager(
            redis_client=InMemoryRedis(),
            namespace='indeterminate',
            worker_id='replacement-worker',
            state_store=AttemptStateStore(),
            tool_executor=type('Executor', (), {
                'execute': lambda self, tool, arguments, **kwargs: executions.append(
                    (tool, arguments)
                ),
                'shutdown': lambda self: None,
            })(),
        )
        try:
            submitted = manager.submit('research_catalog', {})
            record = manager._load(submitted['job_id'])
            record['_attempts'] = 1
            record['execution_semantics'] = 'side_effecting'
            manager._save(record)
            result = manager.run_job(submitted['job_id'])
            self.assertEqual(result['status'], 'indeterminate')
            self.assertTrue(result['indeterminate']['requires_manual_review'])
            self.assertEqual(executions, [])
            with self.assertRaisesRegex(ValueError, 'approve_retry'):
                manager.retry(submitted['job_id'])
            resolved = manager.resolve_indeterminate(
                submitted['job_id'],
                'approve_retry',
                'external system confirms no side effect was committed',
                'test-reviewer',
                evidence={'ticket': 'INC-1'},
            )
            self.assertFalse(resolved['indeterminate']['requires_manual_review'])
            self.assertEqual(resolved['resolution']['reviewer'], 'test-reviewer')
            retried = manager.retry(submitted['job_id'])
            self.assertEqual(retried['retry_of'], submitted['job_id'])
            self.assertEqual(retried['status'], 'queued')
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
