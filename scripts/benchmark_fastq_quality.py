import argparse
import gzip
import hashlib
import json
from pathlib import Path
import platform
import random
from statistics import median
import sys
import tempfile
from time import perf_counter
import tracemalloc
from types import SimpleNamespace
from unittest.mock import patch

from scripts.benchmark_fastq_quality_baseline import BASELINE_SOURCE_COMMIT, legacy_fastq_file_stats
from src import omics_fastq_qc
from src.job_execution import ExecutionLimits, ProcessToolExecutor


CHILD_SOURCE = '''import json
from pathlib import Path
import sys
from types import SimpleNamespace
sys.path.insert(0, REPOSITORY)
from scripts.benchmark_fastq_quality_baseline import legacy_fastq_file_stats
from src import omics_fastq_qc
from src.job_subprocess import _write_process_response
request = json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
arguments = request['arguments']
if arguments['strategy'] == 'legacy':
    omics_fastq_qc._fastq_file_stats = legacy_fastq_file_stats
memory_path = arguments.get('memory_path')
if memory_path:
    import tracemalloc
    tracemalloc.start()
result = omics_fastq_qc.execute_genomics_qc(arguments['input_path'], arguments['output_dir'], dependencies=SimpleNamespace())
if memory_path:
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    Path(memory_path).write_text(json.dumps({'qc_python_peak_bytes': peak}), encoding='utf-8')
raise SystemExit(_write_process_response({'ok': True, 'result': result}, request, Path(sys.argv[2]), 0))
'''


def fixture(path, reads, compressed, unicode):
    rng = random.Random(reads + int(unicode))
    content_hash = hashlib.sha256()
    sequence = 'ACGT' * 37 + 'AC'
    with path.open('wb') as target:
        source = gzip.GzipFile(filename='', mode='wb', compresslevel=6, fileobj=target, mtime=0) if compressed else target
        try:
            for start in range(0, reads, 1000):
                records = []
                for number in range(start, min(start + 1000, reads)):
                    quality = ''.join(rng.choices('!@ABCDEFGHIJ', k=150))
                    if unicode:
                        quality = quality[:-1] + '\u7814'
                    records.append(f'@read{number}\n{sequence}\n+\n{quality}\n')
                encoded = ''.join(records).encode('utf-8')
                source.write(encoded)
                content_hash.update(encoded)
        finally:
            if compressed:
                source.close()
    return content_hash.hexdigest()


def perform(phase, strategy, path, output, memory_path, executor):
    if phase == 'file_stats':
        parser = legacy_fastq_file_stats if strategy == 'legacy' else omics_fastq_qc._fastq_file_stats
        return parser(path)
    return executor.execute('fastq_quality_benchmark', {
        'input_path': str(path), 'output_dir': str(output), 'strategy': strategy,
        'memory_path': str(memory_path) if memory_path else None,
    })


def checked(phase, strategy, path, output, memory_path, executor, expected, manifest, memory=False):
    if memory:
        tracemalloc.start()
    try:
        started = perf_counter()
        result = perform(phase, strategy, path, output, memory_path if memory else None, executor)
        elapsed = perf_counter() - started
        parent_peak = tracemalloc.get_traced_memory()[1] if memory else None
    finally:
        if memory:
            tracemalloc.stop()
    if result != expected or executor._active_processes:
        raise RuntimeError('FASTQ quality benchmark changed statistics or left an active process')
    if phase == 'isolated_qc':
        if (output / 'genomics_qc.json').read_bytes() != manifest:
            raise RuntimeError('FASTQ quality benchmark changed the QC manifest bytes')
        qc_peak = json.loads(memory_path.read_bytes())['qc_python_peak_bytes'] if memory else None
    else:
        qc_peak = parent_peak
    return {'qc': qc_peak, 'executor_parent': parent_peak if phase == 'isolated_qc' else None} if memory else elapsed


