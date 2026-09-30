import argparse
import asyncio
import builtins
import hashlib
import json
import os
from pathlib import Path
import platform
from statistics import median
import tempfile
import threading
from time import perf_counter
from unittest import mock

from scripts.benchmark_cdr_streaming import (
    MemoryUpload, StreamingCleanScanner, fixture, verify_file,
)
from scripts.benchmark_gzip_inspection_baseline import (
    BASELINE_SOURCE_COMMIT, SeparateHashStorage,
)
from src.file_security import ContentDisarmReconstructor, FileSecurityPipeline
from src.file_storage import CHUNK_SIZE, LocalFileStorage, SNIFF_BYTES, UTF8_SAMPLE_LOOKAHEAD_BYTES


HEARTBEAT_SECONDS = 0.001


class ProfiledStorage(LocalFileStorage):
    def __init__(self, root, implementation):
        self.scanner = StreamingCleanScanner()
        super().__init__(root, security_pipeline=FileSecurityPipeline(
            clamav=self.scanner, cdr=ContentDisarmReconstructor(), required=True,
        ))
        self.implementation = implementation
        self.guard = threading.Lock()
        self.inspection_seconds = 0.0
        self.inspection_calls = 0
        self.quota_scan_calls = 0

    def _storage_usage(self, exclude_uploads=()):
        self.quota_scan_calls += 1
        return super()._storage_usage(exclude_uploads)

    def _inspect_and_hash(self, target, filename, size_bytes):
        selected = SeparateHashStorage if self.implementation == 'separate' else LocalFileStorage
        started = perf_counter()
        try:
            return selected._inspect_and_hash(self, target, filename, size_bytes)
        finally:
            elapsed = perf_counter() - started
            with self.guard:
                self.inspection_seconds += elapsed
                self.inspection_calls += 1


def raw_read_sample(storage, target, expected):
    original_path_open, original_builtin_open = Path.open, builtins.open
    reads = []

    class Reader:
        def __init__(self, handle):
            self.handle = handle

        def read(self, size=-1):
            assert 0 <= size <= CHUNK_SIZE
            chunk = self.handle.read(size)
            reads.append(len(chunk))
            return chunk

        def __getattr__(self, name):
            return getattr(self.handle, name)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

    def counted_open(opener):
        def open_file(path, mode='r', *args, **kwargs):
            handle = opener(path, mode, *args, **kwargs)
            if isinstance(path, (str, os.PathLike)) and Path(path) == target and mode == 'rb':
                return Reader(handle)
            return handle
        return open_file

    with mock.patch.object(Path, 'open', counted_open(original_path_open)), mock.patch('builtins.open', counted_open(original_builtin_open)):
        content_type, digest = storage._inspect_and_hash(target, target.name, len(expected))
    assert digest.hexdigest() == hashlib.sha256(expected).hexdigest()
    return {'compressed_or_plain_bytes_read': sum(reads), 'read_calls': len(reads), 'content_type': content_type}


def inspection_sample(storage, target, expected):
    started = perf_counter()
    content_type, digest = storage._inspect_and_hash(target, target.name, len(expected))
    elapsed = perf_counter() - started
    assert digest.hexdigest() == hashlib.sha256(expected).hexdigest()
    assert content_type == ('application/gzip' if target.name.endswith('.gz') else 'text/tab-separated-values')
    return {'elapsed_seconds': elapsed}


async def upload_sample(root, implementation, filename, payload, expected, concurrency):
    storage = ProfiledStorage(root, implementation)
    ready, stop = asyncio.Event(), asyncio.Event()
    gaps = []

    async def heartbeat():
        previous = perf_counter()
        ready.set()
        while not stop.is_set():
            await asyncio.sleep(HEARTBEAT_SECONDS)
            current = perf_counter()
            gaps.append(current - previous)
            previous = current

    monitor = asyncio.create_task(heartbeat())
    await ready.wait()
    started = perf_counter()
    try:
        stored_files = await asyncio.gather(*(storage.save(
            MemoryUpload(filename, payload), file_id=f'{index + 1:032x}',
        ) for index in range(concurrency)))
        elapsed = perf_counter() - started
    finally:
        stop.set()
        await monitor
    report = {
        'elapsed_seconds': elapsed,
        'post_cdr_inspection_seconds': storage.inspection_seconds,
        'post_cdr_inspection_calls': storage.inspection_calls,
        'quota_scan_calls': storage.quota_scan_calls,
        'max_heartbeat_gap_seconds': max(gaps),
    }
    before = (hashlib.sha256(payload).hexdigest(), len(payload))
    after = (hashlib.sha256(expected).hexdigest(), len(expected))
    records = []
    for stored in stored_files:
        verify_file(stored.path, expected)
        assert stored.size_bytes == len(expected) and stored.sha256 == after[0]
        assert stored.security == {
            'status': 'clean', 'clamav': 'clean', 'cdr': 'reconstructed', 'scan_count': 2,
        }
        assert [(digest, size) for file_id, digest, size in storage.scanner.samples if file_id == stored.file_id] == [before, after]
        assert storage.get(stored.file_id) == stored
        records.append((stored.path.parent / 'metadata.json').read_bytes())
    assert storage.inspection_calls == concurrency
    assert storage.quota_scan_calls == 2 * concurrency
    assert len(storage.scanner.samples) == 2 * concurrency
    assert storage._reserved_bytes == 0 and not storage._upload_reservations
    for stored in stored_files:
        await storage.discard(stored)
    assert not list(root.iterdir())
    root.rmdir()
    return report, records


