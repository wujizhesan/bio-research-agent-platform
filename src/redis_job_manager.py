"""Redis-backed job state and queue for horizontally scalable workers."""

from datetime import datetime, timezone
from contextlib import nullcontext
import json
import os
from threading import Lock
from time import time
from uuid import uuid4

try:
    from .domain_registry import active_tool_specs, run_tool, tool_specs
    from .job_manager import TERMINAL_STATUSES
    from .observability import (
        REDIS_JOB_DURATION,
        REDIS_JOB_EXECUTIONS,
        REDIS_JOB_RETRIES,
        REDIS_PROCESSING_DEPTH,
        REDIS_QUEUE_DEPTH,
        REDIS_RESULT_CACHE,
        REDIS_WORKER_ACTIVE,
    )
except ImportError:
    from domain_registry import active_tool_specs, run_tool, tool_specs
    from job_manager import TERMINAL_STATUSES
    from observability import (
        REDIS_JOB_DURATION,
        REDIS_JOB_EXECUTIONS,
        REDIS_JOB_RETRIES,
        REDIS_PROCESSING_DEPTH,
        REDIS_QUEUE_DEPTH,
        REDIS_RESULT_CACHE,
        REDIS_WORKER_ACTIVE,
    )


def _now():
    return datetime.now(timezone.utc).isoformat()


