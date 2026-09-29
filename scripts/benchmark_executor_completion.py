import argparse
from contextlib import ExitStack
from dataclasses import replace
import json
from pathlib import Path
from statistics import median
import tempfile
from threading import Thread
from time import perf_counter, sleep
from unittest.mock import patch
from uuid import uuid4

from scripts.benchmark_secure_jobs import percentile
from src import job_execution, plugin_container
from src.job_execution import ExecutionLimits, ProcessToolExecutor
from src.plugin_container import ContainerToolExecutor
from src.plugin_sandbox_server import SandboxRuntime, create_server


def polling_wait(_futures, timeout):
    sleep(timeout)


def summarize(timings):
    result = {
        name: {
            'median_seconds': round(median(values), 4),
            'p95_seconds': round(percentile(values, 95), 4),
            'seconds': [round(value, 4) for value in values],
        }
        for name, values in timings.items()
    }
    result['comparison'] = {
        'completion_median_improvement_percent': round(
            100 * (1 - median(timings['completion']) / median(timings['polling'])), 1
        ),
        'paired_completion_wins': sum(
            completed < polled
            for completed, polled in zip(timings['completion'], timings['polling'])
        ),
        'paired_samples': len(timings['completion']),
    }
    return result


def measure(executor, arguments, strategy):
    with ExitStack() as stack:
        if strategy == 'polling':
            for module in (job_execution, plugin_container):
                stack.enter_context(patch.object(module, 'wait', polling_wait))
        started = perf_counter()
        result = executor.execute('knowledge_search', arguments)
        elapsed = perf_counter() - started
    matches = (result.get('result') or {}).get('matches') or []
    if result.get('status') != 'ok' or not matches or matches[0]['document_id'] != 'tp53':
        raise RuntimeError('completion benchmark produced an invalid knowledge search result')
    return elapsed, matches


def compare(executor, arguments, samples):
    timings = {'polling': [], 'completion': []}
    expected_matches = None
    for index in range(-1, samples):
        order = tuple(timings) if index % 2 == 0 else tuple(reversed(timings))
        for name in order:
            elapsed, matches = measure(executor, arguments, name)
            if expected_matches is None:
                expected_matches = matches
            elif matches != expected_matches:
                raise RuntimeError('completion strategies returned different search results')
            if index >= 0:
                timings[name].append(elapsed)
    return summarize(timings)


def benchmark(samples, poll_interval):
    limits = replace(
        ExecutionLimits.from_env(),
        timeout_seconds=30,
        poll_interval_seconds=poll_interval,
    )
    with tempfile.TemporaryDirectory(prefix='completion_benchmark_') as raw:
        root = Path(raw)
        index_path = root / 'knowledge-index.json'
        index_path.write_text(json.dumps({
            'version': 1,
            'documents': [
                {'id': 'tp53', 'title': 'TP53', 'text': 'TP53 DNA damage repair'},
                {'id': 'egfr', 'title': 'EGFR', 'text': 'EGFR growth signaling'},
            ],
        }), encoding='utf-8')
        arguments = {'query': 'TP53', 'index_path': str(index_path), 'top_k': 1}
        process = ProcessToolExecutor(limits)
        try:
            process_result = compare(process, arguments, samples)
        finally:
            process.shutdown()
        workspace_root = root / 'exchange'
        runtime = SandboxRuntime(
            executor_factory=lambda: ProcessToolExecutor(limits),
            max_concurrency=1,
            workspace_root=workspace_root,
            pool='light',
        )
        token = uuid4().hex + uuid4().hex
        server = create_server('127.0.0.1', 0, token, runtime=runtime)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        container = ContainerToolExecutor(
            f'http://127.0.0.1:{server.server_port}', token,
            limits=limits, max_concurrency=1,
            input_workspace_root=workspace_root, input_roots=(root,),
        )
        try:
            container_result = compare(container, arguments, samples)
        finally:
            container.shutdown()
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    return {
        'tool': 'knowledge_search',
        'poll_interval_seconds': poll_interval,
        'polling_baseline': 'same executor code with completion waits replaced by fixed sleeps',
        'scope': 'isolated tool and HTTP sandbox round trip; excludes durable queue and API',
        'warmup_pairs_per_path': 1,
        'process': process_result,
        'container_round_trip': container_result,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=16)
    parser.add_argument('--poll-interval', type=float, default=0.1)
    args = parser.parse_args(argv)
    if args.samples < 2 or args.poll_interval <= 0:
        parser.error('samples must be at least two and poll interval must be positive')
    print(json.dumps(benchmark(args.samples, args.poll_interval), sort_keys=True))


if __name__ == '__main__':
    raise SystemExit(main())
