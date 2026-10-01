"""TTL-backed Redis registry for heterogeneous workers."""

import json
import os
from time import time

try:
    from .resource_scheduling import ResourceCapacity, ResourceRequest
except ImportError:
    from resource_scheduling import ResourceCapacity, ResourceRequest


class RedisWorkerRegistry:
    def __init__(self, redis_client, namespace, ttl_seconds=None):
        self.redis = redis_client
        self.namespace = namespace
        try:
            configured = ttl_seconds or os.environ.get(
                'WORKER_REGISTRY_TTL_SECONDS',
                '30',
            )
            self.ttl_seconds = max(int(configured), 5)
        except (TypeError, ValueError):
            self.ttl_seconds = 30

    @property
    def index_key(self):
        return f'{self.namespace}:workers:index'

    def key(self, worker_id):
        return f'{self.namespace}:worker:{worker_id}'

    def heartbeat(self, record, now=None):
        timestamp = float(time() if now is None else now)
        value = dict(record)
        value['last_heartbeat_epoch'] = timestamp
        value['expires_at_epoch'] = timestamp + self.ttl_seconds
        payload = json.dumps(value, ensure_ascii=False, default=str)
        self.redis.set(self.key(value['worker_id']), payload, ex=self.ttl_seconds)
        self.redis.zadd(
            self.index_key,
            {value['worker_id']: value['expires_at_epoch']},
        )
        return value

    def unregister(self, worker_id):
        delete = getattr(self.redis, 'delete', None)
        if delete is not None:
            delete(self.key(worker_id))
        zrem = getattr(self.redis, 'zrem', None)
        if zrem is not None:
            zrem(self.index_key, str(worker_id))

    def list_active(self, now=None):
        timestamp = float(time() if now is None else now)
        prune = getattr(self.redis, 'zremrangebyscore', None)
        if prune is not None:
            prune(self.index_key, '-inf', timestamp)
        range_by_score = getattr(self.redis, 'zrangebyscore', None)
        if range_by_score is not None:
            worker_ids = range_by_score(self.index_key, timestamp, '+inf')
        else:
            worker_ids = self.redis.zrevrange(self.index_key, 0, -1)
        records = []
        for raw_worker_id in worker_ids:
            worker_id = (
                raw_worker_id.decode('utf-8')
                if isinstance(raw_worker_id, bytes)
                else str(raw_worker_id)
            )
            payload = self.redis.get(self.key(worker_id))
            if not payload:
                continue
            if isinstance(payload, bytes):
                payload = payload.decode('utf-8')
            record = json.loads(payload)
            if float(record.get('expires_at_epoch', 0)) > timestamp:
                records.append(record)
        return records

    def compatible_workers(self, record, now=None):
        request = ResourceRequest.from_mapping(record.get('resources'))
        expected = (record.get('execution_identity') or {}).get('fingerprint')
        tool = record.get('tool')
        compatible = []
        for worker in self.list_active(now=now):
            if worker.get('draining'):
                continue
            if worker.get('accepting_work') is False:
                continue
            capacity_value = dict(worker.get('capacity') or {})
            capacity_value['labels'] = tuple(capacity_value.get('labels') or ())
            try:
                capacity = ResourceCapacity(**capacity_value)
            except (TypeError, ValueError):
                continue
            identity = (worker.get('execution_catalog') or {}).get(tool) or {}
            if capacity.fits(request) and identity.get('fingerprint') == expected:
                compatible.append(worker)
        return compatible
