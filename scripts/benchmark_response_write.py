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

from scripts.benchmark_response_write_baseline import BASELINE_SOURCE_COMMIT, legacy_write_process_response
from src.job_execution import ExecutionLimits, JobExecutionError, ProcessToolExecutor
from src.job_subprocess import _write_process_response


CHILD_SOURCE = '''import json
from pathlib import Path
import sys
sys.path.insert(0, REPOSITORY)
from scripts.benchmark_response_write_baseline import legacy_write_process_response
from src.job_subprocess import _write_process_response
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
arguments = request['arguments']
payload = json.loads(Path(arguments['fixture']).read_text(encoding='utf-8'))
memory_path = arguments.get('memory_path')
if memory_path:
    import tracemalloc
    tracemalloc.start()
writer = legacy_write_process_response if arguments['strategy'] == 'legacy' else _write_process_response
code = writer(payload, request, Path(sys.argv[2]), 0)
if memory_path:
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    Path(memory_path).write_text(json.dumps({'writer_python_peak_bytes': peak}), encoding='utf-8')
raise SystemExit(code)
'''


def fixture(path, size, unicode):
    tail = '\u7814\u7a76\U0001f9ec' if unicode else ''
    payload = {'ok': True, 'result': {'blob': tail}, 'telemetry': {'domain': 'research', 'status': 'success', 'duration_seconds': 0.25}}
    overhead = len(json.dumps(payload, ensure_ascii=False).encode('utf-8'))
    payload['result']['blob'] = 'x' * (size - overhead) + tail
    encoded = json.dumps(payload, ensure_ascii=False).encode('utf-8')
    if len(encoded) != size:
        raise RuntimeError('response writing fixture has an incorrect size')
    path.write_bytes(encoded)
    return payload


def perform(phase, strategy, payload, request, fixture_path, response_path, memory_path, executor):
    if phase == 'response_write':
        writer = legacy_write_process_response if strategy == 'legacy' else _write_process_response
        return writer(payload, request, response_path, 0)
    try:
        result = executor.execute('response_write_benchmark', {
            'fixture': str(fixture_path), 'strategy': strategy,
            'memory_path': str(memory_path) if memory_path else None,
        })
    except JobExecutionError as exc:
        return ('error', exc.error_code, str(exc))
    return ('success', result)


def checked(phase, strategy, payload, request, fixture_path, response_path, memory_path, executor, expected, memory=False):
    if memory:
        tracemalloc.start()
    try:
        started = perf_counter()
        result = perform(phase, strategy, payload, request, fixture_path, response_path, memory_path if memory else None, executor)
        elapsed = perf_counter() - started
        parent_peak = tracemalloc.get_traced_memory()[1] if memory else None
    finally:
        if memory:
            tracemalloc.stop()
    if executor._active_processes:
        raise RuntimeError('response writing benchmark left an active process')
    if phase == 'response_write':
        code, encoded = expected
        if result != code or response_path.read_bytes() != encoded:
            raise RuntimeError('response writing benchmark changed the output bytes or exit code')
        writer_peak = parent_peak
    else:
        if result != expected:
            raise RuntimeError('response writing benchmark changed the process result or error')
        writer_peak = json.loads(memory_path.read_bytes())['writer_python_peak_bytes'] if memory else None
    if memory:
        return {'writer': writer_peak, 'executor_parent': parent_peak if phase == 'real_process' else None}
    return elapsed


def compare(phase, fixture_path, payload, unicode, quota, executor, samples, root):
    request = {'limits': {'max_result_bytes': quota}}
    response_path = root / 'response.json'
    memory_path = root / 'child-memory.json'
    code = legacy_write_process_response(payload, request, response_path, 0)
    encoded = response_path.read_bytes()
    expected = (code, encoded) if phase == 'response_write' else (
        ('error', 'execution_failed', f'job result exceeded {quota} byte limit') if code
        else ('success', payload['result'])
    )
    strategies = ('legacy', 'reused')
    peaks = {
        name: checked(phase, name, payload, request, fixture_path, response_path, memory_path, executor, expected, memory=True)
        for name in strategies
    }
    for name in strategies:
        checked(phase, name, payload, request, fixture_path, response_path, memory_path, executor, expected)
    pairs = []
    for index in range(samples):
        order = strategies if index % 2 == 0 else tuple(reversed(strategies))
        pairs.append({name: checked(phase, name, payload, request, fixture_path, response_path, memory_path, executor, expected) for name in order})
    before = median(pair['legacy'] for pair in pairs)
    after = median(pair['reused'] for pair in pairs)
    return {
        'phase': phase, 'payload_bytes': fixture_path.stat().st_size, 'unicode': unicode,
        'quota_bytes': quota, 'rejected': bool(code), 'response_bytes': len(encoded),
        'response_sha256': hashlib.sha256(encoded).hexdigest(), 'exit_code': code,
        'paired_samples': samples, 'legacy_median_seconds': before, 'reused_median_seconds': after,
        'elapsed_change_percent': (after / before - 1) * 100,
        'reused_faster_count': sum(pair['reused'] < pair['legacy'] for pair in pairs),
        'python_peak_bytes_outside_timing': peaks, 'pairs': pairs,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--workspace-root', type=Path, default=Path('output'))
    arguments = parser.parse_args()
    if arguments.samples < 2:
        parser.error('samples must be at least two')
    arguments.workspace_root.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='response-write-', dir=arguments.workspace_root.resolve()) as raw:
        root = Path(raw)
        runner = root / 'response_write_child.py'
        runner.write_text(CHILD_SOURCE.replace('REPOSITORY', repr(str(Path(__file__).resolve().parents[1]))), encoding='utf-8')
        for size, unicode, quota in ((4096, False, 16777216), (1048576, True, 16777216),
                                     (16777216, False, 16777216), (16777216, True, 1024)):
            fixture_path = root / f'{size}-{quota}.json'
            payload = fixture(fixture_path, size, unicode)
            executor = ProcessToolExecutor(
                ExecutionLimits(timeout_seconds=30, memory_limit_mb=0, cpu_time_seconds=0, max_result_bytes=quota),
                python_executable=sys.executable, runner_path=runner,
            )
            try:
                with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                    for phase in ('response_write', 'real_process'):
                        rows.append(compare(phase, fixture_path, payload, unicode, quota, executor, arguments.samples, root))
            finally:
                executor.shutdown()
    report = {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': {
            'response_write': 'JSON serialization, UTF-8 quota check and actual response file writing; excludes fixture creation/loading and verification',
            'real_process': 'real ProcessToolExecutor and Python child; includes workspace/request creation, launch, identical imports, fixture loading in child, actual JSON serialization/quota check/writing, wait, parent reading/parsing, result/error and workspace cleanup; fixed empty specification/environment contract; excludes scientific tool execution, API, HTTP sandbox and queue',
        },
        'output_bytes_exit_codes_and_process_results_or_errors_equal': True,
        'memory': 'one extra tracemalloc run per strategy/scenario outside timing; writer measured in current process or real child after fixture loading; real_process executor parent measured separately; excludes existing payload/fixture allocations, native allocations and OS cache',
        'timed_samples_traced': False, 'warmup_pairs_per_scenario': 1,
        'fixture_generation_verification_and_memory_measurement_outside_timing': True,
        'real_process_workspace_cleanup_in_timing': True,
        'oversize_payload_still_fully_serialized': True,
        'rows': rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