class RedisJobManager:
    backend = 'redis'

    def __init__(self, redis_url=None, namespace=None, redis_client=None, lease_seconds=None, worker_id=None, result_ttl_seconds=None, state_store=None, redis_socket_timeout=None):
        if redis_client is None:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError('Redis backend requires the redis package') from exc
            configured_socket_timeout = redis_socket_timeout or os.environ.get('REDIS_SOCKET_TIMEOUT', '15')
            try:
                socket_timeout = max(float(configured_socket_timeout), 6.0)
            except (TypeError, ValueError):
                socket_timeout = 15.0
            redis_client = redis.Redis.from_url(
                redis_url or os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'),
                decode_responses=True,
                socket_timeout=socket_timeout,
            )
        self.redis = redis_client
        self.namespace = namespace or os.environ.get('REDIS_NAMESPACE', 'bioagent')
        configured_lease = lease_seconds or os.environ.get('JOB_LEASE_SECONDS', '300')
        try:
            self.lease_seconds = max(int(configured_lease), 1)
        except (TypeError, ValueError):
            self.lease_seconds = 300
        configured_ttl = result_ttl_seconds or os.environ.get('JOB_RESULT_TTL_SECONDS', '86400')
        try:
            self.result_ttl_seconds = max(int(configured_ttl), 60)
        except (TypeError, ValueError):
            self.result_ttl_seconds = 86400
        self.worker_id = worker_id or f'worker-{uuid4().hex}'
        self.state_store = state_store
        self._lock = Lock()
        self.redis.ping()

    def _key(self, job_id):
        return f'{self.namespace}:job:{job_id}'

    @property
    def _index_key(self):
        return f'{self.namespace}:jobs:index'

    @property
    def _queue_key(self):
        return f'{self.namespace}:jobs:queue'

    @property
    def _processing_key(self):
        return f'{self.namespace}:jobs:processing'

    def _idempotency_key(self, value):
        return f'{self.namespace}:jobs:idempotency:{value}'

    def _execution_result_key(self, value):
        return f'{self.namespace}:jobs:execution:{value}'

    def _event_key(self, job_id):
        return f'{self.namespace}:job:{job_id}:events'

    @staticmethod
    def _public_record(record):
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

    def _save(self, record):
        self.redis.set(self._key(record['job_id']), json.dumps(record, ensure_ascii=False, default=str))
        score = record.get('_created_score')
        if score is None:
            score = time()
            record['_created_score'] = score
            self.redis.set(self._key(record['job_id']), json.dumps(record, ensure_ascii=False, default=str))
        self.redis.zadd(self._index_key, {record['job_id']: score})
        if self.state_store is not None:
            self.state_store.save(record)
        publish = getattr(self.redis, 'publish', None)
        if publish:
            publish(self._event_key(record['job_id']), json.dumps(self._public_record(record), ensure_ascii=False, default=str))

    def _load(self, job_id):
        payload = self.redis.get(self._key(job_id))
        if not payload:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode('utf-8')
        return json.loads(payload)

    @staticmethod
    def _validate_tool_state(tool):
        known = {spec['name'] for spec in tool_specs()}
        if tool not in known:
            raise ValueError(f'unknown tool: {tool}')
        if tool not in {spec['name'] for spec in active_tool_specs()}:
            raise ValueError(f'plugin domain is disabled for tool: {tool}')

    def _create_job(self, tool, arguments, retry_of=None, idempotency_key=None):
        job_id = uuid4().hex
        record = {
            'job_id': job_id,
            'tool': tool,
            'status': 'queued',
            'created_at': _now(),
            '_arguments': dict(arguments),
            '_cancel_requested': False,
            '_created_score': time(),
            '_execution_key': uuid4().hex,
        }
        if retry_of:
            record['retry_of'] = retry_of
        if idempotency_key:
            record['idempotency_key'] = idempotency_key
            self.redis.set(self._idempotency_key(idempotency_key), job_id)
        self._save(record)
        self.redis.lpush(self._queue_key, job_id)
        self._refresh_queue_metrics()
        return self._public_record(record)

    def submit(self, tool, arguments, idempotency_key=None):
        if not isinstance(tool, str) or not tool:
            raise ValueError('tool is required')
        if not isinstance(arguments, dict):
            raise ValueError('arguments must be an object')
        if idempotency_key is not None:
            if not isinstance(idempotency_key, str) or not idempotency_key.strip():
                raise ValueError('idempotency key must be a non-empty string')
            idempotency_key = idempotency_key.strip()
            if len(idempotency_key) > 128:
                raise ValueError('idempotency key is too long')
        self._validate_tool_state(tool)
        distributed_lock = getattr(self.redis, 'lock', None)
        guard = distributed_lock(f'{self.namespace}:jobs:submit-lock', timeout=10, blocking_timeout=10) if distributed_lock else nullcontext()
        with self._lock, guard:
            if idempotency_key:
                existing_id = self.redis.get(self._idempotency_key(idempotency_key))
                if existing_id:
                    existing = self._load(existing_id)
                    if existing is not None:
                        if existing.get('tool') != tool or existing.get('_arguments') != arguments:
                            raise ValueError('idempotency key already used with different job payload')
                        output = self._public_record(existing)
                        output['deduplicated'] = True
                        return output
            return self._create_job(tool, arguments, idempotency_key=idempotency_key)

    def get(self, job_id):
        record = self._load(str(job_id))
        return self._public_record(record) if record else None

    def subscribe_job_events(self, job_id):
        pubsub_factory = getattr(self.redis, 'pubsub', None)
        if pubsub_factory is None:
            return None
        pubsub = pubsub_factory()
        pubsub.subscribe(self._event_key(str(job_id)))
        return pubsub

    def list(self, limit=20):
        try:
            size = min(max(int(limit), 1), 100)
        except (TypeError, ValueError):
            size = 20
        job_ids = self.redis.zrevrange(self._index_key, 0, size - 1)
        records = []
        for job_id in job_ids:
            record = self._load(job_id)
            if record:
                records.append(self._public_record(record))
        return records

    def cancel(self, job_id):
        record = self._load(str(job_id))
        if record is None:
            raise ValueError(f'job not found: {job_id}')
        if record.get('status') in TERMINAL_STATUSES:
            return self._public_record(record)
        record['_cancel_requested'] = True
        self._save(record)
        return self._public_record(record)

    def retry(self, job_id):
        record = self._load(str(job_id))
        if record is None:
            raise ValueError(f'job not found: {job_id}')
        if record.get('status') not in TERMINAL_STATUSES:
            raise ValueError('only completed, failed or cancelled jobs can be retried')
        arguments = record.get('_arguments')
        if arguments is None:
            raise ValueError('job arguments are unavailable')
        self._validate_tool_state(record['tool'])
        return self._create_job(record['tool'], arguments, retry_of=record['job_id'])

    @staticmethod
    def _lease_active(record, now=None):
        lease_until = record.get('_lease_until')
        if lease_until is None:
            return False
        try:
            return float(lease_until) > (now or time())
        except (TypeError, ValueError):
            return False

    def _claim(self, record):
        record.update({
            'status': 'running',
            'started_at': _now(),
            '_worker_id': self.worker_id,
            '_lease_until': time() + self.lease_seconds,
            '_attempts': int(record.get('_attempts', 0)) + 1,
            '_started_epoch': time(),
        })
        self._save(record)
        if record['_attempts'] > 1 or record.get('retry_of'):
            REDIS_JOB_RETRIES.labels(record['tool']).inc()
        REDIS_WORKER_ACTIVE.labels(self.namespace).inc()

    def _ack(self, job_id):
        self.redis.lrem(self._processing_key, 0, str(job_id))
        self._refresh_queue_metrics()

    def _refresh_queue_metrics(self):
        try:
            queue_size = self.redis.llen(self._queue_key)
            processing_size = self.redis.llen(self._processing_key)
        except Exception:
            return
        REDIS_QUEUE_DEPTH.labels(self.namespace).set(queue_size)
        REDIS_PROCESSING_DEPTH.labels(self.namespace).set(processing_size)

    def _load_execution_result(self, execution_key):
        payload = self.redis.get(self._execution_result_key(execution_key))
        if not payload:
            return None
        if isinstance(payload, bytes):
            payload = payload.decode('utf-8')
        return json.loads(payload)

    def _store_execution_result(self, execution_key, result):
        payload = json.dumps({'result': result}, ensure_ascii=False, default=str)
        key = self._execution_result_key(execution_key)
        try:
            stored = self.redis.set(key, payload, ex=self.result_ttl_seconds, nx=True)
        except TypeError:
            stored = self.redis.set(key, payload)
        if stored is False or stored is None:
            cached = self._load_execution_result(execution_key)
            return cached['result'] if cached else result
        return result

    def _finish(self, job_id, result, failed=False):
        update = {
            'status': 'failed' if failed else 'completed',
            'finished_at': _now(),
            'result': result,
        }
        if failed:
            update['error'] = result.get('error', 'tool returned an error')
        current = self._load(str(job_id))
        if current is None:
            return None
        if (
            current.get('status') == 'running'
            and current.get('_worker_id') not in (None, self.worker_id)
            and self._lease_active(current)
        ):
            return self._public_record(current)
        if current.get('_cancel_requested'):
            update = {'status': 'cancelled', 'finished_at': _now(), 'error': 'job cancelled by user'}
        current.update(update)
        started_epoch = current.pop('_started_epoch', None)
        if started_epoch is not None:
            try:
                REDIS_JOB_DURATION.labels(current['tool']).observe(max(time() - float(started_epoch), 0))
            except (TypeError, ValueError):
                pass
        current.pop('_worker_id', None)
        current.pop('_lease_until', None)
        self._save(current)
        REDIS_JOB_EXECUTIONS.labels(current['tool'], current['status']).inc()
        REDIS_WORKER_ACTIVE.labels(self.namespace).dec()
        return self._public_record(current)

    def recover_stale_jobs(self):
        processing_ids = self.redis.lrange(self._processing_key, 0, -1)
        recovered = []
        now = time()
        for raw_job_id in processing_ids:
            job_id = raw_job_id.decode('utf-8') if isinstance(raw_job_id, bytes) else str(raw_job_id)
            distributed_lock = getattr(self.redis, 'lock', None)
            guard = distributed_lock(
                f'{self.namespace}:jobs:recover:{job_id}',
                timeout=10,
                blocking_timeout=1,
            ) if distributed_lock else nullcontext()
            try:
                with self._lock, guard:
                    record = self._load(job_id)
                    if record is None or record.get('status') in TERMINAL_STATUSES:
                        self._ack(job_id)
                        continue
                    if self._lease_active(record, now):
                        continue
                    record.pop('started_at', None)
                    record.pop('error', None)
                    record.pop('_worker_id', None)
                    record.pop('_lease_until', None)
                    record.pop('_started_epoch', None)
                    record.update({'status': 'queued', 'recovered_at': _now()})
                    self._save(record)
                    self.redis.lpush(self._queue_key, job_id)
                    self._ack(job_id)
                    recovered.append(job_id)
            except Exception:
                continue
        self._refresh_queue_metrics()
        return recovered

    def run_job(self, job_id):
        record = self._load(str(job_id))
        if record is None or record.get('status') in TERMINAL_STATUSES:
            return self.get(job_id)
        if (
            record.get('status') == 'running'
            and record.get('_worker_id') not in (None, self.worker_id)
            and self._lease_active(record)
        ):
            return self._public_record(record)
        if record.get('_cancel_requested'):
            record.update({'status': 'cancelled', 'finished_at': _now(), 'error': 'job cancelled by user'})
            self._save(record)
            return self.get(job_id)
        self._claim(record)
        cached = self._load_execution_result(record['_execution_key'])
        if cached is not None:
            REDIS_RESULT_CACHE.labels(record['tool'], 'hit').inc()
            return self._finish(job_id, cached['result'])
        REDIS_RESULT_CACHE.labels(record['tool'], 'miss').inc()
        try:
            result = run_tool(record['tool'], record.get('_arguments', {}))
            failed = isinstance(result, dict) and result.get('status') == 'error'
            if not failed:
                result = self._store_execution_result(record['_execution_key'], result)
        except Exception as exc:
            return self._finish(job_id, {'status': 'error', 'error': str(exc)}, failed=True)
        return self._finish(job_id, result, failed=failed)

    def run_forever(self, poll_timeout=5):
        self.recover_stale_jobs()
        while True:
            item = self.redis.brpoplpush(self._queue_key, self._processing_key, timeout=poll_timeout)
            if not item:
                self.recover_stale_jobs()
                continue
            job_id = item.decode('utf-8') if isinstance(item, bytes) else str(item)
            self.run_job(job_id)
            record = self._load(job_id)
            if record is None or record.get('status') in TERMINAL_STATUSES:
                self._ack(job_id)

    def shutdown(self):
        close = getattr(self.redis, 'close', None)
        if close:
            close()
