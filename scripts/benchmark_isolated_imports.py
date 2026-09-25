import argparse
import json
import os
from statistics import median
import subprocess
import sys
from time import perf_counter

from scripts.benchmark_secure_jobs import percentile


IMPORTS = {
    'lazy': 'import src.job_subprocess; import src.scoped_tool_runtime',
    'eager': (
        'import src.external_service_policy; '
        'import src.job_subprocess; import src.scoped_tool_runtime'
    ),
}


def measure(source, env):
    started = perf_counter()
    result = subprocess.run(
        (sys.executable, '-c', source),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=20,
        check=False,
    )
    elapsed = perf_counter() - started
    if result.returncode:
        raise RuntimeError(f'isolated import failed: {result.stdout[-1000:]}')
    return elapsed


def benchmark(samples):
    env = {**os.environ, 'BIO_AGENT_ISOLATED_TOOL_CHILD': '1'}
    for source in IMPORTS.values():
        measure(source, env)
    timings = {name: [] for name in IMPORTS}
    for index in range(samples):
        order = tuple(IMPORTS) if index % 2 == 0 else tuple(reversed(IMPORTS))
        for name in order:
            timings[name].append(measure(IMPORTS[name], env))
    summary = {
        name: {
            'median_seconds': round(median(values), 4),
            'p95_seconds': round(percentile(values, 95), 4),
            'seconds': [round(value, 4) for value in values],
        }
        for name, values in timings.items()
    }
    summary['comparison'] = {
        'lazy_median_improvement_percent': round(
            100 * (1 - summary['lazy']['median_seconds'] / summary['eager']['median_seconds']), 1
        ),
        'paired_lazy_wins': sum(
            lazy < eager for lazy, eager in zip(timings['lazy'], timings['eager'])
        ),
        'paired_samples': samples,
    }
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=16)
    args = parser.parse_args(argv)
    if args.samples < 2:
        parser.error('samples must be at least two')
    print(json.dumps(benchmark(args.samples), sort_keys=True))


if __name__ == '__main__':
    raise SystemExit(main())
