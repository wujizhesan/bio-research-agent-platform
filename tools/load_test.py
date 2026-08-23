"""Async load test for the research job API."""

import argparse
import asyncio
import json
import math
import os
from pathlib import Path
from time import perf_counter
from uuid import uuid4

import httpx


TERMINAL_STATUSES = {'completed', 'failed', 'cancelled'}


def percentile(values, quantile):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile / 100
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] + (ordered[upper] - ordered[lower]) * weight


def summarize(values):
    if not values:
        return {'count': 0}
    return {
        'count': len(values),
        'min_ms': round(min(values) * 1000, 2),
        'avg_ms': round(sum(values) / len(values) * 1000, 2),
        'p50_ms': round(percentile(values, 50) * 1000, 2),
        'p95_ms': round(percentile(values, 95) * 1000, 2),
        'p99_ms': round(percentile(values, 99) * 1000, 2),
        'max_ms': round(max(values) * 1000, 2),
    }


async def request_with_limit(semaphore, method, url, **kwargs):
    async with semaphore:
        return await method(url, **kwargs)


async def submit_job(client, semaphore, base_url, tool, arguments, index):
    started = perf_counter()
    try:
        response = await request_with_limit(
            semaphore,
            client.post,
            f'{base_url}/api/v1/jobs',
            json={'tool': tool, 'arguments': arguments},
            headers={'Idempotency-Key': f'load-test-{uuid4().hex}-{index}'},
        )
        elapsed = perf_counter() - started
        payload = response.json()
        if response.status_code != 202:
            return {'ok': False, 'latency': elapsed, 'error': payload, 'status_code': response.status_code}
        return {
            'ok': True,
            'latency': elapsed,
            'job_id': payload['job']['job_id'],
            'submitted_at': started,
        }
    except Exception as exc:
        return {'ok': False, 'latency': perf_counter() - started, 'error': str(exc), 'status_code': None}


async def poll_job(client, semaphore, base_url, submitted, poll_interval, deadline):
    started = submitted['submitted_at']
    while perf_counter() < deadline:
        try:
            response = await request_with_limit(
                semaphore,
                client.get,
                f'{base_url}/api/v1/jobs/{submitted["job_id"]}',
            )
            if response.status_code != 200:
                return {'status': 'http_error', 'latency': perf_counter() - started, 'status_code': response.status_code}
            record = response.json().get('job', {})
            if record.get('status') in TERMINAL_STATUSES:
                return {
                    'status': record['status'],
                    'latency': perf_counter() - started,
                    'record': record,
                }
        except Exception as exc:
            return {'status': 'request_error', 'latency': perf_counter() - started, 'error': str(exc)}
        await asyncio.sleep(poll_interval)
    return {'status': 'timeout', 'latency': perf_counter() - started}


async def stream_job(client, semaphore, base_url, submitted, poll_interval, poll_timeout):
    started = submitted['submitted_at']
    try:
        async with semaphore:
            async with client.stream(
                'GET',
                f'{base_url}/api/v1/jobs/{submitted["job_id"]}/events',
                params={'interval_seconds': poll_interval, 'timeout_seconds': poll_timeout},
            ) as response:
                if response.status_code != 200:
                    return {'status': 'http_error', 'latency': perf_counter() - started, 'status_code': response.status_code}
                event_name = None
                data_lines = []
                async for line in response.aiter_lines():
                    if line.startswith('event: '):
                        event_name = line[7:]
                    elif line.startswith('data: '):
                        data_lines.append(line[6:])
                    elif not line and data_lines:
                        payload = json.loads('\n'.join(data_lines))
                        record = payload.get('job', {})
                        if event_name == 'job' and record.get('status') in TERMINAL_STATUSES:
                            return {
                                'status': record['status'],
                                'latency': perf_counter() - started,
                                'record': record,
                            }
                        if event_name == 'timeout':
                            return {'status': 'timeout', 'latency': perf_counter() - started, 'record': record}
                        event_name = None
                        data_lines = []
    except Exception as exc:
        return {'status': 'request_error', 'latency': perf_counter() - started, 'error': str(exc)}
    return {'status': 'timeout', 'latency': perf_counter() - started}


async def run_load_test(args):
    base_url = args.base_url.rstrip('/')
    run_started = perf_counter()
    headers = {}
    token = args.token or os.environ.get('CADD_API_TOKEN')
    if token:
        headers['Authorization'] = f'Bearer {token}'
    limits = httpx.Limits(
        max_connections=args.concurrency,
        max_keepalive_connections=args.concurrency,
    )
    timeout = httpx.Timeout(args.request_timeout)
    semaphore = asyncio.Semaphore(args.concurrency)
    arguments = json.loads(args.arguments)
    async with httpx.AsyncClient(headers=headers, limits=limits, timeout=timeout) as client:
        submissions = await asyncio.gather(*(
            submit_job(client, semaphore, base_url, args.tool, arguments, index)
            for index in range(args.requests)
        ))
        accepted = [item for item in submissions if item['ok']]
        deadline = perf_counter() + args.poll_timeout
        polls = []
        if accepted:
            if args.transport == 'sse':
                polls = await asyncio.gather(*(
                    stream_job(client, semaphore, base_url, item, args.poll_interval, args.poll_timeout)
                    for item in accepted
                ))
            else:
                polls = await asyncio.gather(*(
                    poll_job(client, semaphore, base_url, item, args.poll_interval, deadline)
                    for item in accepted
                ))

    terminal_counts = {}
    for result in polls:
        terminal_counts[result['status']] = terminal_counts.get(result['status'], 0) + 1
    completed = [item for item in polls if item['status'] == 'completed']
    elapsed = perf_counter() - run_started
    report = {
        'target': base_url,
        'tool': args.tool,
        'requested': args.requests,
        'concurrency': args.concurrency,
        'transport': args.transport,
        'accepted': len(accepted),
        'submit_errors': len(submissions) - len(accepted),
        'terminal_counts': terminal_counts,
        'submit_latency': summarize([item['latency'] for item in submissions]),
        'completion_latency': summarize([item['latency'] for item in polls]),
        'elapsed_seconds': round(elapsed, 3),
        'throughput_jobs_per_second': round(len(completed) / max(elapsed, 0.001), 3),
    }
    if args.output:
        Path(args.output).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description='Load test the Bio Research Agent job API')
    parser.add_argument('--base-url', default='http://127.0.0.1:8000')
    parser.add_argument('--tool', default='research_catalog')
    parser.add_argument('--arguments', default='{}')
    parser.add_argument('--requests', type=int, default=20)
    parser.add_argument('--concurrency', type=int, default=5)
    parser.add_argument('--poll-interval', type=float, default=0.2)
    parser.add_argument('--poll-timeout', type=float, default=60)
    parser.add_argument('--request-timeout', type=float, default=10)
    parser.add_argument('--transport', choices=('poll', 'sse'), default='poll')
    parser.add_argument('--token')
    parser.add_argument('--output')
    args = parser.parse_args(argv)
    if args.requests < 1 or args.concurrency < 1:
        parser.error('--requests and --concurrency must be positive')
    report = asyncio.run(run_load_test(args))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
