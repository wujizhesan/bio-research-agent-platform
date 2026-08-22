"""Asynchronous execution and durable state for registry tools."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
from threading import Lock
from uuid import uuid4

try:
    from .domain_registry import run_tool, active_tool_specs, tool_specs
except ImportError:
    from domain_registry import run_tool, active_tool_specs, tool_specs


TERMINAL_STATUSES = frozenset({'completed', 'failed', 'cancelled'})


def _now():
    return datetime.now(timezone.utc).isoformat()


class JobManager:
    def __init__(self, max_workers=2, store_path=None):
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix='bio-agent-job')
        self._lock = Lock()
        self._jobs = {}
        self._futures = {}
        self._store_path = Path(store_path) if store_path else None
        if self._store_path:
            self._store_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_store()
            self._load_store()

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(str(self._store_path), timeout=30)
        try:
            yield connection
        except Exception:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _init_store(self):
        with self._connection() as connection:
            connection.execute(
                'CREATE TABLE IF NOT EXISTS jobs ('
                'job_id TEXT PRIMARY KEY, tool TEXT NOT NULL, status TEXT NOT NULL, '
                'created_at TEXT NOT NULL, started_at TEXT, finished_at TEXT, '
                'arguments_json TEXT, result_json TEXT, error TEXT, retry_of TEXT, '
                'idempotency_key TEXT, cancel_requested INTEGER DEFAULT 0)'
            )
            columns = {row[1] for row in connection.execute('PRAGMA table_info(jobs)').fetchall()}
            if 'arguments_json' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN arguments_json TEXT')
            if 'retry_of' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN retry_of TEXT')
            if 'idempotency_key' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN idempotency_key TEXT')
            if 'cancel_requested' not in columns:
                connection.execute('ALTER TABLE jobs ADD COLUMN cancel_requested INTEGER DEFAULT 0')

    def _persist(self, record):
        if not self._store_path:
            return
        with self._connection() as connection:
            connection.execute(
                'INSERT OR REPLACE INTO jobs '
                '(job_id, tool, status, created_at, started_at, finished_at, '
                'arguments_json, result_json, error, retry_of, idempotency_key, cancel_requested) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ',
                (
                    record['job_id'],
                    record['tool'],
                    record['status'],
                    record['created_at'],
                    record.get('started_at'),
                    record.get('finished_at'),
                    json.dumps(record.get('_arguments', {}), ensure_ascii=False, default=str),
                    json.dumps(record.get('result'), ensure_ascii=False, default=str)
                    if 'result' in record else None,
                    record.get('error'),
                    record.get('retry_of'),
                    record.get('idempotency_key'),
                    int(bool(record.get('_cancel_requested'))),
                ),
            )

    def _load_store(self):
        with self._connection() as connection:
            rows = connection.execute(
                'SELECT job_id, tool, status, created_at, started_at, finished_at, '
                'arguments_json, result_json, error, retry_of, idempotency_key, cancel_requested FROM jobs'
            ).fetchall()
        interrupted_at = _now()
        resumable = []
        for row in rows:
            job_id, tool, status, created_at, started_at, finished_at, arguments_json, result_json, error, retry_of, idempotency_key, cancel_requested = row
            record = {
                'job_id': job_id,
                'tool': tool,
                'status': status,
                'created_at': created_at,
            }
            if arguments_json:
                try:
                    record['_arguments'] = json.loads(arguments_json)
                except json.JSONDecodeError:
                    record['_arguments'] = {}
            if started_at:
                record['started_at'] = started_at
            if finished_at:
                record['finished_at'] = finished_at
            if result_json:
                record['result'] = json.loads(result_json)
            if error:
                record['error'] = error
            if retry_of:
                record['retry_of'] = retry_of
            if idempotency_key:
                record['idempotency_key'] = idempotency_key
            record['_cancel_requested'] = bool(cancel_requested)
            if status == 'running':
                record.update({
                    'status': 'failed',
                    'finished_at': interrupted_at,
                    'error': 'job interrupted by process restart',
                })
            elif status == 'queued':
                if record.get('_cancel_requested'):
                    record.update({
                        'status': 'cancelled',
                        'finished_at': interrupted_at,
                        'error': 'job cancelled by user',
                    })
                elif record.get('_arguments') is None:
                    record.update({
                        'status': 'failed',
                        'finished_at': interrupted_at,
                        'error': 'queued job arguments are unavailable',
                    })
                else:
                    resumable.append((job_id, tool, dict(record['_arguments'])))
            self._jobs[job_id] = record
        for record in self._jobs.values():
            self._persist(record)
        for job_id, tool, arguments in resumable:
            self._futures[job_id] = self._executor.submit(self._run, job_id, tool, arguments)

    def _public_record(self, record):
        output = dict(record)
        output.pop('_arguments', None)
        output.pop('_cancel_requested', None)
        output.pop('idempotency_key', None)
        if record.get('_cancel_requested'):
            output['cancel_requested'] = True
        return output

    def _create_job_locked(self, tool, arguments, retry_of=None, idempotency_key=None):
        job_id = uuid4().hex
        record = {
            'job_id': job_id,
            'tool': tool,
            'status': 'queued',
            'created_at': _now(),
            '_arguments': dict(arguments),
            '_cancel_requested': False,
        }
        if idempotency_key:
            record['idempotency_key'] = idempotency_key
        if retry_of:
            record['retry_of'] = retry_of
        self._jobs[job_id] = record
        self._persist(record)
        self._futures[job_id] = self._executor.submit(self._run, job_id, tool, dict(arguments))
        return self._public_record(record)

    def _validate_tool_state(self, tool):
        known = {spec['name'] for spec in tool_specs()}
        if tool not in known:
            raise ValueError(f'unknown tool: {tool}')
        if tool not in {spec['name'] for spec in active_tool_specs()}:
            raise ValueError(f'plugin domain is disabled for tool: {tool}')
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
        with self._lock:
            if idempotency_key:
                for existing in self._jobs.values():
                    if existing.get('idempotency_key') != idempotency_key:
                        continue
                    if existing.get('tool') != tool or existing.get('_arguments') != arguments:
                        raise ValueError('idempotency key already used with different job payload')
                    output = self._public_record(existing)
                    output['deduplicated'] = True
                    return output
            return self._create_job_locked(tool, arguments, idempotency_key=idempotency_key)

    def retry(self, job_id):
        with self._lock:
            original = self._jobs.get(str(job_id))
            if original is None:
                raise ValueError(f'job not found: {job_id}')
            if original.get('status') not in TERMINAL_STATUSES:
                raise ValueError('only completed, failed or cancelled jobs can be retried')
            arguments = original.get('_arguments')
            if arguments is None:
                raise ValueError('job arguments are unavailable')
            self._validate_tool_state(original['tool'])
            return self._create_job_locked(original['tool'], arguments, retry_of=original['job_id'])

    def _run(self, job_id, tool, arguments):
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                return
            if record.get('_cancel_requested'):
                record.update({
                    'status': 'cancelled',
                    'finished_at': _now(),
                    'error': 'job cancelled by user',
                })
                self._persist(record)
                self._futures.pop(job_id, None)
                return
            record.update({'status': 'running', 'started_at': _now()})
            self._persist(record)
        try:
            result = run_tool(tool, arguments)
            failed = isinstance(result, dict) and result.get('status') == 'error'
            update = {
                'status': 'failed' if failed else 'completed',
                'finished_at': _now(),
                'result': result,
            }
            if failed:
                update['error'] = result.get('error', 'tool returned an error')
        except Exception as exc:
            update = {
                'status': 'failed',
                'finished_at': _now(),
                'error': str(exc),
            }
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                if record.get('_cancel_requested'):
                    update = {
                        'status': 'cancelled',
                        'finished_at': _now(),
                        'error': 'job cancelled by user',
                    }
                record.update(update)
                self._persist(record)
            self._futures.pop(job_id, None)

    def cancel(self, job_id):
        with self._lock:
            record = self._jobs.get(str(job_id))
            if record is None:
                raise ValueError(f'job not found: {job_id}')
            if record.get('status') in TERMINAL_STATUSES:
                return self._public_record(record)
            record['_cancel_requested'] = True
            future = self._futures.get(str(job_id))
            if record.get('status') == 'queued' and future is not None and future.cancel():
                record.update({
                    'status': 'cancelled',
                    'finished_at': _now(),
                    'error': 'job cancelled by user',
                })
                self._futures.pop(str(job_id), None)
            self._persist(record)
            return self._public_record(record)

    def get(self, job_id):
        with self._lock:
            record = self._jobs.get(str(job_id))
            return self._public_record(record) if record else None

    def list(self, limit=20):
        try:
            size = min(max(int(limit), 1), 100)
        except (TypeError, ValueError):
            size = 20
        with self._lock:
            records = [self._public_record(record) for record in self._jobs.values()]
        return sorted(records, key=lambda item: item['created_at'], reverse=True)[:size]

    def shutdown(self):
        self._executor.shutdown(wait=False, cancel_futures=True)