def paired_summary(pairs, upload=False):
    before = median(pair['separate']['elapsed_seconds'] for pair in pairs)
    after = median(pair['merged']['elapsed_seconds'] for pair in pairs)
    report = {
        'paired_samples': len(pairs),
        'separate_median_seconds': before,
        'merged_median_seconds': after,
        'elapsed_change_percent': (after - before) / before * 100,
        'merged_faster_count': sum(pair['merged']['elapsed_seconds'] < pair['separate']['elapsed_seconds'] for pair in pairs),
        'pairs': pairs,
    }
    if upload:
        for name in ('separate', 'merged'):
            report[f'{name}_post_cdr_inspection_median_seconds'] = median(pair[name]['post_cdr_inspection_seconds'] for pair in pairs)
            report[f'{name}_max_gap_median_seconds'] = median(pair[name]['max_heartbeat_gap_seconds'] for pair in pairs)
    return report


async def benchmark(samples, sizes, concurrencies, workspace_root):
    workspace = Path(workspace_root or tempfile.gettempdir()).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    inspection_rows, upload_rows = [], []
    with tempfile.TemporaryDirectory(prefix='gzip-inspection-', dir=workspace) as raw:
        root = Path(raw).resolve()
        assert root.parent == workspace
        for scenario in ('plain_text', 'vcf_gzip'):
            for size in sizes:
                filename, payload, expected, uncompressed_size = fixture(scenario, size)
                paths, implementations = {}, {}
                for name in ('separate', 'merged'):
                    implementations[name] = ProfiledStorage(root / f'{scenario}-{size}-{name}', name)
                    paths[name] = implementations[name].root / filename
                    paths[name].write_bytes(expected)
                reads = {name: raw_read_sample(implementations[name], paths[name], expected) for name in implementations}
                old_read = len(expected) + min(len(expected), SNIFF_BYTES + UTF8_SAMPLE_LOOKAHEAD_BYTES)
                if scenario == 'vcf_gzip':
                    old_read += len(expected)
                assert reads['separate']['compressed_or_plain_bytes_read'] == old_read
                assert reads['merged']['compressed_or_plain_bytes_read'] == (len(expected) + 2 if scenario == 'vcf_gzip' else old_read)
                pairs = []
                for sample in range(-1, samples):
                    order = ['separate', 'merged'] if sample % 2 == 0 else ['merged', 'separate']
                    pair = {name: inspection_sample(implementations[name], paths[name], expected) for name in order}
                    if sample >= 0:
                        pairs.append(pair)
                row = {'scenario': scenario, 'requested_size_mib': size, 'uncompressed_input_bytes': uncompressed_size, 'output_bytes': len(expected)}
                inspection_rows.append({**row, 'raw_read_measurement_outside_timing': reads, **paired_summary(pairs)})
                for path in paths.values():
                    path.unlink()
                    path.parent.rmdir()
                for concurrency in concurrencies:
                    pairs = []
                    for sample in range(-1, samples):
                        order = ['separate', 'merged'] if sample % 2 == 0 else ['merged', 'separate']
                        pair, records = {'sample': sample, 'order': order}, {}
                        for name in order:
                            pair[name], records[name] = await upload_sample(
                                root / f'{scenario}-{size}-{concurrency}-{name}',
                                name, filename, payload, expected, concurrency,
                            )
                        assert records['separate'] == records['merged']
                        if sample >= 0:
                            pairs.append(pair)
                    upload_rows.append({**row, 'concurrent_uploads': concurrency, **paired_summary(pairs, upload=True)})
    return {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': 'Post-CDR inspection and LocalFileStorage.save with in-memory inputs, real CDR, and two local streaming clean scanner calls; excludes HTTP, real ClamAV networking and S3',
        'timer_excludes': 'fixture generation, read instrumentation, verification and cleanup',
        'timed_samples_instrumented': False,
        'warmup_pairs_per_scenario': 1,
        'output_bytes_metadata_and_scan_digests_equal': True,
        'heartbeat_interval_seconds': HEARTBEAT_SECONDS,
        'inspection_rows': inspection_rows, 'upload_rows': upload_rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--sizes-mib', default='1,16')
    parser.add_argument('--concurrency', default='1,2')
    parser.add_argument('--workspace-root', type=Path)
    args = parser.parse_args()
    try:
        sizes = [int(value) for value in args.sizes_mib.split(',')]
        concurrencies = [int(value) for value in args.concurrency.split(',')]
    except ValueError:
        parser.error('sizes and concurrency must contain integers')
    if args.samples < 1 or not sizes or min(sizes) < 1 or not concurrencies or min(concurrencies) < 1:
        parser.error('samples, sizes and concurrency must be positive')
    print(json.dumps(asyncio.run(benchmark(args.samples, sizes, concurrencies, args.workspace_root)), indent=2))


if __name__ == '__main__':
    main()
