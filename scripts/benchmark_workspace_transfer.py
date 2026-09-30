import argparse
import hashlib
import json
import os
from pathlib import Path
import platform
from statistics import median
import stat
import tempfile
from time import perf_counter
from types import SimpleNamespace
from unittest.mock import patch

from scripts.benchmark_workspace_transfer_baseline import BASELINE_SOURCE_COMMIT, LegacyWorkspaceExecutor
from src.plugin_container import ContainerToolExecutor


def snapshot(root):
    paths = [root, *sorted(root.rglob('*'))] if root.is_dir() else [root]
    result = []
    for path in paths:
        info = path.stat()
        digest = None
        if path.is_file():
            digest = hashlib.sha256()
            with path.open('rb') as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
            digest = digest.hexdigest()
        result.append((path.relative_to(root).as_posix(), stat.S_IMODE(info.st_mode), info.st_mtime_ns, info.st_size if path.is_file() else None, digest))
    return result


def fixture(root, count, file_size):
    source = root / 'source'
    if count is None:
        source.write_bytes(bytes(range(256)) * (file_size // 256))
    else:
        source.mkdir()
        for index in range(32):
            (source / f'group-{index:02d}' / 'empty').mkdir(parents=True)
        for index in range(count):
            (source / f'group-{index % 32:02d}' / f'研究-{index:06d}.bin').write_bytes(
                hashlib.sha256(str(index).encode()).digest() * (file_size // 32)
            )
    for path in [source, *source.rglob('*')] if source.is_dir() else [source]:
        path.chmod(0o755 if path.is_dir() else 0o640)
        os.utime(path, ns=(1700000000000000000, 1700000000123456000))
    return source


def run_transfer(executor_type, source, expected, scenario, phase, instrument=False):
    with tempfile.TemporaryDirectory(prefix=f'{phase}-{scenario}-', dir=source.parent) as raw:
        root = Path(raw)
        workspace = root / 'workspace'
        workspace.mkdir()
        artifacts = root / 'artifacts'
        artifacts.mkdir()
        target = artifacts / 'result'
        executor = SimpleNamespace(input_roots=(source.parent,), artifact_root=artifacts, workspace_max_bytes=1024 ** 3)
        traversals = []
        original_rglob = Path.rglob

        def counted(path, *args, **kwargs):
            if path == source:
                traversals.append('rglob')
            return original_rglob(path, *args, **kwargs)

        module = 'scripts.benchmark_workspace_transfer_baseline' if executor_type is LegacyWorkspaceExecutor else 'src.plugin_container'
        with patch(module + '._tool_filesystem_contract', return_value=({'input_path'}, {}, set())):
            if instrument:
                with patch.object(Path, 'rglob', counted):
                    result, copied = perform(executor_type, executor, source, workspace, target, phase)
                elapsed = None
            else:
                started = perf_counter()
                result, copied = perform(executor_type, executor, source, workspace, target, phase)
                elapsed = perf_counter() - started
        assert snapshot(copied) == expected
        if phase == 'stage':
            assert result == ({'input_path': str(copied), 'unrelated': 7}, ())
        else:
            assert result == ((str(source), str(target)),)
        return {'elapsed_seconds': elapsed, 'source_rglob_calls': len(traversals)}


def perform(executor_type, executor, source, workspace, target, phase):
    if phase == 'stage':
        result = executor_type._stage_workspace(executor, 'benchmark', {'input_path': str(source), 'unrelated': 7}, workspace)
        return result, Path(result[0]['input_path'])
    result = executor_type._publish_outputs(executor, [(str(source), str(target), source.is_dir())])
    return result, target


def compare(source, expected, count, phase, samples):
    scenario = 'directory' if count is not None else 'single_file'
    measured = {
        name: run_transfer(executor_type, source, expected, scenario, phase, instrument=True)
        for name, executor_type in (('legacy', LegacyWorkspaceExecutor), ('manifest', ContainerToolExecutor))
    }
    for executor_type in (LegacyWorkspaceExecutor, ContainerToolExecutor):
        run_transfer(executor_type, source, expected, scenario, phase)
    pairs = []
    for index in range(samples):
        pair = {}
        order = [('legacy', LegacyWorkspaceExecutor), ('manifest', ContainerToolExecutor)]
        if index % 2:
            order.reverse()
        for name, executor_type in order:
            pair[name] = run_transfer(executor_type, source, expected, scenario, phase)
        pairs.append(pair)
    before = median(pair['legacy']['elapsed_seconds'] for pair in pairs)
    after = median(pair['manifest']['elapsed_seconds'] for pair in pairs)
    return {
        'scenario': scenario, 'phase': phase, 'file_count': count or 1,
        'source_bytes': sum(item[3] or 0 for item in expected),
        'paired_samples': samples, 'legacy_median_seconds': before, 'manifest_median_seconds': after,
        'elapsed_change_percent': (after / before - 1) * 100,
        'manifest_faster_count': sum(pair['manifest']['elapsed_seconds'] < pair['legacy']['elapsed_seconds'] for pair in pairs),
        'source_traversals_outside_timing': measured, 'pairs': pairs,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=5)
    parser.add_argument('--file-counts', default='32,512,2048')
    parser.add_argument('--single-file-mib', type=int, default=16)
    parser.add_argument('--workspace-root', type=Path, default=Path('output'))
    arguments = parser.parse_args()
    counts = [int(value) for value in arguments.file_counts.split(',')]
    if arguments.samples < 1 or any(value < 1 for value in counts) or arguments.single_file_mib < 1:
        parser.error('sample counts and sizes must be positive')
    arguments.workspace_root.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='workspace-transfer-', dir=arguments.workspace_root.resolve()) as raw:
        root = Path(raw)
        for index, count in enumerate([*counts, None]):
            group = root / str(index)
            group.mkdir()
            source = fixture(group, count, 128 if count is not None else arguments.single_file_mib * 1024 * 1024)
            expected = snapshot(source)
            for phase in ('stage', 'publish'):
                rows.append(compare(source, expected, count, phase, arguments.samples))
    report = {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': 'Complete local sandbox input staging and output publication measured separately, including quota calculation, checks, byte copying and metadata; excludes container execution, HTTP and S3',
        'output_bytes_hashes_metadata_empty_directories_and_mapping_equal': True,
        'timed_samples_instrumented': False, 'warmup_pairs_per_scenario': 1,
        'fixture_generation_verification_and_cleanup_outside_timing': True,
        'directory_copy_enumeration': {'legacy': 'shutil.copytree enumerates source again', 'manifest': 'copies captured manifest without further enumeration'},
        'rows': rows,
    }
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
