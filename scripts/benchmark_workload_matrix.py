import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from scripts.benchmark_secure_jobs import (
    TERMINAL_STATUSES,
    job_timings,
    summarize,
)
from scripts.verify_secure_fullstack_e2e import request_json


PROFILE_NAMES = (
    'knowledge_search',
    'literature_summarize',
    'omics_inspect_toolchain',
)
TIMING_FIELDS = (
    'client_seconds',
    'observation_lag_seconds',
    'queue_seconds',
    'execution_seconds',
    'server_total_seconds',
    'submission_seconds',
    'outbox_wait_seconds',
    'dispatch_seconds',
    'worker_wait_seconds',
)


def workload_profiles(index_path):
    return {
        'knowledge_search': {
            'tool': 'knowledge_search',
            'arguments': {
                'query': 'TP53',
                'index_path': str(index_path),
                'top_k': 1,
            },
        },
        'literature_summarize': {
            'tool': 'literature_summarize',
            'arguments': {
                'evidence': {
                    'matches': [{
                        'gene_id': 'TP53',
                        'source': 'benchmark-fixture',
                        'title': 'Deterministic evidence',
                    }],
                },
            },
        },
        'omics_inspect_toolchain': {
            'tool': 'omics_inspect_toolchain',
            'arguments': {},
        },
    }


def open_sse(url, timeout):
    return urlopen(
        Request(url, headers={'Accept': 'text/event-stream'}),
        timeout=timeout,
    )


def wait_for_sse_job(
    base_url,
    job_id,
    token,
    timeout_seconds,
    requester=request_json,
    opener=open_sse,
    clock=time.monotonic,
    sleep_fn=time.sleep,
):
    deadline = clock() + timeout_seconds
    cursor = None
    reconnects = 0
    while clock() < deadline:
        ticket = requester(
            base_url,
            f'/api/v1/jobs/{job_id}/events/ticket',
            method='POST',
            token=token,
        ).get('ticket')
        if not ticket:
            raise RuntimeError('benchmark SSE ticket missing')
        remaining = max(deadline - clock(), 1)
        query = {
            'ticket': ticket,
            'interval_seconds': '0.15',
            'timeout_seconds': str(min(int(remaining), 300)),
        }
        if cursor:
            query['last_event_id'] = cursor
        url = (
            f'{base_url.rstrip("/")}/api/v1/jobs/{job_id}/events'
            f'?{urlencode(query)}'
        )
        try:
            with opener(url, min(remaining + 2, timeout_seconds + 2)) as response:
                event_name = None
                data = []
                for raw in response:
                    if clock() >= deadline:
                        raise TimeoutError('benchmark SSE job timed out')
                    line = raw.decode('utf-8').rstrip('\r\n')
                    if line.startswith('id:'):
                        cursor = line[3:].strip()
                    elif line.startswith('event:'):
                        event_name = line[6:].strip()
                    elif line.startswith('data:'):
                        value = line[5:]
                        data.append(value[1:] if value.startswith(' ') else value)
                    elif not line:
                        if event_name == 'job' and data:
                            payload = json.loads('\n'.join(data))
                            job = payload.get('job') or {}
                            if job.get('status') in TERMINAL_STATUSES:
                                return job, reconnects
                        elif event_name in {'error', 'access_revoked'}:
                            raise RuntimeError(f'benchmark SSE {event_name}')
                        event_name = None
                        data = []
        except HTTPError as exc:
            raise RuntimeError(f'benchmark SSE HTTP {exc.code}') from exc
        except OSError:
            if clock() >= deadline:
                break
        reconnects += 1
        sleep_fn(min(0.1, max(deadline - clock(), 0)))
    raise TimeoutError('benchmark SSE job timed out')


def execute_case(
    base_url,
    project_id,
    token,
    profile,
    timeout_seconds,
    requester=request_json,
    observer=wait_for_sse_job,
    clock=time.monotonic,
):
    started = clock()
    job_id = None
    tool = profile['tool']
    try:
        submitted = requester(
            base_url,
            '/api/v1/jobs',
            method='POST',
            data={
                'tool': tool,
                'arguments': profile['arguments'],
                'project_id': project_id,
            },
            token=token,
        )
        job_id = str((submitted.get('job') or {}).get('job_id') or '')
        if not job_id:
            raise RuntimeError('benchmark job submission missing job_id')
        observed, reconnects = observer(
            base_url,
            job_id,
            token,
            timeout_seconds,
            requester=requester,
        )
        client_seconds = clock() - started
        job = (requester(
            base_url,
            f'/api/v1/jobs/{job_id}',
            token=token,
        ).get('job') or observed)
        if job.get('status') not in TERMINAL_STATUSES:
            raise RuntimeError('benchmark SSE terminal state was not durable')
        sample = job_timings(job, client_seconds)
        sample.update({
            'tool': tool,
            'status': job['status'],
            'sse_reconnects': reconnects,
            'observation_lag_seconds': round(max(
                client_seconds - sample['server_total_seconds'], 0
            ), 3),
        })
        if job['status'] != 'completed':
            sample['error_code'] = str(job.get('error_code') or 'job_failed')
        return sample
    except Exception as exc:
        return {
            'job_id': job_id,
            'tool': tool,
            'status': 'observation_error',
            'error_type': type(exc).__name__,
        }


