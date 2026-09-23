"""Redis queue keys, records, events, and execution-result storage."""

import json
from inspect import Parameter, signature
import os
from threading import Lock
from time import time
from uuid import uuid4

try:
    from redis.exceptions import WatchError
except ImportError:
    class WatchError(Exception):
        pass


class RedisQueueStore:
    def __init__(self, redis_client, namespace, state_store=None,
                 result_ttl_seconds=86400):
        self.redis = redis_client
        self.namespace = namespace
        self.state_store = state_store
        self.result_ttl_seconds = result_ttl_seconds
        try:
            self.event_stream_maxlen = max(int(
                os.environ.get('JOB_EVENT_STREAM_MAXLEN', '1000')
            ), 10)
        except (TypeError, ValueError):
            self.event_stream_maxlen = 1000
        self._mutation_lock = Lock()
        self._fallback_fencing_token = 0

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
        return self.queue_keys_for()

    @property
    def route_index_key(self):
        return f'{self.namespace}:jobs:routes'

    def route_key(self, route_id):
        return f'{self.namespace}:jobs:route:{route_id}'

    def registered_route_ids(self):
        members = getattr(self.redis, 'smembers', None)
        if members is None:
            return ()
        return tuple(sorted(
            item.decode('utf-8') if isinstance(item, bytes) else str(item)
            for item in members(self.route_index_key)
        ))

    def register_route(self, record):
        routing = dict(record.get('routing') or {})
        route_id = routing.get('route_id')
        if not route_id:
            return None
        self.redis.set(
            self.route_key(route_id),
            json.dumps(routing, ensure_ascii=False, default=str),
        )
        add = getattr(self.redis, 'sadd', None)
        if add is not None:
            add(self.route_index_key, str(route_id))
        return str(route_id)

    def load_route(self, route_id):
        payload = self.redis.get(self.route_key(route_id))
        if not payload:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode('utf-8')
        return json.loads(payload)

    def queue_keys_for(self, route_ids=None, include_legacy=True):
        selected = (
            self.registered_route_ids()
            if route_ids is None else tuple(sorted(set(route_ids)))
        )
        high = [self.queue_for_priority(10, route_id) for route_id in selected]
        normal = [self.queue_for_priority(0, route_id) for route_id in selected]
        low = [self.queue_for_priority(-1, route_id) for route_id in selected]
        if include_legacy:
            high.insert(0, self.high_queue_key)
            normal.insert(0, self.queue_key)
            low.insert(0, self.low_queue_key)
        return tuple(high + normal + low)

    def queue_for_priority(self, priority, route_id=None):
        if route_id:
            base = f'{self.namespace}:jobs:route:{route_id}:queue'
            if priority >= 10:
                return f'{base}:high'
            if priority < 0:
                return f'{base}:low'
            return base
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

    def discard_prepared(self, job_id, idempotency_key=None, replacement_job_id=None):
        job_id = str(job_id)
        for queue_key in (*self.queue_keys, self.processing_key, self.dead_letter_key):
            self.redis.lrem(queue_key, 0, job_id)
        self.redis.delete(self.key(job_id))
        self.redis.delete(self.event_stream_key(job_id))
        self.redis.zrem(self.index_key, job_id)
        if idempotency_key:
            key = self.idempotency_key(idempotency_key)
            mapped = self.redis.get(key)
            if isinstance(mapped, bytes):
                mapped = mapped.decode('utf-8')
            if str(mapped or '') == job_id:
                if replacement_job_id:
                    self.redis.set(key, str(replacement_job_id))
                else:
                    self.redis.delete(key)

    def execution_result_key(self, value):
        return f'{self.namespace}:jobs:execution:{value}'

    def event_key(self, job_id):
        return f'{self.namespace}:job:{job_id}:events'

    def event_stream_key(self, job_id):
        return f'{self.namespace}:job:{job_id}:event-stream'

    @property
    def fencing_key(self):
        return f'{self.namespace}:jobs:fencing-token'

    @staticmethod
    def public_record(record):
        output = dict(record)
        output.pop('_arguments', None)
        output.pop('_cancel_requested', None)
        output.pop('_created_score', None)
        output.pop('idempotency_key', None)
        output.pop('_worker_id', None)
        output.pop('_lease_until', None)
        output.pop('_fencing_token', None)
        output.pop('_execution_key', None)
        output.pop('_claim_ticket', None)
        output.pop('_dispatch_generation', None)
        output.pop('_retry_not_before', None)
        output.pop('_deferred_attempt', None)
        output.pop('_started_epoch', None)
        output.pop('_capability_routing', None)
        revision = output.pop('_revision', None)
        if revision is not None:
            output['revision'] = int(revision)
        if '_attempts' in record:
            output['attempts'] = record['_attempts']
        if record.get('_cancel_requested'):
            output['cancel_requested'] = True
        return output

    def server_time(self):
        reader = getattr(self.redis, 'time', None)
        if reader is None:
            return time()
        value = reader()
        if isinstance(value, (list, tuple)) and len(value) == 2:
            return float(value[0]) + float(value[1]) / 1_000_000
        return float(value)

    def next_fencing_token(self):
        increment = getattr(self.redis, 'incr', None)
        if increment is not None:
            sequence = int(increment(self.fencing_key))
        else:
            with self._mutation_lock:
                self._fallback_fencing_token += 1
                sequence = self._fallback_fencing_token
        epoch = int(self.server_time() * 1_000_000)
        return f'{epoch}-{sequence}-{uuid4().hex}'

    def _event_payload(self, record):
        return json.dumps(
            self.public_record(record),
            ensure_ascii=False,
            default=str,
        )

    def _append_event(self, client, record):
        payload = self._event_payload(record)
        append = getattr(client, 'xadd', None)
        event_id = None
        if append:
            event_id = append(
                self.event_stream_key(record['job_id']),
                {'payload': payload},
                maxlen=self.event_stream_maxlen,
                approximate=True,
            )
        publish = getattr(client, 'publish', None)
        if publish:
            publish(self.event_key(record['job_id']), payload)
        if isinstance(event_id, bytes):
            event_id = event_id.decode('utf-8')
        return str(event_id) if event_id is not None else None

    def _persist_state_store(self, record, event_id=None):
        if self.state_store is not None:
            durable = dict(record)
            if event_id:
                durable['_event_id'] = str(event_id)
            self.state_store.save(durable)

    def save(self, record, persist_state=True):
        score = record.get('_created_score')
        if score is None:
            score = self.server_time()
            record['_created_score'] = score
        record['_revision'] = int(record.get('_revision', 0)) + 1
        payload = json.dumps(record, ensure_ascii=False, default=str)
        self.redis.set(self.key(record['job_id']), payload)
        self.redis.zadd(self.index_key, {record['job_id']: score})
        event_id = self._append_event(self.redis, record)
        if persist_state:
            self._persist_state_store(record, event_id)

    def atomic_update(self, job_id, updater, retries=8, persist_state=True):
        key = self.key(str(job_id))
        pipeline_factory = getattr(self.redis, 'pipeline', None)
        if pipeline_factory is None:
            with self._mutation_lock:
                current = self.load(job_id)
                if current is None:
                    return None, False
                updated = updater(dict(current), self.server_time())
                if updated is None:
                    return current, False
                updated['_revision'] = max(
                    int(current.get('_revision', 0)) + 1,
                    int(updated.get('_revision', 0)),
                )
                score = updated.get('_created_score', self.server_time())
                updated['_created_score'] = score
                payload = json.dumps(updated, ensure_ascii=False, default=str)
                self.redis.set(key, payload)
                self.redis.zadd(self.index_key, {updated['job_id']: score})
                event_id = self._append_event(self.redis, updated)
                if persist_state:
                    self._persist_state_store(updated, event_id)
                return updated, True

        for _ in range(max(int(retries), 1)):
            pipe = pipeline_factory()
            try:
                pipe.watch(key)
                payload = pipe.get(key)
                if not payload:
                    pipe.unwatch()
                    return None, False
                if isinstance(payload, bytes):
                    payload = payload.decode('utf-8')
                current = json.loads(payload)
                updated = updater(dict(current), self.server_time())
                if updated is None:
                    pipe.unwatch()
                    return current, False
                updated['_revision'] = max(
                    int(current.get('_revision', 0)) + 1,
                    int(updated.get('_revision', 0)),
                )
                score = updated.get('_created_score', self.server_time())
                updated['_created_score'] = score
                encoded = json.dumps(updated, ensure_ascii=False, default=str)
                event_payload = self._event_payload(updated)
                pipe.multi()
                pipe.set(key, encoded)
                pipe.zadd(self.index_key, {updated['job_id']: score})
                pipe.xadd(
                    self.event_stream_key(updated['job_id']),
                    {'payload': event_payload},
                    maxlen=self.event_stream_maxlen,
                    approximate=True,
                )
                pipe.publish(self.event_key(updated['job_id']), event_payload)
                results = pipe.execute()
                event_id = results[2] if len(results) > 2 else None
                if isinstance(event_id, bytes):
                    event_id = event_id.decode('utf-8')
                if persist_state:
                    self._persist_state_store(updated, event_id)
                return updated, True
            except WatchError:
                continue
            finally:
                reset = getattr(pipe, 'reset', None)
                if reset:
                    reset()
        raise RuntimeError(f'concurrent job update did not converge: {job_id}')

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

    def read_job_events(self, job_id, last_event_id='0-0', block_ms=1000, count=100):
        reader = getattr(self.redis, 'xread', None)
        if reader is None:
            return []
        response = reader(
            {self.event_stream_key(str(job_id)): last_event_id},
            count=max(int(count), 1),
            block=max(int(block_ms), 0),
        )
        events = []
        for _, entries in response or []:
            for event_id, fields in entries:
                if isinstance(event_id, bytes):
                    event_id = event_id.decode('utf-8')
                payload = fields.get('payload')
                if payload is None:
                    payload = fields.get(b'payload')
                if isinstance(payload, bytes):
                    payload = payload.decode('utf-8')
                if payload:
                    events.append((str(event_id), json.loads(payload)))
        return events

    def enqueue(self, job_id, priority=0, route_id=None):
        self.redis.lpush(
            self.queue_for_priority(priority, route_id),
            str(job_id),
        )

    def next_job(self, route_ids=None, include_legacy=True):
        move = getattr(self.redis, 'rpoplpush', None)
        for queue_key in self.queue_keys_for(route_ids, include_legacy):
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

    def processing_contains(self, job_id):
        job_id = str(job_id)
        locate = getattr(self.redis, 'lpos', None)
        if locate is not None:
            return locate(self.processing_key, job_id) is not None
        return any(
            (item.decode('utf-8') if isinstance(item, bytes) else str(item))
            == job_id
            for item in self.redis.lrange(self.processing_key, 0, -1)
        )

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
        routes = self.registered_route_ids()
        return {
            'high': sum(
                self.redis.llen(self.queue_for_priority(10, route_id))
                for route_id in (None, *routes)
            ),
            'normal': sum(
                self.redis.llen(self.queue_for_priority(0, route_id))
                for route_id in (None, *routes)
            ),
            'low': sum(
                self.redis.llen(self.queue_for_priority(-1, route_id))
                for route_id in (None, *routes)
            ),
            'dead_letter': self.redis.llen(self.dead_letter_key),
        }

    def _load_redis_execution_result(self, execution_key):
        payload = self.redis.get(self.execution_result_key(execution_key))
        if not payload:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode('utf-8')
        return json.loads(payload)

    @staticmethod
    def _call_with_supported_keywords(callable_value, *args, **kwargs):
        parameters = signature(callable_value).parameters.values()
        accepts_kwargs = any(
            parameter.kind == Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        supported = kwargs if accepts_kwargs else {
            key: value
            for key, value in kwargs.items()
            if key in signature(callable_value).parameters
        }
        return callable_value(*args, **supported)

    def load_execution_result(
        self,
        execution_key,
        job_id=None,
        fencing_token=None,
        worker_id=None,
        attempt=None,
    ):
        durable_loader = getattr(self.state_store, 'load_execution_result', None)
        if durable_loader is not None:
            durable = self._call_with_supported_keywords(
                durable_loader,
                execution_key,
                job_id=job_id,
                fencing_token=fencing_token,
                worker_id=worker_id,
                attempt=attempt,
            )
            if durable is not None and durable.get('status') == 'completed':
                payload = {
                    'result': durable['result'],
                    'job_id': durable.get('job_id'),
                    'fencing_token': durable.get('fencing_token'),
                }
                self.redis.set(
                    self.execution_result_key(execution_key),
                    json.dumps(payload, ensure_ascii=False, default=str),
                    ex=self.result_ttl_seconds,
                )
                return payload
        cached = self._load_redis_execution_result(execution_key)
        if (
            cached is not None
            and durable_loader is not None
            and fencing_token is not None
            and str(cached.get('fencing_token', '')) != str(fencing_token)
        ):
            self.redis.delete(self.execution_result_key(execution_key))
            cached = None
        durable_saver = getattr(self.state_store, 'store_execution_result', None)
        if cached is not None and durable_saver is not None and job_id is not None:
            publications = [
                str(item['publication_id'])
                for item in (
                    cached.get('result', {}).get('artifacts', [])
                    if isinstance(cached.get('result'), dict) else []
                )
                if isinstance(item, dict) and item.get('publication_id')
            ]
            if publications:
                durable_saver = getattr(
                    self.state_store,
                    'store_execution_result_with_artifacts',
                    durable_saver,
                )
            durable = self._call_with_supported_keywords(
                durable_saver,
                execution_key,
                job_id,
                cached['result'],
                publication_ids=publications,
                fencing_token=fencing_token,
                worker_id=worker_id,
                attempt=attempt,
            )
            return {'result': durable['result']}
        return cached

    def store_execution_result(
        self,
        execution_key,
        result,
        job_id=None,
        fencing_token=None,
        worker_id=None,
        attempt=None,
        publication_ids=None,
    ):
        publication_ids = [str(value) for value in (publication_ids or [])]
        payload = json.dumps({
            'result': result,
            'job_id': job_id,
            'fencing_token': fencing_token,
        }, ensure_ascii=False, default=str)
        key = self.execution_result_key(execution_key)
        durable_saver = getattr(self.state_store, 'store_execution_result', None)
        if durable_saver is not None and job_id is not None:
            if publication_ids:
                durable_saver = getattr(
                    self.state_store,
                    'store_execution_result_with_artifacts',
                    None,
                )
                if durable_saver is None:
                    raise RuntimeError(
                        'durable artifact transaction is unavailable'
                    )
            durable = self._call_with_supported_keywords(
                durable_saver,
                execution_key,
                job_id,
                result,
                publication_ids=publication_ids,
                fencing_token=fencing_token,
                worker_id=worker_id,
                attempt=attempt,
            )
            result = durable['result']
            payload = json.dumps({
                'result': result,
                'job_id': job_id,
                'fencing_token': durable.get('fencing_token', fencing_token),
            }, ensure_ascii=False, default=str)
            self.redis.set(key, payload, ex=self.result_ttl_seconds)
            return result
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
            cached = self._load_redis_execution_result(execution_key)
            result = cached['result'] if cached else result
        return result
