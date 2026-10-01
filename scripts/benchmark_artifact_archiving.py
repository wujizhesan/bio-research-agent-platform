import argparse
import gzip
import hashlib
import json
from pathlib import Path
import random
from statistics import median
import tarfile
import tempfile
from time import perf_counter

from scripts.benchmark_secure_jobs import percentile
from src.artifact_store import _archive_directory, _reservation_size, _sha256


def archive_with_reread(source, target, root_name):
    with target.open('wb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(fileobj=compressed, mode='w', format=tarfile.PAX_FORMAT) as archive:
                for entry in [source, *sorted(source.rglob('*'))]:
                    arcname = Path(root_name) / entry.relative_to(source)
                    info = archive.gettarinfo(str(entry), arcname.as_posix())
                    info.uid = 0
                    info.gid = 0
                    info.uname = ''
                    info.gname = ''
                    info.mtime = 0
                    info.mode = 0o755 if entry.is_dir() else 0o644
                    if entry.is_file():
                        with entry.open('rb') as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)
    return _sha256(target)


def make_dataset(root, size_mib, profile, file_count=16):
    root.mkdir(parents=True)
    size = size_mib * 1024 * 1024
    rng = random.Random(42)
    if profile == 'incompressible':
        payload = rng.randbytes(size)
        extension = '.bin'
    elif profile == 'synthetic_fastq':
        block = ''.join(
            f'@read-{index}\n' + ''.join(rng.choices('ACGT', k=100)) + '\n+\n'
            + 'I' * 100 + '\n' for index in range(4096)
        ).encode('ascii')
        payload = (block * (size // len(block) + 1))[:size]
        extension = '.fastq'
    else:
        raise ValueError('unsupported archive benchmark profile')
    width = (len(payload) + file_count - 1) // file_count
    for index in range(file_count):
        directory = root / f'group-{index % 4}'
        directory.mkdir(exist_ok=True)
        (directory / f'part-{index:03d}{extension}').write_bytes(
            payload[index * width:(index + 1) * width]
        )
    return {
        item.relative_to(root).as_posix(): _sha256(item)
        for item in root.rglob('*') if item.is_file()
    }


def verify_archive(target, expected, result):
    digest, size = _sha256(target)
    if (digest, size) != result:
        raise AssertionError('archive digest or length did not match the resulting file')
    files = {}
    with tarfile.open(target, 'r:gz') as archive:
        for member in archive:
            if (
                member.uid != 0 or member.gid != 0 or member.uname or member.gname
                or member.mtime != 0 or member.mode != (0o755 if member.isdir() else 0o644)
            ):
                raise AssertionError('archive metadata changed')
            if not member.isfile():
                continue
            relative = Path(member.name).relative_to('dataset').as_posix()
            digest = hashlib.sha256()
            size = 0
            with archive.extractfile(member) as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                    digest.update(chunk)
                    size += len(chunk)
            files[relative] = (digest.hexdigest(), size)
    if files != expected:
        raise AssertionError('archive members did not match the source files')


def compare(root, size_mib, profile, samples):
    source = root / 'source'
    expected = make_dataset(source, size_mib, profile)
    strategies = {'reread': archive_with_reread, 'streaming': _archive_directory}
    timings = {name: [] for name in strategies}
    reference = None
    for index in range(-1, samples):
        order = tuple(strategies) if index % 2 else tuple(reversed(strategies))
        for name in order:
            target = root / f'{index}-{name}.tar.gz'
            started = perf_counter()
            result = strategies[name](source, target, 'dataset')
            elapsed = perf_counter() - started
            verify_archive(target, expected, result)
            if reference is not None and result != reference:
                raise AssertionError('archive strategies produced different bytes')
            reference = result
            target.unlink()
            if index >= 0:
                timings[name].append(elapsed)
    return {
        'source_size_mib': size_mib,
        'profile': profile,
        'source_file_count': len(expected),
        'archive_size_bytes': reference[1],
        **{
            name: {
                'median_seconds': round(median(values), 6),
                'p95_seconds': round(percentile(values, 95), 6),
                'seconds': [round(value, 6) for value in values],
            } for name, values in timings.items()
        },
        'comparison': {
            'streaming_median_improvement_percent': round(
                100 * (1 - median(timings['streaming']) / median(timings['reread'])), 1
            ),
            'streaming_wins': sum(
                new < old for old, new in zip(timings['reread'], timings['streaming'])
            ),
            'paired_samples': samples,
            'verification_reread_bytes_saved_per_archive': reference[1],
        },
    }


def compare_compression_levels(root, size_mib, profile, samples):
    source = root / 'source'
    expected = make_dataset(source, size_mib, profile)
    reserved_bytes = _reservation_size(source, archive=True)
    levels = (1, 6, 9)
    timings = {level: [] for level in levels}
    references = {}
    for index in range(-1, samples):
        offset = index % len(levels)
        for level in levels[offset:] + levels[:offset]:
            target = root / f'{index}-{level}.tar.gz'
            started = perf_counter()
            result = _archive_directory(source, target, 'dataset', compresslevel=level)
            elapsed = perf_counter() - started
            verify_archive(target, expected, result)
            if result[1] > reserved_bytes:
                raise AssertionError('archive exceeded its publication reservation')
            if level in references and result != references[level]:
                raise AssertionError('compression level produced inconsistent archive bytes')
            references[level] = result
            target.unlink()
            if index >= 0:
                timings[level].append(elapsed)
    baseline = median(timings[9])
    level_medians = {level: median(timings[level]) for level in levels}
    level_sizes = {level: references[level][1] for level in levels}
    return {
        'source_size_mib': size_mib,
        'profile': profile,
        'source_file_count': len(expected),
        'reserved_bytes': reserved_bytes,
        'paired_samples_per_level': samples,
        'levels': {
            str(level): {
                'median_seconds': round(level_medians[level], 6),
                'p95_seconds': round(percentile(timings[level], 95), 6),
                'archive_size_bytes': level_sizes[level],
                'median_improvement_vs_level_9_percent': round(
                    100 * (1 - level_medians[level] / baseline), 1
                ),
                'extra_bytes_vs_level_9': level_sizes[level] - level_sizes[9],
                'break_even_transfer_mib_per_second': round(
                    (level_sizes[level] - level_sizes[9])
                    / (baseline - level_medians[level]) / (1024 * 1024), 3
                ) if level_sizes[level] > level_sizes[9] and level_medians[level] < baseline else None,
                'wins_vs_level_9': sum(
                    faster < slower
                    for faster, slower in zip(timings[level], timings[9])
                ) if level != 9 else None,
                'seconds': [round(value, 6) for value in timings[level]],
            } for level in levels
        },
    }


def benchmark(samples, sizes, workspace_root=None):
    rows = []
    with tempfile.TemporaryDirectory(prefix='artifact_archive_benchmark_', dir=workspace_root) as raw:
        for size in sizes:
            for profile in ('synthetic_fastq', 'incompressible'):
                rows.append(compare(Path(raw) / f'{size}-{profile}', size, profile, samples))
    return {
        'scope': 'local deterministic directory packing and SHA-256 verification; '
        'uses synthetic FASTQ-shaped text and incompressible bytes; excludes S3 upload, '
        'network, queue, API and tool execution',
        'verification': 'every archive is independently reread, SHA-256/length checked, '
        'and every decompressed member and its metadata verified outside the timed interval',
        'compression': 'unchanged gzip defaults and deterministic tar metadata',
        'workspace_root': str(Path(workspace_root or tempfile.gettempdir()).resolve()),
        'warmup_pairs_per_dataset': 1,
        'datasets': rows,
    }


def benchmark_compression_levels(samples, sizes, workspace_root=None):
    rows = []
    with tempfile.TemporaryDirectory(prefix='artifact_compression_benchmark_', dir=workspace_root) as raw:
        for size in sizes:
            for profile in ('synthetic_fastq', 'incompressible'):
                rows.append(compare_compression_levels(
                    Path(raw) / f'{size}-{profile}', size, profile, samples,
                ))
    return {
        'scope': 'local deterministic directory packing and SHA-256 generation; '
        'uses synthetic FASTQ-shaped text and incompressible bytes; '
        'excludes S3 upload, network, queue, API and tool execution',
        'break_even_model': 'additional archive bytes divided by median packing time saved; '
        'assumes equal fixed upload overhead and ignores retries and network variation',
        'verification': 'every archive is independently reread, SHA-256/length checked, '
        'and every decompressed member and its metadata verified outside the timed interval',
        'workspace_root': str(Path(workspace_root or tempfile.gettempdir()).resolve()),
        'warmup_per_level_per_dataset': 1,
        'datasets': rows,
    }


def main():
    parser = argparse.ArgumentParser(description='Compare artifact archive verification strategies')
    parser.add_argument('--samples', type=int, default=16)
    parser.add_argument('--sizes-mib', default='1,16')
    parser.add_argument('--workspace-root')
    parser.add_argument('--compression-levels', action='store_true')
    args = parser.parse_args()
    try:
        sizes = [int(value) for value in args.sizes_mib.split(',')]
    except ValueError:
        parser.error('sizes-mib must contain positive integers')
    if args.samples < 2 or not sizes or any(size < 1 for size in sizes):
        parser.error('samples must be at least two and sizes must be positive')
    if args.workspace_root is not None and not Path(args.workspace_root).is_dir():
        parser.error('workspace-root must be an existing directory')
    run = benchmark_compression_levels if args.compression_levels else benchmark
    print(json.dumps(run(args.samples, sizes, args.workspace_root), sort_keys=True))


if __name__ == '__main__':
    main()
