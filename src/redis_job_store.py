"""Redis queue keys, records, events, and execution-result storage."""

import json
from time import time


class RedisQueueStore:
    def __init__(self, redis_client, namespace, state_store=None,
                 result_ttl_seconds=86400):
        self.redis = redis_client
        self.namespace = namespace
        self.state_store = state_store
        self.result_ttl_seconds = result_ttl_seconds

    def key(self, job_id):
        return f'{self.namespace}:job:{job_id}'

    @property
    def index_key(self):
        return f'{self.namespace}:jobs:index'

    @property
    def queue_key(self):
        return f'{self.namespace}:jobs:queue'

    @property
    def high_queue_key(self):
        return f'{self.namespace}:jobs:queue:high'

    @property
    def low_queue_key(self):
        return f'{self.namespace}:jobs:queue:low'

    @property
    def queue_keys(self):
        return self.high_queue_key, self.queue_key, self.low_queue_key

    def queue_for_priority(self, priority):
        if priority >= 10:
            return self.high_queue_key
        if priority < 0:
            return self.low_queue_key
        return self.queue_key

    @property
    def processing_key(self):
        return f'{self.namespace}:jobs:processing'

    @property
    def dead_letter_key(self):
        return f'{self.namespace}:jobs:dead-letter'

    def idempotency_key(self, value):
        return f'{self.namespace}:jobs:idempotency:{value}'

    def get_idempotent_job(self, value):
        return self.redis.get(self.idempotency_key(value))

    def set_idempotent_job(self, value, job_id):
        self.redis.set(self.idempotency_key(value), job_id)

    def execution_result_key(self, value):
        return f'{self.namespace}:jobs:execution:{value}'

    def event_key(self, job_id):
        return f'{self.namespace}:job:{job_id}:events'

    @staticmethod
    def public_record(record):
        output = dict(record)
        output.pop('_arguments', None)
        output.pop('_cancel_requested', None)
        output.pop('_created_score', None)
        output.pop('idempotency_key', None)
        output.pop('_worker_id', None)
        output.pop('_lease_until', None)
        output.pop('_execution_key', None)
        output.pop('_started_epoch', None)
        if '_attempts' in record:
            output['attempts'] = record['_attempts']
        if record.get('_cancel_requested'):
            output['cancel_requested'] = True
        return output

    def save(self, record):
        score = record.get('_created_score')
        if score is None:
            score = time()
            record['_created_score'] = score
        payload = json.dumps(record, ensure_ascii=False, default=str)
        self.redis.set(self.key(record['job_id']), payload)
        self.redis.zadd(self.index_key, {record['job_id']: score})
        if self.state_store is not None:
            self.state_store.save(record)
        publish = getattr(self.redis, 'publish', None)
        if publish:
            publish(
                self.event_key(record['job_id']),
                json.dumps(
                    self.public_record(record),
                    ensure_ascii=False,
                    default=str,
                ),
            )

    def load(self, job_id):
        payload = self.redis.get(self.key(job_id))
        if not payload:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode('utf-8')
        return json.loads(payload)

    def list_records(self, limit=20):
        try:
            size = min(max(int(limit), 1), 100)
        except (TypeError, ValueError):
            size = 20
        job_ids = self.redis.zrevrange(self.index_key, 0, size - 1)
        records = []
        for job_id in job_ids:
            record = self.load(job_id)
            if record:
                records.append(self.public_record(record))
        return records

    def subscribe_job_events(self, job_id):
        pubsub_factory = getattr(self.redis, 'pubsub', None)
        if pubsub_factory is None:
            return None
        pubsub = pubsub_factory()
        pubsub.subscribe(self.event_key(str(job_id)))
        return pubsub

    def enqueue(self, job_id, priority=0):
        self.redis.lpush(self.queue_for_priority(priority), str(job_id))

    def next_job(self):
        move = getattr(self.redis, 'rpoplpush', None)
        for queue_key in self.queue_keys:
            if move is not None:
                item = move(queue_key, self.processing_key)
            else:
                item = self.redis.brpoplpush(
                    queue_key,
                    self.processing_key,
                    timeout=0,
                )
            if item:
                return item
        return None

    def acknowledge(self, job_id):
        self.redis.lrem(self.processing_key, 0, str(job_id))

    def queue_contains(self, job_id):
        job_id = str(job_id)
        locate = getattr(self.redis, 'lpos', None)
        if locate is not None:
            return any(
                locate(queue_key, job_id) is not None
                for queue_key in self.queue_keys
            )
        for queue_key in self.queue_keys:
            queued = self.redis.lrange(queue_key, 0, -1)
            if any(
                (item.decode('utf-8') if isinstance(item, bytes) else str(item))
                == job_id
                for item in queued
            ):
                return True
        return False

    def move_to_dead_letter(self, job_id):
        job_id = str(job_id)
        self.redis.lrem(self.dead_letter_key, 0, job_id)
        self.redis.lpush(self.dead_letter_key, job_id)

    def queue_depths(self):
        return {
            'queue': sum(self.redis.llen(key) for key in self.queue_keys),
            'processing': self.redis.llen(self.processing_key),
            'dead_letter': self.redis.llen(self.dead_letter_key),
        }

    def priority_depths(self):
        return {
            'high': self.redis.llen(self.high_queue_key),
            'normal': self.redis.llen(self.queue_key),
            'low': self.redis.llen(self.low_queue_key),
            'dead_letter': self.redis.llen(self.dead_letter_key),
        }

    def load_execution_result(self, execution_key):
        payload = self.redis.get(self.execution_result_key(execution_key))
        if not payload:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode('utf-8')
        return json.loads(payload)

    def store_execution_result(self, execution_key, result):
        payload = json.dumps({'result': result}, ensure_ascii=False, default=str)
        key = self.execution_result_key(execution_key)
        try:
            stored = self.redis.set(
                key,
                payload,
                ex=self.result_ttl_seconds,
                nx=True,
            )
        except TypeError:
            stored = self.redis.set(key, payload)
        if stored is False or stored is None:
            cached = self.load_execution_result(execution_key)
            return cached['result'] if cached else result
        return result
