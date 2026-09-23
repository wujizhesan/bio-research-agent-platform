import asyncio
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from threading import Event
from time import time
import tempfile
import unittest
from unittest.mock import patch
from prometheus_client import generate_latest

from src.artifact_store import LocalArtifactStore
from src.database import Database, JobOutboxRow
from src.external_service_policy import ServiceRetryDeferredError
from src.job_execution import InlineToolExecutor
from src.job_state_store import DatabaseStateWriter
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
    def test_worker_claim_after_successful_dispatch_reconciliation_delay(self):
        with tempfile.TemporaryDirectory(prefix='worker_claim_dispatch_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'jobs.sqlite3').as_posix()}"
            database = Database(url)
            manager = RedisJobManager(
                redis_client=InMemoryRedis(), namespace='worker-claim-dispatch'
            )
            try:
                asyncio.run(database.init_schema())
                prepared = manager.prepare_durable('research_catalog', {})
                job_id = prepared['job_id']
                asyncio.run(database.stage_job(prepared))
                dispatched = asyncio.run(database.claim_dispatch_batch(
                    'dispatch-worker-test', limit=1
                ))[0]
                completed = asyncio.run(database.complete_dispatch_claims(
                    'dispatch-worker-test', [{
                        'job_id': job_id,
                        'generation': dispatched['_dispatch_generation'],
                        'succeeded': True,
                    }],
                ))
                self.assertEqual(completed, [job_id])

                async def retry_time():
                    async with database.sessions() as session:
                        outbox = await session.get(JobOutboxRow, job_id)
                        return outbox.next_attempt_at

                self.assertGreater(asyncio.run(retry_time()), time())
                claim = asyncio.run(database.claim_worker_job(
                    job_id,
                    prepared['_execution_key'],
                    'worker-after-dispatch',
                    dispatched['_claim_ticket'],
                    30,
                ))
                self.assertEqual(claim['job_id'], job_id)
            finally:
                manager.shutdown()
                asyncio.run(database.close())

    def test_durable_retry_reconciles_after_redis_transition_failure(self):
        with tempfile.TemporaryDirectory(prefix='durable_retry_torn_write_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'jobs.sqlite3').as_posix()}"
            redis = InMemoryRedis()
            calls = []

            def execute(_tool, _arguments):
                calls.append(True)
                if len(calls) == 1:
                    raise ServiceRetryDeferredError('uniprot', 45, 429)
                return {'status': 'ok', 'attempt': len(calls)}

            with patch.dict('os.environ', {'AUTO_CREATE_SCHEMA': 'true'}):
                writer = DatabaseStateWriter(url, require_job_scope=True)
            database = Database(url)
            manager = RedisJobManager(
                redis_client=redis,
                namespace='durable-retry-torn-write',
                worker_id='retry-worker',
                state_store=writer,
                tool_executor=InlineToolExecutor(execute),
            )
            try:
                prepared = manager.prepare_durable('research_catalog', {})
                job_id = prepared['job_id']
                asyncio.run(database.stage_job(prepared))
                first = asyncio.run(database.claim_dispatch_batch(
                    'first-dispatcher', limit=1
                ))[0]
                manager.rebuild_durable_queue(loader=lambda limit: [first])
                self.assertEqual(manager._next_job(), job_id)
                original_update = manager._store.atomic_update

                def fail_cache_transition(*args, **kwargs):
                    if kwargs.get('persist_state') is False:
                        raise ConnectionError('simulated Redis transition failure')
                    return original_update(*args, **kwargs)

                with patch.object(
                    manager._store, 'atomic_update', side_effect=fail_cache_transition
                ):
                    manager._complete_queued_item(job_id, 0.05)
                writer.flush()
                self.assertEqual(manager.get(job_id)['status'], 'running')
                self.assertEqual(
                    asyncio.run(database.get_job(job_id))['scheduling']['status'],
                    'waiting_for_external_service',
                )
                self.assertFalse(manager._store.processing_contains(job_id))
                self.assertIsNone(manager._next_job())
                self.assertIn(
                    'bio_agent_redis_deferred_cache_sync_failures_total'
                    '{namespace="durable-retry-torn-write"} 1.0',
                    generate_latest().decode('utf-8'),
                )
                self.assertEqual(asyncio.run(database.claim_dispatch_batch(
                    'early-dispatcher', limit=1
                )), [])

                async def release_retry():
                    async with database.sessions() as session:
                        outbox = await session.get(JobOutboxRow, job_id)
                        outbox.next_attempt_at = time() - 1
                        payload = dict(outbox.payload)
                        payload['_retry_not_before'] = time() - 1
                        outbox.payload = payload
                        await session.commit()

                asyncio.run(release_retry())
                second = asyncio.run(database.claim_dispatch_batch(
                    'second-dispatcher', limit=1
                ))[0]
                self.assertEqual(manager.rebuild_durable_queue(
                    loader=lambda limit: [second]
                ), [job_id])
                self.assertIn(
                    'bio_agent_redis_deferred_reconciliations_total'
                    '{mode="stale_cache",namespace="durable-retry-torn-write"} 1.0',
                    generate_latest().decode('utf-8'),
                )
                self.assertEqual(manager._next_job(), job_id)
                manager._complete_queued_item(job_id, 0.05)
                writer.flush()
                self.assertEqual(manager.get(job_id)['status'], 'completed')
                self.assertEqual(len(calls), 2)
            finally:
                manager.shutdown()
                writer.close()
                asyncio.run(database.close())

    def test_durable_external_retry_waits_and_survives_redis_loss(self):
        with tempfile.TemporaryDirectory(prefix='durable_retry_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'jobs.sqlite3').as_posix()}"
            redis = InMemoryRedis()
            calls = []

            def execute(_tool, _arguments):
                calls.append(True)
                if len(calls) == 1:
                    raise ServiceRetryDeferredError('uniprot', 45, 429)
                return {'status': 'ok', 'attempt': len(calls)}

            with patch.dict('os.environ', {'AUTO_CREATE_SCHEMA': 'true'}):
                writer = DatabaseStateWriter(url, require_job_scope=True)
            database = Database(url)
            manager = RedisJobManager(
                redis_client=redis,
                namespace='durable-retry',
                worker_id='first-worker',
                state_store=writer,
                tool_executor=InlineToolExecutor(execute),
            )
            recovered = None
            try:
                prepared = manager.prepare_durable('research_catalog', {})
                job_id = prepared['job_id']
                asyncio.run(database.stage_job(prepared))
                first_dispatch = asyncio.run(database.claim_dispatch_batch(
                    'first-dispatcher', limit=1
                ))[0]
                self.assertEqual(manager.rebuild_durable_queue(
                    loader=lambda limit: [first_dispatch]
                ), [job_id])
                self.assertEqual(manager._next_job(), job_id)
                manager._complete_queued_item(job_id, 0.05)
                writer.flush()

                waiting = manager.get(job_id)
                self.assertEqual(waiting['status'], 'queued')
                self.assertEqual(waiting['attempts'], 1)
                self.assertEqual(
                    waiting['scheduling']['status'],
                    'waiting_for_external_service',
                )
                self.assertEqual(
                    asyncio.run(database.get_job(job_id))['scheduling']['status'],
                    'waiting_for_external_service',
                )
                writer.save({
                    **prepared,
                    'status': 'running',
                    '_worker_id': 'first-worker',
                    '_fencing_token': '1',
                    '_attempts': 1,
                })
                writer.flush()
                self.assertEqual(
                    asyncio.run(database.get_job(job_id))['status'], 'queued'
                )
                self.assertEqual(manager.run_job(job_id)['status'], 'queued')
                self.assertEqual(len(calls), 1)
                self.assertIsNone(manager._next_job())
                self.assertEqual(asyncio.run(database.claim_dispatch_batch(
                    'early-dispatcher', limit=1
                )), [])
                self.assertIsNone(asyncio.run(database.claim_worker_job(
                    job_id,
                    prepared['_execution_key'],
                    'stale-worker',
                    first_dispatch['_claim_ticket'],
                    30,
                )))
                with self.assertRaisesRegex(RuntimeError, 'deferred'):
                    asyncio.run(database.store_execution_result(
                        prepared['_execution_key'],
                        job_id,
                        {'status': 'ok', 'stale': True},
                        '1',
                    ))

                redis.values.clear()
                redis.sorted_sets.clear()
                redis.lists.clear()
                redis.sets.clear()
                recovered = RedisJobManager(
                    redis_client=redis,
                    namespace='durable-retry',
                    worker_id='second-worker',
                    state_store=writer,
                    tool_executor=InlineToolExecutor(execute),
                )
                self.assertEqual(recovered.rebuild_durable_queue(), [])
                self.assertIsNone(recovered._next_job())

                async def release_retry():
                    async with database.sessions() as session:
                        outbox = await session.get(JobOutboxRow, job_id)
                        outbox.next_attempt_at = time() - 1
                        payload = dict(outbox.payload)
                        payload['_retry_not_before'] = time() - 1
                        outbox.payload = payload
                        await session.commit()

                asyncio.run(release_retry())
                second_dispatch = asyncio.run(database.claim_dispatch_batch(
                    'second-dispatcher', limit=1
                ))[0]
                self.assertEqual(recovered.rebuild_durable_queue(
                    loader=lambda limit: [second_dispatch]
                ), [job_id])
                self.assertEqual(recovered._next_job(), job_id)
                recovered._complete_queued_item(job_id, 0.05)
                writer.flush()
                self.assertEqual(recovered.get(job_id)['status'], 'completed')
                self.assertEqual(recovered.get(job_id)['result']['attempt'], 2)
                self.assertEqual(
                    asyncio.run(database.get_job(job_id))['status'],
                    'completed',
                )
                events = asyncio.run(database.list_job_events(job_id))
                waiting_event = next(
                    item for item in events
                    if (item['job'].get('scheduling') or {}).get('status')
                    == 'waiting_for_external_service'
                )
                replayed = asyncio.run(database.list_job_events(
                    job_id, after_event_id=waiting_event['event_id']
                ))
                self.assertTrue(any(
                    item['status'] == 'completed'
                    and item['revision'] > waiting_event['revision']
                    for item in replayed
                ))
            finally:
                if recovered is not None:
                    recovered.shutdown()
                manager.shutdown()
                writer.close()
                asyncio.run(database.close())

    def test_cancelling_deferred_retry_removes_durable_dispatch(self):
        with tempfile.TemporaryDirectory(prefix='cancel_deferred_retry_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'jobs.sqlite3').as_posix()}"
            redis = InMemoryRedis()
            with patch.dict('os.environ', {'AUTO_CREATE_SCHEMA': 'true'}):
                writer = DatabaseStateWriter(url, require_job_scope=True)
            database = Database(url)

            def defer(_tool, _arguments):
                raise ServiceRetryDeferredError('uniprot', 45, 429)

            manager = RedisJobManager(
                redis_client=redis,
                namespace='cancel-deferred-retry',
                state_store=writer,
                tool_executor=InlineToolExecutor(defer),
            )
            try:
                prepared = manager.prepare_durable('research_catalog', {})
                job_id = prepared['job_id']
                asyncio.run(database.stage_job(prepared))
                dispatched = asyncio.run(database.claim_dispatch_batch(
                    'cancel-dispatcher', limit=1
                ))[0]
                manager.rebuild_durable_queue(loader=lambda limit: [dispatched])
                self.assertEqual(manager._next_job(), job_id)
                manager._complete_queued_item(job_id, 0.05)
                writer.flush()
                redis.values.clear()
                redis.sorted_sets.clear()
                redis.lists.clear()
                redis.sets.clear()
                cancelled = asyncio.run(database.cancel_deferred_job(job_id))
                self.assertEqual(cancelled['status'], 'cancelled')
                self.assertEqual(
                    asyncio.run(database.get_job(job_id))['status'],
                    'cancelled',
                )
                self.assertEqual(asyncio.run(database.list_dispatchable_jobs()), [])
                self.assertEqual(manager.rebuild_durable_queue(), [])
            finally:
                manager.shutdown()
                writer.close()
                asyncio.run(database.close())

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
            self.assertEqual(
                result['error_code'],
                'execution_indeterminate',
            )
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
            self.assertEqual(failed['error'], 'tool execution failed')
            self.assertEqual(failed['error_code'], 'tool_execution_failed')
            self.assertEqual(failed['dead_letter_reason'], 'execution_failed')
            self.assertEqual(
                redis.lists['test:jobs:dead-letter'], [submitted['job_id']]
            )
        finally:
            manager.shutdown()

    def test_deferred_external_retry_is_not_reported_as_success(self):
        class DeferredExecutor:
            def execute(self, *_args, **_kwargs):
                raise ServiceRetryDeferredError('uniprot', 45, 429)

        class StateStore:
            def __init__(self):
                self.deferred = 0

            def save(self, _record):
                return None

            def defer_pure_job(self, *_args):
                self.deferred += 1
                return None

        redis = InMemoryRedis()
        state_store = StateStore()
        manager = RedisJobManager(
            redis_client=redis,
            namespace='deferred-external',
            tool_executor=DeferredExecutor(),
            state_store=state_store,
            max_attempts=1,
        )
        try:
            submitted = manager.submit('research_catalog', {})
            failed = manager.run_job(submitted['job_id'])
            self.assertEqual(failed['status'], 'failed')
            self.assertEqual(failed['error_code'], 'external_retry_deferred')
            self.assertEqual(failed['result']['retry_after_seconds'], 45)
            self.assertEqual(
                redis.lists['deferred-external:jobs:dead-letter'],
                [submitted['job_id']],
            )
            self.assertEqual(state_store.deferred, 0)
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


    def test_database_claim_provider_controls_attempt_and_fencing_token(self):
        class ClaimStore:
            require_job_scope = True

            def __init__(self):
                self.saved = []
                self.claims = 0

            def save(self, record):
                self.saved.append(dict(record))

            def claim_job(
                self,
                job_id,
                capability,
                worker_id,
                claim_ticket,
                lease_seconds,
            ):
                self.claims += 1
                self.claim_ticket = claim_ticket
                self.lease_seconds = lease_seconds
                return {
                    'job_id': job_id,
                    'worker_id': worker_id,
                    'attempt': 7,
                    'fencing_token': 'db-7',
                }

        state_store = ClaimStore()
        manager = RedisJobManager(
            redis_client=InMemoryRedis(),
            namespace='db-claim',
            state_store=state_store,
            worker_id='worker-db-claim',
        )
        try:
            submitted = manager.prepare('research_catalog', {})
            queued = manager._load(submitted['job_id'])
            queued['_claim_ticket'] = 'claim-ticket-7'
            manager._save(queued)
            claimed = manager._claim(manager._load(submitted['job_id']))
            self.assertEqual(claimed['_attempts'], 7)
            self.assertEqual(claimed['_fencing_token'], 'db-7')
            self.assertEqual(state_store.claims, 1)
            self.assertEqual(state_store.claim_ticket, 'claim-ticket-7')
            self.assertEqual(state_store.lease_seconds, manager.lease_seconds)
        finally:
            manager.shutdown()

    def test_prepare_durable_does_not_write_redis(self):
        redis_client = InMemoryRedis()
        manager = RedisJobManager(
            redis_client=redis_client,
            namespace='durable-api-boundary',
        )
        try:
            record = manager.prepare_durable(
                'research_catalog',
                {},
                idempotency_key='scoped:key',
                project_id='project-a',
            )
            self.assertIn('_execution_key', record)
            self.assertIsNone(manager._load(record['job_id']))
            self.assertFalse(manager._store.queue_contains(record['job_id']))
            self.assertIsNone(
                redis_client.get(manager._idempotency_key('scoped:key'))
            )
        finally:
            manager.shutdown()

    def test_worker_publishes_artifact_manifest_before_completion(self):
        class ArtifactExecutor:
            def execute(self, _tool, arguments, **_kwargs):
                target = Path(arguments['output_path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"status":"ok"}\n', encoding='utf-8')
                return {'status': 'ok', 'output_path': str(target)}

            def shutdown(self):
                return None

        with tempfile.TemporaryDirectory(prefix='redis_artifact_') as raw:
            root = Path(raw)
            input_dir = root / 'input'
            input_dir.mkdir()
            output_path = root / 'output' / 'index.json'
            manager = RedisJobManager(
                redis_client=InMemoryRedis(),
                namespace='artifact-publish',
                tool_executor=ArtifactExecutor(),
                artifact_store=LocalArtifactStore(),
                capability_routing=False,
                require_execution_fingerprint=False,
            )
            try:
                submitted = manager.submit('knowledge_ingest_directory', {
                    'input_dir': str(input_dir),
                    'output_path': str(output_path),
                })
                completed = manager.run_job(submitted['job_id'])
                self.assertEqual(completed['status'], 'completed')
                self.assertEqual(completed['result']['output_path'], str(output_path))
                self.assertEqual(len(completed['artifacts']), 1)
                artifact = completed['artifacts'][0]
                self.assertEqual(artifact['storage_backend'], 'local')
                self.assertEqual(artifact['path'], str(output_path))
                self.assertTrue(output_path.is_file())
                cached = manager._load_execution_result(
                    manager._load(submitted['job_id'])['_execution_key']
                )
                self.assertEqual(
                    cached['result']['schema'],
                    'bioagent.execution-result.v1',
                )
                self.assertEqual(cached['result']['result']['status'], 'ok')
            finally:
                manager.shutdown()

    def test_worker_commits_durable_artifact_lifecycle(self):
        class ArtifactExecutor:
            def execute(self, _tool, arguments, **_kwargs):
                target = Path(arguments['output_path'])
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('{"status":"ok"}\n', encoding='utf-8')
                return {'status': 'ok', 'output_path': str(target)}

            def shutdown(self):
                return None

        with tempfile.TemporaryDirectory(prefix='redis_artifact_db_') as raw:
            root = Path(raw)
            database_url = (
                f"sqlite+aiosqlite:///{(root / 'artifacts.sqlite3').as_posix()}"
            )
            input_dir = root / 'input'
            input_dir.mkdir()
            output_path = root / 'output' / 'index.json'
            writer = DatabaseStateWriter(database_url)
            manager = RedisJobManager(
                redis_client=InMemoryRedis(),
                namespace='artifact-lifecycle',
                state_store=writer,
                tool_executor=ArtifactExecutor(),
                artifact_store=LocalArtifactStore(),
                capability_routing=False,
                require_execution_fingerprint=False,
            )
            try:
                submitted = manager.submit('knowledge_ingest_directory', {
                    'input_dir': str(input_dir),
                    'output_path': str(output_path),
                })
                writer.flush()
                completed = manager.run_job(submitted['job_id'])
                writer.flush()
                database = Database(database_url)
                try:
                    artifacts = asyncio.run(
                        database.list_job_artifacts(submitted['job_id'])
                    )
                finally:
                    asyncio.run(database.close())
                self.assertEqual(completed['status'], 'completed')
                self.assertEqual(len(artifacts), 1)
                self.assertEqual(artifacts[0]['status'], 'committed')
                self.assertEqual(
                    artifacts[0]['publication_id'],
                    completed['artifacts'][0]['publication_id'],
                )
            finally:
                manager.shutdown()
                writer.close()


if __name__ == '__main__':
    unittest.main()