def compare(phase, path, reads, compressed, unicode, digest, output, executor, samples, root):
    if phase == 'file_stats':
        expected = legacy_fastq_file_stats(path)
        manifest = None
        normalized = {key: value for key, value in expected.items() if key != 'path'}
    else:
        with patch.object(omics_fastq_qc, '_fastq_file_stats', legacy_fastq_file_stats):
            expected = omics_fastq_qc.execute_genomics_qc(path, output, dependencies=SimpleNamespace())
        manifest = (output / 'genomics_qc.json').read_bytes()
        normalized = {
            'status': expected['status'], 'input_type': expected['input_type'], 'tool': expected['tool'],
            'metrics': expected['metrics'],
            'files': [{key: value for key, value in item.items() if key != 'path'} for item in expected['files']],
        }
    memory_path = root / 'child-memory.json'
    strategies = ('legacy', 'translated')
    peaks = {
        name: checked(phase, name, path, output, memory_path, executor, expected, manifest, memory=True)
        for name in strategies
    }
    for name in strategies:
        checked(phase, name, path, output, memory_path, executor, expected, manifest)
    pairs = []
    for index in range(samples):
        order = strategies if index % 2 == 0 else tuple(reversed(strategies))
        pairs.append({name: checked(phase, name, path, output, memory_path, executor, expected, manifest) for name in order})
    before = median(pair['legacy'] for pair in pairs)
    after = median(pair['translated'] for pair in pairs)
    return {
        'phase': phase, 'reads': reads, 'read_length': 150, 'gzip': compressed, 'unicode': unicode,
        'input_bytes': path.stat().st_size, 'decompressed_content_sha256': digest,
        'normalized_result_sha256': hashlib.sha256(json.dumps(normalized, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest(),
        'paired_samples': samples, 'legacy_median_seconds': before, 'translated_median_seconds': after,
        'elapsed_change_percent': (after / before - 1) * 100,
        'translated_faster_count': sum(pair['translated'] < pair['legacy'] for pair in pairs),
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
    with tempfile.TemporaryDirectory(prefix='fastq-quality-', dir=arguments.workspace_root.resolve()) as raw:
        root = Path(raw)
        runner = root / 'fastq_quality_child.py'
        runner.write_text(CHILD_SOURCE.replace('REPOSITORY', repr(str(Path(__file__).resolve().parents[1]))), encoding='utf-8')
        executor = ProcessToolExecutor(
            ExecutionLimits(timeout_seconds=30, memory_limit_mb=0, cpu_time_seconds=0),
            python_executable=sys.executable, runner_path=runner,
        )
        try:
            with patch('src.job_execution._tool_spec', return_value=None), patch('src.job_execution._sandbox_environment', return_value=None):
                for reads, unicode in ((1000, False), (20000, False), (20000, True)):
                    for compressed in (False, True):
                        name = f'{reads}-{int(unicode)}'
                        path = root / (name + ('.fastq.gz' if compressed else '.fastq'))
                        digest = fixture(path, reads, compressed, unicode)
                        output = root / 'qc'
                        for phase in ('file_stats', 'isolated_qc'):
                            rows.append(compare(phase, path, reads, compressed, unicode, digest, output, executor, arguments.samples, root))
        finally:
            executor.shutdown()
    report = {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': {
            'file_stats': 'complete actual plain/gzip file reading, UTF-8 replacement decoding, FASTQ validation and statistics; excludes fixture creation and process launch',
            'isolated_qc': 'real ProcessToolExecutor/Python child and actual execute_genomics_qc; includes workspace/request creation, launch, identical imports, validation, actual plain/gzip reading and statistics, QC JSON manifest writing, response writing, wait, parent reading/parsing and cleanup; fixed empty specification/environment contract, no external executable dependencies; excludes plugin registry, scientific output contract validation, API, HTTP sandbox, queue and artifact publication',
        },
        'all_statistics_results_and_manifest_bytes_equal': True,
        'memory': 'one extra tracemalloc run per strategy/scenario outside timing; qc allocation scope is complete parser in current process or actual QC handler in real child after imports/before response writing; real executor parent measured separately; excludes fixture creation, native allocations and OS cache',
        'timed_samples_traced': False, 'warmup_pairs_per_scenario': 1,
        'fixture_generation_verification_and_memory_measurement_outside_timing': True,
        'isolated_workspace_cleanup_in_timing': True,
        'cross_platform_result_digest_excludes_only_environment_specific_paths': True,
        'rows': rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
