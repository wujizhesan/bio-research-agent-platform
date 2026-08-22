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
except ImportError:
    from domain_registry import active_tool_specs, run_tool, tool_specs
    from job_manager import TERMINAL_STATUSES


def _now():
    return datetime.now(timezone.utc).isoformat()


class RedisJobManager:
    backend = 'redis'

    def __init__(self, redis_url=None, namespace=None, redis_client=None):
        if redis_client is None:
            try:
                import redis
            except ImportError as exc:
                raise RuntimeError('Redis backend requires the redis package') from exc
            redis_client = redis.Redis.from_url(
                redis_url or os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'),
                decode_responses=True,
            )
        self.redis = redis_client
        self.namespace = namespace or os.environ.get('REDIS_NAMESPACE', 'bioagent')
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

    def _idempotency_key(self, value):
        return f'{self.namespace}:jobs:idempotency:{value}'

    @staticmethod
    def _public_record(record):
        output = dict(record)
        output.pop('_arguments', None)
        output.pop('_cancel_requested', None)
        output.pop('_created_score', None)
        output.pop('idempotency_key', None)
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
        }
        if retry_of:
            record['retry_of'] = retry_of
        if idempotency_key:
            record['idempotency_key'] = idempotency_key
            self.redis.set(self._idempotency_key(idempotency_key), job_id)
        self._save(record)
        self.redis.rpush(self._queue_key, job_id)
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

    def run_job(self, job_id):
        record = self._load(str(job_id))
        if record is None or record.get('status') in TERMINAL_STATUSES:
            return self.get(job_id)
        if record.get('_cancel_requested'):
            record.update({'status': 'cancelled', 'finished_at': _now(), 'error': 'job cancelled by user'})
            self._save(record)
            return self.get(job_id)
        record.update({'status': 'running', 'started_at': _now()})
        self._save(record)
        try:
            result = run_tool(record['tool'], record.get('_arguments', {}))
            failed = isinstance(result, dict) and result.get('status') == 'error'
            update = {
                'status': 'failed' if failed else 'completed',
                'finished_at': _now(),
                'result': result,
            }
            if failed:
                update['error'] = result.get('error', 'tool returned an error')
        except Exception as exc:
            update = {'status': 'failed', 'finished_at': _now(), 'error': str(exc)}
        current = self._load(str(job_id))
        if current is not None:
            if current.get('_cancel_requested'):
                update = {'status': 'cancelled', 'finished_at': _now(), 'error': 'job cancelled by user'}
            current.update(update)
            self._save(current)
        return self.get(job_id)

    def run_forever(self, poll_timeout=5):
        while True:
            item = self.redis.brpop(self._queue_key, timeout=poll_timeout)
            if not item:
                continue
            job_id = item[1] if isinstance(item, (tuple, list)) else item
            self.run_job(job_id)

    def shutdown(self):
        close = getattr(self.redis, 'close', None)
        if close:
            close()
