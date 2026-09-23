import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import time

from scripts.verify_secure_fullstack_e2e import request_json


TERMINAL_STATUSES = {'completed', 'failed', 'cancelled', 'indeterminate'}


def percentile(values, percent):
    ordered = sorted(values)
    position = (len(ordered) - 1) * percent / 100
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def summarize(samples, field):
    values = [sample[field] for sample in samples]
    return {
        'min': round(min(values), 3),
        'p50': round(percentile(values, 50), 3),
        'p95': round(percentile(values, 95), 3),
        'max': round(max(values), 3),
    }


def job_timings(job, client_seconds):
    timestamps = []
    for field in ('created_at', 'started_at', 'finished_at'):
        value = job.get(field)
        if not value:
            raise RuntimeError(f'benchmark job missing {field}')
        parsed = datetime.fromisoformat(value)
        if parsed.utcoffset() is None:
            raise RuntimeError(f'benchmark job has timezone-free {field}')
        timestamps.append(parsed)
    created, started, finished = timestamps
    queue_seconds = (started - created).total_seconds()
    execution_seconds = (finished - started).total_seconds()
    if queue_seconds < 0 or execution_seconds < 0:
        raise RuntimeError('benchmark job timestamps are out of order')
    return {
        'job_id': str(job['job_id']),
        'client_seconds': round(client_seconds, 3),
        'queue_seconds': round(queue_seconds, 3),
        'execution_seconds': round(execution_seconds, 3),
        'server_total_seconds': round((finished - created).total_seconds(), 3),
    }


def benchmark(
    base_url,
    index_path,
    username,
    password,
    samples=10,
    timeout_seconds=120,
    poll_interval=0.5,
    requester=request_json,
    sleep_fn=time.sleep,
    clock=time.monotonic,
):
    if samples < 1 or timeout_seconds <= 0 or poll_interval <= 0:
        raise ValueError('samples, timeout, and poll interval must be positive')
    auth = requester(
        base_url,
        '/api/v1/auth/token',
        method='POST',
        data={'username': username, 'password': password},
        form=True,
    )
    token = str(auth.get('access_token') or '')
    if not token:
        raise RuntimeError('benchmark authentication failed')
    project = requester(
        base_url,
        '/api/v1/projects',
        method='POST',
        data={'name': f'Latency baseline {int(time.time())}'},
        token=token,
    )
    project_id = str((project.get('project') or {}).get('project_id') or '')
    if not project_id:
        raise RuntimeError('benchmark project creation failed')
    recorded = []
    for run_index in range(samples + 1):
        started = clock()
        submitted = requester(
            base_url,
            '/api/v1/jobs',
            method='POST',
            data={
                'tool': 'knowledge_search',
                'arguments': {'query': 'TP53', 'index_path': index_path, 'top_k': 1},
                'project_id': project_id,
            },
            token=token,
        )
        job_id = str((submitted.get('job') or {}).get('job_id') or '')
        if not job_id:
            raise RuntimeError('benchmark job submission failed')
        deadline = clock() + timeout_seconds
        while True:
            job = (requester(
                base_url,
                f'/api/v1/jobs/{job_id}',
                token=token,
            ).get('job') or {})
            if job.get('status') in TERMINAL_STATUSES:
                break
            if clock() >= deadline:
                raise RuntimeError(f'benchmark job {job_id} timed out')
            sleep_fn(poll_interval)
        if job.get('status') != 'completed':
            code = str(job.get('error_code') or 'job_did_not_complete')
            raise RuntimeError(f'benchmark job {job_id} failed with {code}')
        sample = job_timings(job, clock() - started)
        if run_index:
            recorded.append(sample)
    fields = (
        'client_seconds',
        'queue_seconds',
        'execution_seconds',
        'server_total_seconds',
    )
    return {
        'tool': 'knowledge_search',
        'sample_count': samples,
        'warmup_count': 1,
        'sequential': True,
        'samples': recorded,
        'summary_seconds': {field: summarize(recorded, field) for field in fields},
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18080')
    parser.add_argument('--index-path', required=True)
    parser.add_argument('--username', default=os.environ.get('SECURE_E2E_USERNAME', ''))
    parser.add_argument('--password', default=os.environ.get('SECURE_E2E_PASSWORD', ''))
    parser.add_argument('--samples', type=int, default=10)
    parser.add_argument('--timeout-seconds', type=float, default=120)
    parser.add_argument('--poll-interval', type=float, default=0.5)
    parser.add_argument('--output', default='output/performance-baseline.json')
    args = parser.parse_args(argv)
    if not args.username or not args.password:
        parser.error('benchmark credentials are required')
    if args.samples < 1 or args.timeout_seconds <= 0 or args.poll_interval <= 0:
        parser.error('samples, timeout, and poll interval must be positive')
    report = benchmark(
        args.base_url,
        args.index_path,
        args.username,
        args.password,
        samples=args.samples,
        timeout_seconds=args.timeout_seconds,
        poll_interval=args.poll_interval,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
    print(json.dumps(report['summary_seconds'], sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
