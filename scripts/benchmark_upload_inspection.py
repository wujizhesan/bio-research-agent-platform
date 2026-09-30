import argparse
import asyncio
import gzip
import hashlib
import json
import platform
import tempfile
import threading
from contextvars import copy_context
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any
from uuid import uuid4

from scripts.benchmark_vcf_compression import vcf_for
from src.file_security import (
    ContentDisarmReconstructor, FileSecurityPipeline, VCF_GZIP_COMPRESSION_LEVEL,
)
from src.file_storage import (
    CHUNK_SIZE, FILE_ID_PATTERN, MALWARE_MARKERS, MAX_CONCURRENT_SCANS,
    LocalFileStorage, StoredFile,
)


BASELINE_SOURCE_COMMIT = '1dff3a757edde6697d44388c2866f082526d47ba'
HEARTBEAT_SECONDS = 0.001


class StreamingCleanScanner:
    def __init__(self):
        self.samples = []
        self.guard = threading.Lock()

    def scan(self, path):
        digest = hashlib.sha256()
        with Path(path).open('rb') as handle:
            for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
                digest.update(chunk)
        with self.guard:
            self.samples.append((Path(path).parent.name, digest.hexdigest()))
        return 'clean'


class ProfiledInspectionStorage(LocalFileStorage):
    def __init__(self, root):
        self.scanner = StreamingCleanScanner()
        super().__init__(root, security_pipeline=FileSecurityPipeline(
            clamav=self.scanner, cdr=ContentDisarmReconstructor(), required=True,
        ))
        self.guard = threading.Lock()
        self.inspection_seconds = 0.0
        self.inspection_calls = 0

    def _inspect_content(self, target, filename, size_bytes):
        started = perf_counter()
        try:
            return super()._inspect_content(target, filename, size_bytes)
        finally:
            with self.guard:
                self.inspection_seconds += perf_counter() - started
                self.inspection_calls += 1


class SynchronousInspectionStorage(ProfiledInspectionStorage):
    async def save(self, upload: Any, file_id: str | None = None) -> StoredFile:
        filename = self._safe_filename(getattr(upload, 'filename', None))
        extension = Path(filename).suffix.lower()
        is_vcf_gzip = filename.lower().endswith('.vcf.gz')
        if extension not in self.allowed_extensions and not is_vcf_gzip:
            allowed = ', '.join(sorted(self.allowed_extensions | {'.vcf.gz'}))
            raise ValueError(f'unsupported file type: {extension or "none"}; allowed: {allowed}')

        file_id = str(file_id or uuid4().hex)
        if not FILE_ID_PATTERN.fullmatch(file_id):
            raise ValueError('invalid stored file id')
        directory = await self._begin_upload(file_id)
        target = directory / filename
        size_bytes = 0
        digest = hashlib.sha256()
        scan_tail = b''

        try:
            with target.open('wb') as output:
                while True:
                    chunk = await upload.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > self.max_bytes:
                        raise ValueError(
                            f'file exceeds maximum size of {self.max_bytes} bytes'
                        )
                    await self._reserve_upload_bytes(
                        file_id, size_bytes, 'upload storage quota exceeded',
                    )
                    scanned = scan_tail + chunk
                    if any(marker in scanned for marker in MALWARE_MARKERS):
                        raise ValueError('known malicious test signature detected')
                    scan_tail = scanned[-64:]
                    output.write(chunk)
                    digest.update(chunk)
            if size_bytes == 0:
                raise ValueError('empty files are not allowed')
            content_type = self._inspect_content(
                target, filename, size_bytes
            )
            security = None
            if self.security_pipeline is not None:
                scan_result = await self._scan_upload(target, filename)
                security = scan_result.as_dict()
                size_bytes = target.stat().st_size
                if size_bytes > self.max_bytes:
                    raise ValueError(
                        'CDR output exceeds maximum upload size'
                    )
                await self._reserve_upload_bytes(
                    file_id, size_bytes, 'CDR output exceeds upload storage quota',
                )
                content_type = self._inspect_content(
                    target, filename, size_bytes
                )
                digest = hashlib.sha256()
                with target.open('rb') as source:
                    for chunk in iter(lambda: source.read(CHUNK_SIZE), b''):
                        digest.update(chunk)
            stored = StoredFile(
                file_id=file_id,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=digest.hexdigest(),
                path=target,
                security=security,
            )
            await self._commit_upload(stored)
            return stored
        except BaseException:
            cleanup = asyncio.create_task(self._abort_upload(file_id, directory))
            while not cleanup.done():
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    continue
            cleanup.result()
            raise

    async def _scan_upload(self, target, filename):
        async with self._scan_slots:
            context = copy_context()
            pending = asyncio.get_running_loop().run_in_executor(
                None, context.run, self.security_pipeline.process, target, filename,
            )
            return await self._wait_for_worker(pending)


class MemoryUpload:
    filename = 'variants.vcf.gz'

    def __init__(self, payload):
        self.payload = payload
        self.offset = 0

    async def read(self, size):
        chunk = self.payload[self.offset:self.offset + size]
        self.offset += len(chunk)
        return chunk


