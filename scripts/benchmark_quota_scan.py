import argparse
import asyncio
import hashlib
import json
import platform
import tempfile
from pathlib import Path
from statistics import median
from time import perf_counter

from scripts.benchmark_cdr_streaming import StreamingCleanScanner
from scripts.benchmark_quota_scan_baseline import BASELINE_SOURCE_COMMIT, WalkQuotaStorage
from scripts.benchmark_upload_usage import MemoryUpload, seed_history
from src.file_security import ContentDisarmReconstructor, FileSecurityPipeline
from src.file_storage import LocalFileStorage


HEARTBEAT_SECONDS = 0.001


class ProfiledQuotaStorage(LocalFileStorage):
    def __init__(self, root, implementation):
        self.scanner = StreamingCleanScanner()
        super().__init__(root, security_pipeline=FileSecurityPipeline(
            clamav=self.scanner, cdr=ContentDisarmReconstructor(), required=True,
        ))
        self.implementation = implementation
        self.usage_seconds = 0.0
        self.usage_calls = 0

    def _storage_usage(self, exclude_uploads=()):
        function = WalkQuotaStorage._storage_usage if self.implementation == 'walk' else LocalFileStorage._storage_usage
        started = perf_counter()
        try:
            return function(self, exclude_uploads)
        finally:
            self.usage_seconds += perf_counter() - started
            self.usage_calls += 1


def scan_sample(storage, expected):
    started = perf_counter()
    size = storage._storage_usage()
    elapsed = perf_counter() - started
    assert size == expected
    return elapsed


async def upload_sample(storage, payload, expected, concurrency):
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

    storage.usage_seconds = 0.0
    storage.usage_calls = 0
    storage.scanner.samples.clear()
    monitor = asyncio.create_task(heartbeat())
    await ready.wait()
    started = perf_counter()
    try:
        stored_files = await asyncio.gather(*(storage.save(
            MemoryUpload(payload), file_id=f'{index + 1:032x}',
        ) for index in range(concurrency)))
        elapsed = perf_counter() - started
    finally:
        stop.set()
        await monitor
    result = {
        'elapsed_seconds': elapsed,
        'quota_scan_seconds': storage.usage_seconds,
        'quota_scan_calls': storage.usage_calls,
        'max_heartbeat_gap_seconds': max(gaps),
    }
    assert storage.usage_calls == 2 * concurrency
    assert storage._reserved_bytes == 0 and not storage._upload_reservations
    assert len(storage.scanner.samples) == 2 * concurrency
    before = (hashlib.sha256(payload).hexdigest(), len(payload))
    after = (hashlib.sha256(expected).hexdigest(), len(expected))
    records = []
    for stored in stored_files:
        assert stored.path.read_bytes() == expected
        assert stored.size_bytes == len(expected) and stored.sha256 == after[0]
        assert stored.security == {
            'status': 'clean', 'clamav': 'clean', 'cdr': 'reconstructed', 'scan_count': 2,
        }
        assert [(digest, size) for file_id, digest, size in storage.scanner.samples if file_id == stored.file_id] == [before, after]
        assert storage.get(stored.file_id) == stored
        records.append((stored.path.parent / 'metadata.json').read_bytes())
    for stored in stored_files:
        await storage.discard(stored)
    return result, records


