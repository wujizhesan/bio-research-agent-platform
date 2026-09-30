import argparse
import asyncio
import gc
import gzip
import hashlib
import json
from pathlib import Path
import platform
from statistics import median
import subprocess
import sys
import tempfile
import threading
from time import perf_counter
import tracemalloc

from scripts.benchmark_cdr_baseline import BASELINE_SOURCE_COMMIT, WholeFileReconstructor
from scripts.benchmark_vcf_compression import vcf_for
from src.file_security import (
    ContentDisarmReconstructor, FileSecurityPipeline,
    TEXT_RECONSTRUCTION_CHUNK_BYTES, VCF_GZIP_COMPRESSION_LEVEL,
)
from src.file_storage import CHUNK_SIZE, LocalFileStorage


HEARTBEAT_SECONDS = 0.001


def reconstructor(implementation):
    return WholeFileReconstructor() if implementation == 'whole_file' else ContentDisarmReconstructor()


def fixture(scenario, size_mib):
    requested = size_mib * 1024 * 1024
    if scenario == 'vcf_gzip':
        content = vcf_for('varied_annotations', requested)
        filename = 'variants.vcf.gz'
    else:
        unit = (
            'AA科研🙂\tACGTACGTACGTACGTZ\r\n'.encode()
            if scenario == 'unicode_text' else b'chr1\t12345\tGeneA\t0.125\r\n'
        )
        content = unit * (requested // len(unit))
        filename = 'sample.tsv'
    normalized = WholeFileReconstructor()._reconstruct_text(content, '.vcf' if scenario == 'vcf_gzip' else '.tsv')
    payload = gzip.compress(content, compresslevel=6, mtime=0) if scenario == 'vcf_gzip' else content
    expected = gzip.compress(normalized, compresslevel=6, mtime=0) if scenario == 'vcf_gzip' else normalized
    if scenario == 'vcf_gzip':
        assert len(content) <= len(payload) * 100
    return filename, payload, expected, len(content)


def verify_file(path, expected):
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
            digest.update(chunk)
            size += len(chunk)
    assert size == len(expected)
    assert digest.hexdigest() == hashlib.sha256(expected).hexdigest()
    assert not path.with_name('.' + path.name + '.cdr').exists()


def memory_worker(implementation, path, filename):
    selected = reconstructor(implementation)
    gc.collect()
    tracemalloc.start()
    selected.reconstruct(path, filename)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss = None
    if sys.platform.startswith('linux'):
        # getrusage can retain the parent's peak across exec.
        status = Path('/proc/self/status').read_text(encoding='ascii')
        peak = next(line for line in status.splitlines() if line.startswith('VmHWM:'))
        rss = int(peak.split()[1]) * 1024
    return {'traced_peak_bytes': peak, 'process_peak_rss_bytes': rss}


def reconstruction_sample(root, implementation, filename, payload, expected, memory=False):
    path = root / filename
    path.write_bytes(payload)
    if memory:
        result = subprocess.run([
            sys.executable, '-m', 'scripts.benchmark_cdr_streaming',
            '--memory-worker', implementation, '--input-path', str(path),
            '--filename', filename,
        ], check=True, stdout=subprocess.PIPE)
        report = json.loads(result.stdout)
    else:
        selected = reconstructor(implementation)
        started = perf_counter()
        selected.reconstruct(path, filename)
        report = {'elapsed_seconds': perf_counter() - started}
    verify_file(path, expected)
    path.unlink()
    return report


class StreamingCleanScanner:
    def __init__(self):
        self.samples = []
        self.guard = threading.Lock()

    def scan(self, path):
        digest = hashlib.sha256()
        size = 0
        with Path(path).open('rb') as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
                digest.update(chunk)
                size += len(chunk)
        with self.guard:
            self.samples.append((Path(path).parent.name, digest.hexdigest(), size))
        return 'clean'


class MemoryUpload:
    def __init__(self, filename, payload):
        self.filename = filename
        self.payload = payload
        self.offset = 0

    async def read(self, size):
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


async def upload_sample(root, implementation, filename, payload, expected, concurrency, memory=False):
    scanner = StreamingCleanScanner()
    storage = LocalFileStorage(root, security_pipeline=FileSecurityPipeline(
        clamav=scanner, cdr=reconstructor(implementation), required=True,
    ))
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

    monitor = None
    if memory:
        gc.collect()
        tracemalloc.start()
    else:
        monitor = asyncio.create_task(heartbeat())
        await ready.wait()
    started = perf_counter()
    try:
        results = await asyncio.gather(*(storage.save(
            MemoryUpload(filename, payload), file_id=f'{index + 1:032x}',
        ) for index in range(concurrency)))
        elapsed = perf_counter() - started
        if memory:
            _, peak = tracemalloc.get_traced_memory()
            report = {'traced_peak_bytes': peak}
        else:
            report = {'elapsed_seconds': elapsed}
    finally:
        if memory:
            tracemalloc.stop()
        else:
            stop.set()
            await monitor
    if not memory:
        report['max_heartbeat_gap_seconds'] = max(gaps)
    before = (hashlib.sha256(payload).hexdigest(), len(payload))
    after = (hashlib.sha256(expected).hexdigest(), len(expected))
    records = []
    for stored in results:
        verify_file(stored.path, expected)
        assert stored.size_bytes == len(expected) and stored.sha256 == after[0]
        assert stored.security == {
            'status': 'clean', 'clamav': 'clean', 'cdr': 'reconstructed', 'scan_count': 2,
        }
        assert [(digest, size) for file_id, digest, size in scanner.samples if file_id == stored.file_id] == [before, after]
        assert storage.get(stored.file_id) == stored
        records.append((stored.path.parent / 'metadata.json').read_bytes())
    assert len(scanner.samples) == 2 * concurrency
    assert storage._reserved_bytes == 0 and not storage._upload_reservations
    for stored in results:
        await storage.discard(stored)
    assert not list(root.iterdir())
    root.rmdir()
    report['records'] = records
    return report


def paired_summary(timing_pairs, memory_pairs, upload=False):
    before = median(pair['whole_file']['elapsed_seconds'] for pair in timing_pairs)
    after = median(pair['streaming']['elapsed_seconds'] for pair in timing_pairs)
    old_peak = median(pair['whole_file']['traced_peak_bytes'] for pair in memory_pairs)
    new_peak = median(pair['streaming']['traced_peak_bytes'] for pair in memory_pairs)
    report = {
        'paired_timing_samples': len(timing_pairs),
        'paired_memory_samples': len(memory_pairs),
        'whole_file_median_seconds': before,
        'streaming_median_seconds': after,
        'elapsed_change_percent': (after - before) / before * 100,
        'streaming_faster_count': sum(pair['streaming']['elapsed_seconds'] < pair['whole_file']['elapsed_seconds'] for pair in timing_pairs),
        'whole_file_traced_peak_median_bytes': old_peak,
        'streaming_traced_peak_median_bytes': new_peak,
        'traced_peak_reduction_percent': (old_peak - new_peak) / old_peak * 100,
        'timing_pairs': timing_pairs,
        'memory_pairs': memory_pairs,
    }
    if upload:
        old_gap = median(pair['whole_file']['max_heartbeat_gap_seconds'] for pair in timing_pairs)
        new_gap = median(pair['streaming']['max_heartbeat_gap_seconds'] for pair in timing_pairs)
        report.update({
            'whole_file_max_gap_median_seconds': old_gap,
            'streaming_max_gap_median_seconds': new_gap,
            'heartbeat_gap_reduction_percent': (old_gap - new_gap) / old_gap * 100,
        })
    else:
        old_rss = [pair['whole_file']['process_peak_rss_bytes'] for pair in memory_pairs]
        new_rss = [pair['streaming']['process_peak_rss_bytes'] for pair in memory_pairs]
        if all(value is not None for value in [*old_rss, *new_rss]):
            report['whole_file_process_peak_rss_median_bytes'] = median(old_rss)
            report['streaming_process_peak_rss_median_bytes'] = median(new_rss)
            report['process_peak_rss_reduction_percent'] = (median(old_rss) - median(new_rss)) / median(old_rss) * 100
    return report


async def benchmark(samples, memory_samples, sizes_mib, upload_sizes_mib, concurrencies, workspace_root):
    workspace = Path(workspace_root or tempfile.gettempdir()).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    reconstruction_rows, upload_rows = [], []
    with tempfile.TemporaryDirectory(prefix='cdr-streaming-', dir=workspace) as raw:
        root = Path(raw).resolve()
        assert root.parent == workspace
        for scenario in ('ascii_text', 'unicode_text', 'vcf_gzip'):
            for size_mib in sizes_mib:
                filename, payload, expected, source_bytes = fixture(scenario, size_mib)
                timing_pairs, memory_pairs = [], []
                for memory, count in ((False, samples), (True, memory_samples)):
                    for sample in range(-1 if not memory else 0, count):
                        order = ['whole_file', 'streaming']
                        if sample % 2:
                            order.reverse()
                        pair = {'sample': sample, 'order': order}
                        for name in order:
                            pair[name] = reconstruction_sample(root, name, filename, payload, expected, memory)
                        if sample >= 0:
                            (memory_pairs if memory else timing_pairs).append(pair)
                reconstruction_rows.append({
                    'scenario': scenario, 'requested_mib': size_mib,
                    'source_bytes': source_bytes, 'input_bytes': len(payload),
                    'rebuilt_bytes': len(expected), 'output_sha256': hashlib.sha256(expected).hexdigest(),
                    **paired_summary(timing_pairs, memory_pairs),
                })
            for size_mib in upload_sizes_mib:
                filename, payload, expected, source_bytes = fixture(scenario, size_mib)
                for concurrency in concurrencies:
                    timing_pairs, memory_pairs = [], []
                    for memory, count in ((False, samples), (True, memory_samples)):
                        for sample in range(-1 if not memory else 0, count):
                            order = ['whole_file', 'streaming']
                            if sample % 2:
                                order.reverse()
                            pair = {'sample': sample, 'order': order}
                            for name in order:
                                pair[name] = await upload_sample(root / name, name, filename, payload, expected, concurrency, memory)
                            assert pair['whole_file'].pop('records') == pair['streaming'].pop('records')
                            if sample >= 0:
                                (memory_pairs if memory else timing_pairs).append(pair)
                    upload_rows.append({
                        'scenario': scenario, 'requested_mib': size_mib,
                        'source_bytes': source_bytes, 'input_bytes': len(payload),
                        'rebuilt_bytes': len(expected), 'concurrent_uploads': concurrency,
                        'output_sha256': hashlib.sha256(expected).hexdigest(),
                        **paired_summary(timing_pairs, memory_pairs, upload=True),
                    })
        assert not list(root.iterdir())
    return {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(), 'python_version': platform.python_version(),
        'scope': 'Complete CDR reconstruction and LocalFileStorage.save with real text/gzip inspection, CDR and two local streaming clean scanner stubs; in-memory upload reads and empty history; excludes HTTP, real ClamAV network and S3',
        'timer_excludes': 'fixture generation, staging reconstruction inputs, memory tracking, output verification and rollback/discard cleanup',
        'memory_scope': 'Separate samples; Python allocations traced after fixtures exist. Reconstruction uses a fresh child per sample; Linux process peak RSS includes interpreter/imports. Upload tracing includes both file workers but excludes preallocated payload/expected bytes; upload RSS is not measured.',
        'linux_rss_measurement': '/proc/self/status VmHWM in the child after exec; excludes inherited pre-exec getrusage peaks',
        'timed_samples_instrumented': False,
        'warmup_pairs_per_scenario': 1,
        'output_bytes_metadata_and_scan_digests_equal': True,
        'text_chunk_bytes': TEXT_RECONSTRUCTION_CHUNK_BYTES,
        'vcf_gzip_compression_level': VCF_GZIP_COMPRESSION_LEVEL,
        'heartbeat_interval_seconds': HEARTBEAT_SECONDS,
        'reconstruction_rows': reconstruction_rows, 'upload_rows': upload_rows,
    }


def positive_list(parser, value, label):
    try:
        selected = [int(item) for item in value.split(',')]
    except ValueError:
        parser.error(f'{label} must contain positive integers separated by commas')
    if not selected or min(selected) < 1:
        parser.error(f'{label} must be positive')
    return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--memory-samples', type=int, default=3)
    parser.add_argument('--sizes-mib', default='1,32')
    parser.add_argument('--upload-sizes-mib', default='1,16')
    parser.add_argument('--concurrency', default='1,2')
    parser.add_argument('--workspace-root', type=Path)
    parser.add_argument('--memory-worker', choices=('whole_file', 'streaming'))
    parser.add_argument('--input-path', type=Path)
    parser.add_argument('--filename')
    args = parser.parse_args()
    if args.memory_worker:
        if args.input_path is None or not args.filename:
            parser.error('memory worker requires an input path and filename')
        print(json.dumps(memory_worker(args.memory_worker, args.input_path, args.filename)))
        return
    if args.samples < 1 or args.memory_samples < 1:
        parser.error('samples must be positive')
    print(json.dumps(asyncio.run(benchmark(
        args.samples, args.memory_samples,
        positive_list(parser, args.sizes_mib, 'sizes'),
        positive_list(parser, args.upload_sizes_mib, 'upload sizes'),
        positive_list(parser, args.concurrency, 'concurrency'),
        args.workspace_root,
    )), indent=2))


if __name__ == '__main__':
    main()
