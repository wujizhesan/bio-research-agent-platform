import argparse
from dataclasses import replace
import json
from pathlib import Path
import random
from statistics import median
import tempfile
from time import perf_counter

from scripts.benchmark_secure_jobs import percentile
from src.job_execution import ExecutionLimits, ProcessToolExecutor
from src.knowledge_plugin import knowledge_ingest_directory, knowledge_search


def summarize(timings):
    result = {
        name: {
            'median_seconds': round(median(values), 6),
            'p95_seconds': round(percentile(values, 95), 6),
            'seconds': [round(value, 6) for value in values],
        }
        for name, values in timings.items()
    }
    result['comparison'] = {
        'precomputed_median_improvement_percent': round(
            100 * (1 - median(timings['precomputed']) / median(timings['legacy'])), 1
        ),
        'paired_precomputed_wins': sum(
            new < old for new, old in zip(timings['precomputed'], timings['legacy'])
        ),
        'paired_samples': len(timings['precomputed']),
    }
    return result


def compare(search, paths, samples, size):
    timings = {name: [] for name in paths}
    by_query = {kind: {name: [] for name in paths} for kind in ('selective', 'shared_terms')}
    for index in range(-1, samples):
        order = tuple(paths) if (index // 2) % 2 == 0 else tuple(reversed(paths))
        number = index % size
        query_kind = 'selective' if index % 2 == 0 else 'shared_terms'
        query = f'Gene{number}' if query_kind == 'selective' else 'TP53 DNA repair'
        expected_matches = None
        for name in order:
            arguments = {'query': query, 'index_path': str(paths[name]), 'top_k': 5}
            started = perf_counter()
            result = search(arguments)
            elapsed = perf_counter() - started
            matches = (result.get('result') or {}).get('matches') or []
            if result.get('status') != 'ok' or not matches or (
                query_kind == 'selective' and (
                    len(matches) != 1 or matches[0]['document_id'] != f'{number:05d}.txt'
                )
            ):
                raise RuntimeError('knowledge index benchmark produced an invalid search result')
            if expected_matches is not None and matches != expected_matches:
                raise RuntimeError('precomputed index changed citation scores or ranking')
            expected_matches = matches
            if index >= 0:
                timings[name].append(elapsed)
                by_query[query_kind][name].append(elapsed)
    return {**summarize(timings), 'by_query': {
        kind: summarize(values) for kind, values in by_query.items()
    }}


def fixture(root, size):
    folder = root / str(size)
    source = folder / 'documents'
    source.mkdir(parents=True)
    rng = random.Random(size)
    vocabulary = (
        'TP53', 'DNA', 'repair', 'RNA', 'design', 'protein', 'expression',
        'pathway', 'sequence', 'evidence', 'tumor', 'single', 'cell', 'analysis',
    )
    for number in range(size):
        text = ' '.join([f'Gene{number}', *rng.choices(vocabulary, k=80)])
        (source / f'{number:05d}.txt').write_text(text, encoding='utf-8')
    paths = {'legacy': folder / 'legacy.json', 'precomputed': folder / 'indexed.json'}
    started = perf_counter()
    knowledge_ingest_directory(source, paths['precomputed'])
    ingestion_seconds = perf_counter() - started
    payload = json.loads(paths['precomputed'].read_text(encoding='utf-8'))
    indexed = 'tfidf_index' in payload
    payload.pop('tfidf_index', None)
    paths['legacy'].write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return paths, {
        'documents': size,
        'precomputed_statistics': indexed,
        'ingestion_seconds': round(ingestion_seconds, 6),
        'index_bytes': {name: path.stat().st_size for name, path in paths.items()},
    }


def benchmark(samples, sizes):
    executor = ProcessToolExecutor(replace(ExecutionLimits.from_env(), timeout_seconds=60))
    rows = []
    try:
        with tempfile.TemporaryDirectory(prefix='knowledge_index_benchmark_') as raw:
            for size in sizes:
                paths, result = fixture(Path(raw), size)
                result['warm_search'] = compare(
                    lambda args: knowledge_search(**args), paths, samples, size,
                )
                result['isolated_execution'] = compare(
                    lambda args: executor.execute('knowledge_search', args), paths, samples, size,
                )
                rows.append(result)
    finally:
        executor.shutdown()
    return {
        'scope': 'warm search and isolated plugin execution; excludes durable queue and API',
        'warmup_pairs_per_path': 1,
        'datasets': rows,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--sizes', default='8,64,512')
    args = parser.parse_args(argv)
    try:
        sizes = tuple(int(item) for item in args.sizes.split(','))
    except ValueError:
        parser.error('sizes must contain positive integers')
    if args.samples < 2 or not sizes or any(size < 1 for size in sizes):
        parser.error('samples must be at least two and sizes must be positive')
    print(json.dumps(benchmark(args.samples, sizes), sort_keys=True))


if __name__ == '__main__':
    raise SystemExit(main())
