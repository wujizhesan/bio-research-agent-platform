import argparse
import asyncio
import hashlib
import json
import platform
import tempfile
from pathlib import Path
from statistics import median
from time import perf_counter

from src.file_storage import LocalFileStorage, METADATA_RESERVE_BYTES


BASELINE_SOURCE_COMMIT = '373ce90a05c07a651e5d6d6d6dbdc64d952306f0'
HEARTBEAT_SECONDS = 0.001


class ProfiledUsageStorage(LocalFileStorage):
    def __init__(self, root):
        super().__init__(root)
        self.usage_seconds = 0.0
        self.usage_calls = 0

    def _storage_usage(self, exclude_uploads=()):
        started = perf_counter()
        try:
            return super()._storage_usage(exclude_uploads)
        finally:
            self.usage_seconds += perf_counter() - started
            self.usage_calls += 1


class SynchronousUsageStorage(ProfiledUsageStorage):
    async def _begin_upload(self, file_id):
        async with self._quota_lock:
            self._quota_usage_bytes = self._storage_usage(self._upload_reservations)
            if (
                self._quota_usage_bytes + self._reserved_bytes + METADATA_RESERVE_BYTES
                > self.total_quota_bytes
            ):
                raise ValueError('upload storage quota exceeded')
            directory = self.root / file_id
            directory.mkdir(parents=False, exist_ok=False)
            self._upload_reservations[file_id] = METADATA_RESERVE_BYTES
            self._reserved_bytes += METADATA_RESERVE_BYTES
        return directory

    async def _commit_upload(self, stored):
        metadata = json.dumps({
            'file_id': stored.file_id,
            'filename': stored.filename,
            'content_type': stored.content_type,
            'size_bytes': stored.size_bytes,
            'sha256': stored.sha256,
            'storage_key': stored.storage_key,
            'version_id': stored.version_id,
            'security': stored.security,
        }, ensure_ascii=False).encode('utf-8')
        async with self._quota_lock:
            current_usage = self._storage_usage(self._upload_reservations)
            remaining = self._reserved_bytes - self._upload_reservations[stored.file_id]
            committed = stored.size_bytes + len(metadata)
            if current_usage + remaining + committed > self.total_quota_bytes:
                raise ValueError('upload storage quota exceeded')
            (stored.path.parent / 'metadata.json').write_bytes(metadata)
            self._quota_usage_bytes = current_usage + committed
            self._reserved_bytes -= self._upload_reservations.pop(stored.file_id)

    async def _abort_upload(self, file_id, directory):
        async with self._quota_lock:
            try:
                if directory.exists():
                    for child in directory.iterdir():
                        child.unlink(missing_ok=True)
                    directory.rmdir()
            finally:
                self._reserved_bytes -= self._upload_reservations.pop(file_id)
                self._quota_usage_bytes = self._storage_usage(self._upload_reservations)


class MemoryUpload:
    filename = 'sample.txt'

    def __init__(self, payload):
        self.payload = payload
        self.offset = 0

    async def read(self, size):
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


def seed_history(root, count):
    payload = b'ACGT\n' * 100
    for index in range(count):
        file_id = f'{index + 1048576:032x}'
        directory = root / file_id
        directory.mkdir()
        (directory / 'sample.txt').write_bytes(payload)
        (directory / 'metadata.json').write_text(json.dumps({
            'file_id': file_id, 'filename': 'sample.txt', 'size_bytes': len(payload),
        }), encoding='utf-8')


