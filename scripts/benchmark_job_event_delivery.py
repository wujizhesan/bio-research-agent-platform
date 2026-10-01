import argparse
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from statistics import median
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI
from sqlalchemy import text

from src.api_job_routes import _register_job_event_routes
from src.auth import Principal
from src.database import Database, set_database_principal
from src.job_event_notifications import JobEventListener


class EarlyRedisReader:
    def __init__(self, record):
        self.record = record

    def read_job_events(self, *_args):
        return [('2000-0', {**self.record, 'revision': 2})]


async def measure(writer, reader, listener, project_id, delay_seconds, notify):
    record = {
        'job_id': uuid4().hex,
        'project_id': project_id,
        'tool': 'research_catalog',
        'status': 'running',
        'created_at': datetime.now(timezone.utc).isoformat(),
        '_revision': 1,
        '_event_id': '1000-0',
    }
    await writer.upsert_job(record)
    app = FastAPI()
    app.state.job_backend = 'redis'
    app.state.job_event_listener = listener if notify else None
    principal = Principal('event-benchmark', ('researcher',), 'jwt')

    async def permitted(*_args):
        return principal

    _register_job_event_routes(
        app, jobs=EarlyRedisReader(record), database=reader,
        output_root=Path('.'), audit=None, require_permission=lambda _scope: permitted,
        job_access=permitted, issue_stream_ticket=None, stream_ticket_ttl=60,
        stream_principal=permitted, read_job=reader.get_job,
        allow_legacy_artifact_paths=False,
    )
    endpoint = next(
        route.endpoint for route in app.routes
        if getattr(route, 'path', '') == '/api/v1/jobs/{job_id}/events'
    )
    response = await endpoint(
        record['job_id'], interval_seconds=0.2, timeout_seconds=5,
        last_event_id=None, query_last_event_id=None, principal=principal,
    )
    waiting = asyncio.Event()
    wait_started = None

    async def consume():
        nonlocal wait_started
        try:
            async for chunk in response.body_iterator:
                if chunk.startswith(': keep-alive') and not waiting.is_set():
                    wait_started = perf_counter()
                    waiting.set()
                data = next((
                    line[6:] for line in chunk.splitlines() if line.startswith('data: ')
                ), None)
                if data is not None and json.loads(data).get('job', {}).get('status') == 'completed':
                    if 'id: r-2\n' not in chunk or wait_started is None:
                        raise AssertionError('expected the committed terminal revision')
                    return perf_counter() - wait_started
            raise AssertionError('SSE closed before committed completion')
        finally:
            await response.body_iterator.aclose()

    consumer = asyncio.create_task(consume())
    try:
        await asyncio.wait_for(waiting.wait(), 2)
        await asyncio.sleep(delay_seconds)
        await writer.upsert_job({
            **record, 'status': 'completed', '_revision': 2, '_event_id': '2000-0',
            'finished_at': datetime.now(timezone.utc).isoformat(), 'result': {'status': 'ok'},
        })
        return await asyncio.wait_for(consumer, 3)
    finally:
        if not consumer.done():
            consumer.cancel()
        with suppress(asyncio.CancelledError, Exception):
            await consumer
        async with writer.engine.begin() as cleanup:
            await cleanup.execute(
                text('DELETE FROM job_records WHERE job_id = :job_id'),
                {'job_id': record['job_id']},
            )


def summarize(values):
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return {
        'median_seconds': round(median(values), 6),
        'p95_seconds': round(
            ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower), 6
        ),
        'seconds': [round(value, 6) for value in values],
    }


async def benchmark(samples, delays_ms):
    writer = Database(os.environ['DATABASE_URL'])
    reader_url = os.environ.get('API_DATABASE_URL') or os.environ['DATABASE_URL']
    reader = Database(reader_url)
    listener = JobEventListener(reader_url)
    project_id = f'event-benchmark-{uuid4().hex}'
    listener.start()
    try:
        await asyncio.wait_for(listener.ready.wait(), 5)
        await writer.create_project(
            project_id, 'Event delivery benchmark', None, 'event-benchmark',
            datetime.now(timezone.utc).isoformat(),
        )
        set_database_principal(Principal('event-benchmark', ('researcher',), 'jwt'))
        datasets = []
        for delay_ms in delays_ms:
            values = {'polling': [], 'notification': []}
            for index in range(-1, samples):
                order = ('polling', 'notification') if index % 2 else ('notification', 'polling')
                for strategy in order:
                    elapsed = await measure(
                        writer, reader, listener, project_id, delay_ms / 1000,
                        strategy == 'notification',
                    )
                    if index >= 0:
                        values[strategy].append(elapsed)
            polling = median(values['polling'])
            notification = median(values['notification'])
            datasets.append({
                'injected_commit_delay_ms': delay_ms,
                **{name: summarize(samples_) for name, samples_ in values.items()},
                'comparison': {
                    'paired_samples': samples,
                    'notification_wins': sum(
                        new < old for old, new in zip(values['polling'], values['notification'])
                    ),
                    'median_improvement_percent': round(100 * (1 - notification / polling), 1),
                },
            })
        if listener._subscribers:
            raise AssertionError('SSE subscriptions were not released')
        return {
            'scope': 'production SSE body iterator and real PostgreSQL commit notifications; '
            'measures early state wake to delivery of the committed terminal revision, with '
            'injected commit delay; excludes HTTP transport, authentication, queue and tool execution',
            'warmup_pairs_per_delay': 1,
            'datasets': datasets,
        }
    finally:
        set_database_principal()
        await listener.close()
        await reader.close()
        async with writer.engine.begin() as cleanup:
            await cleanup.execute(
                text('DELETE FROM projects WHERE project_id = :project_id'),
                {'project_id': project_id},
            )
        await writer.close()


def main():
    parser = argparse.ArgumentParser(description='Compare durable SSE event delivery strategies')
    parser.add_argument('--samples', type=int, default=16)
    parser.add_argument('--commit-delays-ms', default='20,80')
    args = parser.parse_args()
    delays = [float(value) for value in args.commit_delays_ms.split(',')]
    if args.samples < 1 or not delays or any(delay <= 0 for delay in delays):
        parser.error('samples and commit delays must be positive')
    print(json.dumps(asyncio.run(benchmark(args.samples, delays)), sort_keys=True))


if __name__ == '__main__':
    main()
