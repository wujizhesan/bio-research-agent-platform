import argparse
import hashlib
import json
from pathlib import Path
import platform
from statistics import median
import sys
import tempfile
from time import perf_counter
import tracemalloc
from unittest.mock import patch

from scripts.benchmark_stderr_tail_baseline import BASELINE_SOURCE_COMMIT, legacy_stderr_tail
from src.job_execution import ExecutionLimits, JobExecutionError, ProcessToolExecutor, _read_stderr_tail


CHILD_SOURCE = '''import json
from pathlib import Path
import sys
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
with Path(request['arguments']['fixture']).open('rb') as source:
    for chunk in iter(lambda: source.read(65536), b''):
        sys.stderr.buffer.write(chunk)
raise SystemExit(7)
'''


def fixture(path, size):
    suffix = '\u6700\u7ec8\u8bca\u65ad\uff1a\u5de5\u5177\u5f02\u5e38\u9000\u51fa\U0001f600\r\n'.encode('utf-8')
    block = b'progress line\r\n' * 4096
    remaining = size - len(suffix)
    with path.open('wb') as destination:
        while remaining:
            chunk = block[:remaining]
            destination.write(chunk)
            remaining -= len(chunk)
        destination.write(suffix)
    if path.stat().st_size != size:
        raise RuntimeError('stderr fixture has an incorrect size')


def perform(phase, reader, path, executor):
    if phase == 'diagnostic_read':
        return reader(path)
    with patch('src.job_execution._read_stderr_tail', reader):
        try:
            executor.execute('stderr_tail_benchmark', {'fixture': str(path)})
        except JobExecutionError as exc:
            if exc.error_code != 'execution_failed':
                raise RuntimeError('stderr benchmark changed the execution error code') from exc
            return str(exc)
    raise RuntimeError('failed child unexpectedly produced a successful result')


def checked(phase, reader, path, executor, expected, measure_memory=False):
    if measure_memory:
        tracemalloc.start()
    try:
        started = perf_counter()
        result = perform(phase, reader, path, executor)
        elapsed = perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1] if measure_memory else None
    finally:
        if measure_memory:
            tracemalloc.stop()
    if result != expected:
        raise RuntimeError('stderr strategies produced different diagnostic messages')
    if executor._active_processes:
        raise RuntimeError('stderr benchmark left an active child behind')
    return peak if measure_memory else elapsed


def compare(phase, path, executor, samples):
    readers = {'legacy': legacy_stderr_tail, 'bounded': _read_stderr_tail}
    expected = legacy_stderr_tail(path)
    if phase == 'failed_process':
        expected = f'isolated worker exited with code 7: {expected}'
    peaks = {
        name: checked(phase, reader, path, executor, expected, measure_memory=True)
        for name, reader in readers.items()
    }
    for reader in readers.values():
        checked(phase, reader, path, executor, expected)
    pairs = []
    for index in range(samples):
        order = tuple(readers) if index % 2 == 0 else tuple(reversed(readers))
        pairs.append({name: checked(phase, readers[name], path, executor, expected) for name in order})
    before = median(pair['legacy'] for pair in pairs)
    after = median(pair['bounded'] for pair in pairs)
    size = path.stat().st_size
    return {
        'phase': phase, 'stderr_bytes': size, 'paired_samples': samples,
        'legacy_median_seconds': before, 'bounded_median_seconds': after,
        'elapsed_change_percent': (after / before - 1) * 100,
        'bounded_faster_count': sum(pair['bounded'] < pair['legacy'] for pair in pairs),
        'parent_python_peak_bytes_outside_timing': peaks,
        'diagnostic_bytes_read_bound': {'legacy': size, 'bounded': min(size, 8000)},
        'error_message_characters': len(expected),
        'error_message_sha256': hashlib.sha256(expected.encode('utf-8')).hexdigest(),
        'pairs': pairs,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--workspace-root', type=Path, default=Path('output'))
    arguments = parser.parse_args()
    if arguments.samples < 2:
        parser.error('samples must be at least two')
    arguments.workspace_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='stderr-tail-', dir=arguments.workspace_root.resolve()) as raw:
        root = Path(raw)
        runner = root / 'failed_child.py'
        runner.write_text(CHILD_SOURCE, encoding='utf-8')
        executor = ProcessToolExecutor(
            ExecutionLimits(timeout_seconds=30, memory_limit_mb=0, cpu_time_seconds=0),
            python_executable=sys.executable, runner_path=runner,
        )
        rows = []
        try:
            with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                for size in (4096, 1024 * 1024, 16 * 1024 * 1024):
                    path = root / f'{size}.log'
                    fixture(path, size)
                    for phase in ('diagnostic_read', 'failed_process'):
                        rows.append(compare(phase, path, executor, arguments.samples))
        finally:
            executor.shutdown()
    report = {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': {
            'diagnostic_read': 'complete stderr diagnostic extraction from a real file; excludes log generation, tool execution and subprocess launch',
            'failed_process': 'real ProcessToolExecutor with a real Python child writing the fixture to stderr and exiting with code 7 without a response; includes workspace and request creation, launch, log copying, wait, diagnostic extraction and cleanup; uses a fixed empty tool specification and environment contract; excludes scientific tool execution, API, HTTP sandbox and queue',
        },
        'baseline': 'same process executor with the original stderr tail expression frozen from the source commit',
        'diagnostic_unicode_newlines_error_codes_and_messages_equal': True,
        'fixture': 'ASCII progress lines with CRLF and a Chinese/emoji diagnostic suffix',
        'memory': 'one separate tracemalloc run per strategy and scenario; parent Python allocations only, excludes child/native allocations and operating system file cache',
        'read_bounds': 'derived from the frozen whole-file read and the bounded reader; bounded actual reads independently verified in regression tests',
        'timed_samples_traced': False, 'warmup_pairs_per_scenario': 1,
        'fixture_generation_verification_and_memory_measurement_outside_timing': True,
        'failed_process_workspace_cleanup_in_timing': True,
        'rows': rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