def run_scenario(name, profiles, concurrency, sample_count, runner, clock=time.monotonic):
    selected = (
        tuple(profiles.values())
        if name == 'mixed' else (profiles[name],)
    )
    started = clock()
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        futures = [
            executor.submit(runner, selected[index % len(selected)])
            for index in range(sample_count)
        ]
        samples = [future.result() for future in futures]
    duration = max(clock() - started, 0.001)
    successful = [sample for sample in samples if sample['status'] == 'completed']
    failed = [sample for sample in samples if sample['status'] != 'completed']
    return {
        'name': name,
        'concurrency': concurrency,
        'sample_count': sample_count,
        'completed_count': len(successful),
        'failed_count': len(failed),
        'failure_rate': round(len(failed) / sample_count, 3),
        'wall_seconds': round(duration, 3),
        'throughput_jobs_per_second': round(len(successful) / duration, 3),
        'summary_seconds': {
            field: summarize(successful, field) if successful else None
            for field in TIMING_FIELDS
        },
        'samples': samples,
    }


def benchmark_matrix(
    base_url,
    index_path,
    username,
    password,
    sample_count=8,
    concurrency_levels=(1, 2, 8),
    timeout_seconds=120,
    requester=request_json,
    observer=wait_for_sse_job,
    clock=time.monotonic,
):
    if sample_count < 1 or not concurrency_levels or any(
        level < 1 for level in concurrency_levels
    ) or timeout_seconds <= 0:
        raise ValueError('benchmark sample count, concurrency, and timeout must be positive')
    token = str(requester(
        base_url,
        '/api/v1/auth/token',
        method='POST',
        data={'username': username, 'password': password},
        form=True,
    ).get('access_token') or '')
    if not token:
        raise RuntimeError('benchmark authentication failed')
    project_id = str((requester(
        base_url,
        '/api/v1/projects',
        method='POST',
        data={'name': f'Workload matrix {int(time.time())}'},
        token=token,
    ).get('project') or {}).get('project_id') or '')
    if not project_id:
        raise RuntimeError('benchmark project creation failed')
    profiles = workload_profiles(index_path)

    def runner(profile):
        return execute_case(
            base_url,
            project_id,
            token,
            profile,
            timeout_seconds,
            requester=requester,
            observer=observer,
            clock=clock,
        )

    warmups = {name: runner(profile) for name, profile in profiles.items()}
    for name, sample in warmups.items():
        if sample['status'] != 'completed':
            raise RuntimeError(
                f'benchmark warmup {name} failed with '
                f'{sample.get("error_code") or sample.get("error_type") or sample["status"]}'
            )
    scenarios = [
        run_scenario(name, profiles, concurrency, sample_count, runner, clock)
        for name in (*PROFILE_NAMES, 'mixed')
        for concurrency in concurrency_levels
    ]
    return {
        'version': 1,
        'observation_mode': 'sse',
        'sample_count_per_scenario': sample_count,
        'concurrency_levels': list(concurrency_levels),
        'warmup_count': len(warmups),
        'scenarios': scenarios,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18080')
    parser.add_argument('--index-path', required=True)
    parser.add_argument('--username', default=os.environ.get('SECURE_E2E_USERNAME', ''))
    parser.add_argument('--password', default=os.environ.get('SECURE_E2E_PASSWORD', ''))
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--concurrency', default='1,2,8')
    parser.add_argument('--timeout-seconds', type=float, default=120)
    parser.add_argument('--output', default='output/performance-matrix.json')
    args = parser.parse_args(argv)
    try:
        levels = tuple(int(value) for value in args.concurrency.split(','))
    except ValueError as exc:
        parser.error(f'invalid concurrency levels: {exc}')
    if not args.username or not args.password:
        parser.error('benchmark credentials are required')
    report = benchmark_matrix(
        args.base_url,
        args.index_path,
        args.username,
        args.password,
        sample_count=args.samples,
        concurrency_levels=levels,
        timeout_seconds=args.timeout_seconds,
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
    print(json.dumps([
        {
            'name': item['name'],
            'concurrency': item['concurrency'],
            'failure_rate': item['failure_rate'],
            'throughput_jobs_per_second': item['throughput_jobs_per_second'],
            'server_p95_seconds': (
                item['summary_seconds']['server_total_seconds'] or {}
            ).get('p95'),
        }
        for item in report['scenarios']
    ], sort_keys=True))
    return int(any(item['failed_count'] for item in report['scenarios']))


if __name__ == '__main__':
    raise SystemExit(main())