async def benchmark(samples, histories, concurrencies, workspace_root):
    workspace = Path(workspace_root or tempfile.gettempdir()).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    payload = b'\xef\xbb\xbf' + b'ACGT\r\n' * 512
    expected = b'ACGT\n' * 512
    scan_rows, upload_rows = [], []
    with tempfile.TemporaryDirectory(prefix='quota-scan-', dir=workspace) as raw:
        root = Path(raw).resolve()
        assert root.parent == workspace
        for history in histories:
            implementations = {
                name: ProfiledQuotaStorage(root / str(history) / name, name)
                for name in ('walk', 'scandir')
            }
            for storage in implementations.values():
                seed_history(storage.root, history)
            expected_usage = WalkQuotaStorage._storage_usage(implementations['walk'])
            excluded = frozenset([f'{1048576:032x}'])
            assert implementations['walk']._storage_usage(excluded) == implementations['scandir']._storage_usage(excluded)
            scan_pairs = []
            for sample in range(-1, samples):
                order = ['walk', 'scandir'] if sample % 2 == 0 else ['scandir', 'walk']
                pair = {name: scan_sample(implementations[name], expected_usage) for name in order}
                if sample >= 0:
                    scan_pairs.append(pair)
            before = median(pair['walk'] for pair in scan_pairs)
            after = median(pair['scandir'] for pair in scan_pairs)
            scan_rows.append({
                'historical_uploads': history,
                'historical_files': history * 2,
                'usage_bytes': expected_usage,
                'paired_samples': samples,
                'walk_median_seconds': before,
                'scandir_median_seconds': after,
                'elapsed_reduction_percent': (before - after) / before * 100,
                'scandir_faster_count': sum(pair['scandir'] < pair['walk'] for pair in scan_pairs),
                'pairs': scan_pairs,
            })
            for concurrency in concurrencies:
                pairs = []
                for sample in range(-1, samples):
                    order = ['walk', 'scandir'] if sample % 2 == 0 else ['scandir', 'walk']
                    pair = {'sample': sample, 'order': order}
                    records = {}
                    for name in order:
                        pair[name], records[name] = await upload_sample(
                            implementations[name], payload, expected, concurrency,
                        )
                    assert records['walk'] == records['scandir']
                    if sample >= 0:
                        pairs.append(pair)
                before = median(pair['walk']['elapsed_seconds'] for pair in pairs)
                after = median(pair['scandir']['elapsed_seconds'] for pair in pairs)
                upload_rows.append({
                    'historical_uploads': history,
                    'historical_files': history * 2,
                    'concurrent_uploads': concurrency,
                    'paired_samples': samples,
                    'walk_median_seconds': before,
                    'scandir_median_seconds': after,
                    'elapsed_reduction_percent': (before - after) / before * 100,
                    'walk_quota_scan_median_seconds': median(pair['walk']['quota_scan_seconds'] for pair in pairs),
                    'scandir_quota_scan_median_seconds': median(pair['scandir']['quota_scan_seconds'] for pair in pairs),
                    'quota_scan_calls_per_sample': 2 * concurrency,
                    'walk_max_gap_median_seconds': median(pair['walk']['max_heartbeat_gap_seconds'] for pair in pairs),
                    'scandir_max_gap_median_seconds': median(pair['scandir']['max_heartbeat_gap_seconds'] for pair in pairs),
                    'scandir_faster_count': sum(pair['scandir']['elapsed_seconds'] < pair['walk']['elapsed_seconds'] for pair in pairs),
                    'pairs': pairs,
                })
    return {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'scope': 'Fresh directory quota scans and LocalFileStorage.save with synthetic historical uploads, in-memory reads, real CDR and two local streaming clean scanner calls; excludes HTTP, real ClamAV networking and S3',
        'timer_excludes': 'fixture generation, verification and cleanup',
        'input_bytes_per_upload': len(payload),
        'output_bytes_per_upload': len(expected),
        'output_bytes_metadata_and_scan_digests_equal': True,
        'max_concurrent_quota_scans_per_storage': 1,
        'warmup_pairs_per_scenario': 1,
        'heartbeat_interval_seconds': HEARTBEAT_SECONDS,
        'scan_rows': scan_rows,
        'upload_rows': upload_rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--histories', default='0,256,2048')
    parser.add_argument('--concurrency', default='1,8')
    parser.add_argument('--workspace-root', type=Path)
    args = parser.parse_args()
    try:
        histories = [int(value) for value in args.histories.split(',')]
        concurrencies = [int(value) for value in args.concurrency.split(',')]
    except ValueError:
        parser.error('histories and concurrency must contain integers')
    if args.samples < 1 or not histories or min(histories) < 0 or not concurrencies or min(concurrencies) < 1:
        parser.error('samples and concurrency must be positive; histories must be nonnegative')
    print(json.dumps(asyncio.run(benchmark(
        args.samples, histories, concurrencies, args.workspace_root,
    )), indent=2))


if __name__ == '__main__':
    main()