async def measure(storage, payload, expected, concurrency):
    gaps = []
    ready, stop = asyncio.Event(), asyncio.Event()

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
    storage.inspection_seconds = 0.0
    storage.inspection_calls = 0
    storage.scanner.samples.clear()
    started = perf_counter()
    try:
        results = await asyncio.gather(*(storage.save(
            MemoryUpload(payload), file_id=f'{index + 1:032x}',
        ) for index in range(concurrency)))
        elapsed = perf_counter() - started
    finally:
        stop.set()
        await monitor
    report = {
        'elapsed_seconds': elapsed,
        'inspection_seconds': storage.inspection_seconds,
        'inspection_calls': storage.inspection_calls,
        'max_heartbeat_gap_seconds': max(gaps),
        'heartbeat_observations': len(gaps),
        'completed_count': len(results),
    }
    before_sha = hashlib.sha256(payload).hexdigest()
    after_sha = hashlib.sha256(expected).hexdigest()
    records = []
    for stored in results:
        assert stored.path.read_bytes() == expected
        assert stored.size_bytes == len(expected)
        assert stored.sha256 == after_sha
        assert stored.content_type == 'application/gzip'
        assert stored.security == {
            'status': 'clean', 'clamav': 'clean', 'cdr': 'reconstructed', 'scan_count': 2,
        }
        assert storage.get(stored.file_id) == stored
        assert [digest for file_id, digest in storage.scanner.samples if file_id == stored.file_id] == [before_sha, after_sha]
        records.append((stored.path.parent / 'metadata.json').read_bytes())
    assert storage.inspection_calls == 2 * concurrency
    assert len(storage.scanner.samples) == 2 * concurrency
    assert storage._reserved_bytes == 0
    assert not storage._upload_reservations
    for stored in results:
        await storage.discard(stored)
    assert not list(storage.root.iterdir())
    report['records'] = records
    return report


async def benchmark(samples, sizes_mib, concurrencies, workspace_root):
    workspace = Path(workspace_root or tempfile.gettempdir()).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='upload-inspection-', dir=workspace) as raw:
        root = Path(raw).resolve()
        assert root.parent == workspace
        for scenario in ('repeated_records', 'varied_annotations'):
            for size_mib in sizes_mib:
                content = vcf_for(scenario, size_mib * 1024 * 1024)
                normalized = ContentDisarmReconstructor()._reconstruct_text(content, '.vcf')
                payload = gzip.compress(content, compresslevel=9, mtime=0)
                expected = gzip.compress(normalized, compresslevel=VCF_GZIP_COMPRESSION_LEVEL, mtime=0)
                for concurrency in concurrencies:
                    implementations = {
                        'synchronous': SynchronousInspectionStorage(root / 'synchronous'),
                        'threaded': ProfiledInspectionStorage(root / 'threaded'),
                    }
                    pairs = []
                    for sample in range(-1, samples):
                        order = list(implementations)
                        if sample % 2:
                            order.reverse()
                        pair = {'sample': sample, 'order': order}
                        for name in order:
                            pair[name] = await measure(implementations[name], payload, expected, concurrency)
                        assert pair['synchronous'].pop('records') == pair['threaded'].pop('records')
                        if sample >= 0:
                            pairs.append(pair)
                    before = median(pair['synchronous']['elapsed_seconds'] for pair in pairs)
                    after = median(pair['threaded']['elapsed_seconds'] for pair in pairs)
                    old_gap = median(pair['synchronous']['max_heartbeat_gap_seconds'] for pair in pairs)
                    new_gap = median(pair['threaded']['max_heartbeat_gap_seconds'] for pair in pairs)
                    rows.append({
                        'scenario': scenario,
                        'requested_mib': size_mib,
                        'source_bytes': len(content),
                        'compressed_upload_bytes': len(payload),
                        'rebuilt_bytes': len(expected),
                        'concurrent_uploads': concurrency,
                        'paired_samples': samples,
                        'synchronous_median_seconds': before,
                        'threaded_median_seconds': after,
                        'elapsed_change_percent': (after - before) / before * 100,
                        'synchronous_max_gap_median_seconds': old_gap,
                        'threaded_max_gap_median_seconds': new_gap,
                        'heartbeat_gap_reduction_percent': (old_gap - new_gap) / old_gap * 100,
                        'threaded_gap_lower_count': sum(
                            pair['threaded']['max_heartbeat_gap_seconds'] < pair['synchronous']['max_heartbeat_gap_seconds']
                            for pair in pairs
                        ),
                        'pairs': pairs,
                    })
                    for storage in implementations.values():
                        storage.root.rmdir()
    return {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'scope': 'Complete LocalFileStorage.save with real VCF gzip inspection and CDR, two local streaming clean scan stubs, in-memory reads and empty history; excludes HTTP, ClamAV network and S3',
        'timer_excludes': 'fixture generation, output verification and cleanup',
        'warmup_pairs_per_scenario': 1,
        'heartbeat_interval_seconds': HEARTBEAT_SECONDS,
        'max_concurrent_file_workers_per_storage': MAX_CONCURRENT_SCANS,
        'vcf_gzip_compression_level': VCF_GZIP_COMPRESSION_LEVEL,
        'output_bytes_and_metadata_equal': True,
        'rows': rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--sizes-mib', default='1,16')
    parser.add_argument('--concurrency', default='1,2')
    parser.add_argument('--workspace-root', type=Path)
    args = parser.parse_args()
    try:
        sizes_mib = [int(item) for item in args.sizes_mib.split(',')]
        concurrencies = [int(item) for item in args.concurrency.split(',')]
    except ValueError:
        parser.error('sizes and concurrency must contain positive integers separated by commas')
    if args.samples < 1 or not sizes_mib or min(sizes_mib) < 1 or not concurrencies or min(concurrencies) < 1:
        parser.error('samples, sizes and concurrency must be positive')
    print(json.dumps(asyncio.run(benchmark(
        args.samples, sizes_mib, concurrencies, args.workspace_root,
    )), indent=2))


if __name__ == '__main__':
    main()