async def measure(storage, payload, concurrency):
    gaps = []
    ready = asyncio.Event()
    stop = asyncio.Event()

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
    storage.usage_seconds = 0.0
    storage.usage_calls = 0
    started = perf_counter()
    try:
        results = await asyncio.gather(*(storage.save(
            MemoryUpload(payload), file_id=f'{index + 1:032x}',
        ) for index in range(concurrency)))
        elapsed = perf_counter() - started
    finally:
        stop.set()
        await monitor
    result = {
        'elapsed_seconds': elapsed,
        'storage_scan_seconds': storage.usage_seconds,
        'storage_scan_calls': storage.usage_calls,
        'max_heartbeat_gap_seconds': max(gaps),
        'heartbeat_observations': len(gaps),
        'completed_count': len(results),
    }
    expected_sha = hashlib.sha256(payload).hexdigest()
    records = []
    for stored in results:
        assert stored.path.read_bytes() == payload
        assert stored.size_bytes == len(payload)
        assert stored.sha256 == expected_sha
        assert storage.get(stored.file_id) == stored
        records.append((stored.path.parent / 'metadata.json').read_bytes())
    assert storage._reserved_bytes == 0
    assert not storage._upload_reservations
    assert storage.usage_calls == 2 * concurrency
    for stored in results:
        await storage.discard(stored)
    result['records'] = records
    return result


async def benchmark(samples, histories, concurrency, workspace_root):
    payload = b'ACGT\n' * 512
    rows = []
    for history in histories:
        with tempfile.TemporaryDirectory(prefix='upload-usage-', dir=workspace_root) as raw:
            root = Path(raw).resolve()
            if workspace_root is not None:
                assert root.parent == workspace_root.resolve()
            implementations = {
                'synchronous': SynchronousUsageStorage(root / 'synchronous'),
                'threaded': ProfiledUsageStorage(root / 'threaded'),
            }
            for storage in implementations.values():
                seed_history(storage.root, history)
            pairs = []
            for sample in range(-1, samples):
                order = list(implementations)
                if sample % 2:
                    order.reverse()
                pair = {'sample': sample, 'order': order}
                for name in order:
                    pair[name] = await measure(implementations[name], payload, concurrency)
                assert pair['synchronous'].pop('records') == pair['threaded'].pop('records')
                if sample >= 0:
                    pairs.append(pair)
            before = median(pair['synchronous']['elapsed_seconds'] for pair in pairs)
            after = median(pair['threaded']['elapsed_seconds'] for pair in pairs)
            old_gap = median(pair['synchronous']['max_heartbeat_gap_seconds'] for pair in pairs)
            new_gap = median(pair['threaded']['max_heartbeat_gap_seconds'] for pair in pairs)
            rows.append({
                'historical_uploads': history,
                'historical_files': 2 * history,
                'paired_samples': samples,
                'synchronous_median_seconds': before,
                'threaded_median_seconds': after,
                'elapsed_change_percent': (after - before) / before * 100,
                'synchronous_max_gap_median_seconds': old_gap,
                'threaded_max_gap_median_seconds': new_gap,
                'heartbeat_gap_reduction_percent': (old_gap - new_gap) / old_gap * 100,
                'threaded_gap_lower_count': sum(
                    pair['threaded']['max_heartbeat_gap_seconds']
                    < pair['synchronous']['max_heartbeat_gap_seconds'] for pair in pairs
                ),
                'pairs': pairs,
            })
    return {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'scope': 'LocalFileStorage.save with synthetic historical upload folders and in-memory reads; excludes HTTP, ClamAV, CDR and S3',
        'timer_excludes': 'fixture generation, output verification, hashing and cleanup',
        'bytes_per_upload': len(payload),
        'concurrent_uploads': concurrency,
        'warmup_pairs_per_scenario': 1,
        'heartbeat_interval_seconds': HEARTBEAT_SECONDS,
        'max_concurrent_usage_scans_per_storage': 1,
        'output_bytes_and_metadata_equal': True,
        'rows': rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--histories', default='0,256,2048')
    parser.add_argument('--concurrency', type=int, default=8)
    parser.add_argument('--workspace-root', type=Path)
    args = parser.parse_args()
    try:
        histories = [int(item) for item in args.histories.split(',')]
    except ValueError:
        parser.error('--histories must contain nonnegative integers separated by commas')
    if args.samples < 1 or args.concurrency < 1 or not histories or min(histories) < 0:
        parser.error('samples and concurrency must be positive; histories must be nonnegative')
    if args.workspace_root is not None:
        args.workspace_root.mkdir(parents=True, exist_ok=True)
    print(json.dumps(asyncio.run(benchmark(
        args.samples, histories, args.concurrency, args.workspace_root,
    )), indent=2))


if __name__ == '__main__':
    main()
