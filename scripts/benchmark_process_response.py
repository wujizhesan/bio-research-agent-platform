import argparse
import json
import os
from pathlib import Path
import platform
from statistics import median
import sys
import tempfile
from time import perf_counter
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch

from scripts.benchmark_process_response_baseline import BASELINE_SOURCE_COMMIT, legacy_read_process_response
from src.job_execution import ExecutionLimits, JobExecutionError, ProcessToolExecutor, _read_process_response


CHILD_SOURCE = '''import json
from pathlib import Path
import sys
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
with Path(request['arguments']['fixture']).open('rb') as source, Path(sys.argv[2]).open('wb') as target:
    for chunk in iter(lambda: source.read(65536), b''):
        target.write(chunk)
'''


def legacy(path, quota):
    return legacy_read_process_response(SimpleNamespace(limits=SimpleNamespace(max_result_bytes=quota)), path)


def fixture(path, size, unicode=False):
    prefix = b'{"ok":true,"result":{"blob":"'
    suffix = ('\u7814\u7a76\U0001f9ec' if unicode else '').encode('utf-8') + b'"}}'
    encoded = prefix + b'x' * (size - len(prefix) - len(suffix)) + suffix
    path.write_bytes(encoded)
    if path.stat().st_size != size:
        raise RuntimeError('process response fixture has an incorrect size')
    return json.loads(encoded.decode('utf-8'))


def perform(phase, reader, path, executor, quota):
    if phase == 'response_read':
        return reader(path, quota)
    with patch('src.job_execution._read_process_response', reader):
        return executor.execute('response_benchmark', {'fixture': str(path)})


def checked(phase, reader, path, executor, quota, expected, memory=False):
    if memory:
        tracemalloc.start()
    try:
        started = perf_counter()
        result = perform(phase, reader, path, executor, quota)
        elapsed = perf_counter() - started
        peak = tracemalloc.get_traced_memory()[1] if memory else None
    finally:
        if memory:
            tracemalloc.stop()
    if result != expected or executor._active_processes:
        raise RuntimeError('response benchmark changed the result or left an active process')
    return peak if memory else elapsed


def compare(phase, path, payload, unicode, executor, samples):
    quota = executor.limits.max_result_bytes
    expected = payload if phase == 'response_read' else payload['result']
    readers = {'legacy': legacy, 'bounded': _read_process_response}
    peaks = {
        name: checked(phase, reader, path, executor, quota, expected, memory=True)
        for name, reader in readers.items()
    }
    for reader in readers.values():
        checked(phase, reader, path, executor, quota, expected)
    pairs = []
    for index in range(samples):
        order = tuple(readers) if index % 2 == 0 else tuple(reversed(readers))
        pairs.append({name: checked(phase, readers[name], path, executor, quota, expected) for name in order})
    before = median(pair['legacy'] for pair in pairs)
    after = median(pair['bounded'] for pair in pairs)
    return {
        'phase': phase, 'response_bytes': path.stat().st_size, 'unicode': unicode,
        'quota_bytes': quota, 'paired_samples': samples,
        'legacy_median_seconds': before, 'bounded_median_seconds': after,
        'elapsed_change_percent': (after / before - 1) * 100,
        'bounded_faster_count': sum(pair['bounded'] < pair['legacy'] for pair in pairs),
        'parent_python_peak_bytes_outside_timing': peaks, 'pairs': pairs,
    }


class CountedReader:
    def __init__(self, source):
        self.source = source
        self.actual_bytes = 0

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.source.__exit__(*args)

    def fileno(self):
        return self.source.fileno()

    def read(self, size):
        result = self.source.read(size)
        self.actual_bytes += len(result)
        return result


