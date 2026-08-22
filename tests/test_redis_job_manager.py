from collections import defaultdict
import json
import unittest

from src.redis_job_manager import RedisJobManager


class InMemoryRedis:
    def __init__(self):
        self.values = {}
        self.sorted_sets = defaultdict(dict)
        self.lists = defaultdict(list)

    def ping(self):
        return True

    def set(self, key, value):
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

    def brpop(self, key, timeout=0):
        if not self.lists[key]:
            return None
        return key, self.lists[key].pop(0)

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
            self.assertNotIn('_created_score', completed)
        finally:
            manager.shutdown()


if __name__ == '__main__':
    unittest.main()