def growth_probe(root):
    path = root / 'growth.json'
    grown = b'{"ok":true,"result":{"blob":"' + b'x' * (4 * 1024 * 1024) + b'"}}'
    quota = 1024
    rows = {}
    for name, reader in (('legacy', legacy), ('bounded', _read_process_response)):
        path.write_bytes(b'{"ok":true,"result":{"blob":"small"}}')
        original_stat = Path.stat
        original_fstat = os.fstat
        original_open = Path.open
        holders = []
        changed = False

        def grow_path(selected, *args, **kwargs):
            nonlocal changed
            info = original_stat(selected, *args, **kwargs)
            if selected == path and not changed:
                changed = True
                path.write_bytes(grown)
            return info

        def grow_handle(descriptor):
            nonlocal changed
            info = original_fstat(descriptor)
            if holders and descriptor == holders[0].fileno() and not changed:
                changed = True
                path.write_bytes(grown)
            return info

        def counted_open(selected, *args, **kwargs):
            source = original_open(selected, *args, **kwargs)
            if selected == path and args and args[0] == 'rb':
                wrapper = CountedReader(source)
                holders.append(wrapper)
                return wrapper
            return source

        with patch.object(Path, 'stat', grow_path) if name == 'legacy' else patch('src.job_execution.os.fstat', side_effect=grow_handle):
            with patch.object(Path, 'open', counted_open):
                try:
                    payload = reader(path, quota)
                except JobExecutionError as exc:
                    rows[name] = {'accepted': False, 'error_code': exc.error_code, 'error': str(exc), 'actual_bytes_read': holders[0].actual_bytes}
                else:
                    rows[name] = {'accepted': True, 'returned_blob_bytes': len(payload['result']['blob'])}
        if not changed:
            raise RuntimeError('response growth probe did not modify the file at the size check')
    if not rows['legacy']['accepted'] or rows['legacy']['returned_blob_bytes'] != 4 * 1024 * 1024:
        raise RuntimeError('frozen response reader did not reproduce the quota bypass')
    if rows['bounded']['accepted'] or rows['bounded']['actual_bytes_read'] != quota + 1:
        raise RuntimeError('bounded response reader did not enforce the actual read limit')
    return {'quota_bytes': quota, 'grown_response_bytes': len(grown), 'strategies': rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--workspace-root', type=Path, default=Path('output'))
    arguments = parser.parse_args()
    if arguments.samples < 2:
        parser.error('samples must be at least two')
    arguments.workspace_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='process-response-', dir=arguments.workspace_root.resolve()) as raw:
        root = Path(raw)
        runner = root / 'response_child.py'
        runner.write_text(CHILD_SOURCE, encoding='utf-8')
        executor = ProcessToolExecutor(
            ExecutionLimits(timeout_seconds=30, memory_limit_mb=0, cpu_time_seconds=0, max_result_bytes=16 * 1024 * 1024),
            python_executable=sys.executable, runner_path=runner,
        )
        rows = []
        try:
            with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                for size, unicode in ((4096, False), (1024 * 1024, True), (16 * 1024 * 1024, False)):
                    path = root / f'{size}.json'
                    payload = fixture(path, size, unicode)
                    for phase in ('response_read', 'real_process'):
                        rows.append(compare(phase, path, payload, unicode, executor, arguments.samples))
        finally:
            executor.shutdown()
        growth = growth_probe(root)
    report = {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': {
            'response_read': 'complete quota check and JSON response reading from a real file; excludes fixture creation and process launch',
            'real_process': 'real ProcessToolExecutor with a real Python child copying the fixture to response.json; includes workspace/request creation, launch, actual response writing, wait, parsing and cleanup; fixed empty tool specification/environment contract; excludes scientific tool execution, API, HTTP sandbox and queue',
        },
        'valid_result_values_equal': True,
        'memory': 'one separate tracemalloc run per strategy/scenario; parent Python allocations only, excludes child/native allocations and operating system cache',
        'timed_samples_traced': False, 'warmup_pairs_per_scenario': 1,
        'fixture_generation_verification_and_memory_measurement_outside_timing': True,
        'real_process_workspace_cleanup_in_timing': True,
        'growth_probe_outside_timing': growth,
        'rows': rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
