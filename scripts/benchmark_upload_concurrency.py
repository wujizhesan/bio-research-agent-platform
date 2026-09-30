import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from statistics import median
import tempfile
import threading
from time import perf_counter, sleep
from typing import Any
from uuid import uuid4

from src.file_security import ContentDisarmReconstructor, FileSecurityPipeline
from src.file_storage import (
    CHUNK_SIZE, FILE_ID_PATTERN, MALWARE_MARKERS, METADATA_RESERVE_BYTES,
    MAX_CONCURRENT_SCANS, LocalFileStorage, StoredFile,
)


class SerializedUploadStorage(LocalFileStorage):
    def _storage_usage(self) -> int:
        total = 0
        for directory, _names, filenames in os.walk(self.root):
            for filename in filenames:
                try:
                    total += (Path(directory) / filename).stat().st_size
                except OSError:
                    continue
        return total

    async def save(self, upload: Any, file_id: str | None = None) -> StoredFile:
        filename = self._safe_filename(getattr(upload, 'filename', None))
        extension = Path(filename).suffix.lower()
        is_vcf_gzip = filename.lower().endswith('.vcf.gz')
        if extension not in self.allowed_extensions and not is_vcf_gzip:
            allowed = ', '.join(sorted(self.allowed_extensions | {'.vcf.gz'}))
            raise ValueError(f'unsupported file type: {extension or "none"}; allowed: {allowed}')

        async with self._quota_lock:
            current_usage = self._storage_usage()
            file_id = str(file_id or uuid4().hex)
            if not FILE_ID_PATTERN.fullmatch(file_id):
                raise ValueError('invalid stored file id')
            directory = self.root / file_id
            directory.mkdir(parents=False, exist_ok=False)
            target = directory / filename
            metadata_path = directory / 'metadata.json'
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
                        if (
                            current_usage + size_bytes + METADATA_RESERVE_BYTES
                            > self.total_quota_bytes
                        ):
                            raise ValueError('upload storage quota exceeded')
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
                    scan_result = await asyncio.to_thread(
                        self.security_pipeline.process, target, filename
                    )
                    security = scan_result.as_dict()
                    size_bytes = target.stat().st_size
                    if size_bytes > self.max_bytes:
                        raise ValueError(
                            'CDR output exceeds maximum upload size'
                        )
                    if (
                        current_usage + size_bytes + METADATA_RESERVE_BYTES
                        > self.total_quota_bytes
                    ):
                        raise ValueError('CDR output exceeds upload storage quota')
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
                metadata_path.write_text(json.dumps({
                    'file_id': stored.file_id,
                    'filename': stored.filename,
                    'content_type': stored.content_type,
                    'size_bytes': stored.size_bytes,
                    'sha256': stored.sha256,
                    'storage_key': stored.storage_key,
                    'version_id': stored.version_id,
                    'security': stored.security,
                }, ensure_ascii=False), encoding='utf-8')
                return stored
            except Exception:
                if directory.exists():
                    for child in directory.iterdir():
                        child.unlink(missing_ok=True)
                    directory.rmdir()
                raise


class DeferredUpload:
    def __init__(self, content, filename, read_delay):
        self.content = content
        self.filename = filename
        self.read_delay = read_delay
        self.offset = 0

    async def read(self, size):
        chunk = self.content[self.offset:self.offset + size]
        if chunk and self.read_delay:
            await asyncio.sleep(self.read_delay)
        self.offset += len(chunk)
        return chunk


class DelayedScanner:
    def __init__(self, delay):
        self.delay = delay
        self.lock = threading.Lock()
        self.bytes_scanned = 0
        self.calls = 0

    def scan(self, path):
        data = Path(path).read_bytes()
        if self.delay:
            sleep(self.delay)
        with self.lock:
            self.bytes_scanned += len(data)
            self.calls += 1
        return 'clean'


async def measure(storage_type, root, payload, concurrency, read_delay, scan_delay):
    scanner = DelayedScanner(scan_delay) if scan_delay else None
    pipeline = FileSecurityPipeline(
        clamav=scanner, cdr=ContentDisarmReconstructor(), required=True,
    ) if scanner is not None else None
    storage = storage_type(root, security_pipeline=pipeline)
    started = perf_counter()

    async def save(index):
        upload = DeferredUpload(payload, f'reads-{index}.fastq', read_delay)
        stored = await storage.save(upload, file_id=f'{index + 1:032x}')
        return stored, perf_counter() - started

    results = await asyncio.gather(*(save(index) for index in range(concurrency)))
    elapsed = perf_counter() - started
    expected_sha = hashlib.sha256(payload).hexdigest()
    records = []
    for stored, _ in results:
        assert stored.path.read_bytes() == payload
        assert stored.size_bytes == len(payload)
        assert stored.sha256 == expected_sha
        assert storage.get(stored.file_id) == stored
        metadata = (stored.path.parent / 'metadata.json').read_bytes()
        assert json.loads(metadata)['sha256'] == expected_sha
        records.append(metadata)
    assert storage._reserved_bytes == 0
    assert not storage._upload_reservations
    if scanner is not None:
        assert scanner.calls == 2 * concurrency
        assert scanner.bytes_scanned == 2 * concurrency * len(payload)
        assert all(stored.security == {
            'status': 'clean', 'clamav': 'clean', 'cdr': 'reconstructed', 'scan_count': 2,
        } for stored, _ in results)
    return {
        'elapsed_seconds': elapsed,
        'first_completion_seconds': min(completed for _, completed in results),
        'completed_count': len(results),
        'scanned_bytes': scanner.bytes_scanned if scanner is not None else 0,
        'records': records,
    }


async def benchmark(samples, concurrencies, size_mib, workspace_root=None):
    unit = b'@read\nACGTACGTACGTACGT\n+\nIIIIIIIIIIIIIIII\n'
    size = size_mib * 1024 * 1024
    payload = (unit * (size // len(unit) + 1))[:size]
    rows = []
    for scenario, read_delay, scan_delay in (
        ('read_wait', 0.04, 0.0),
        ('scan_wait_with_cdr', 0.0, 0.025),
        ('no_wait', 0.0, 0.0),
    ):
        for concurrency in concurrencies:
            pairs = []
            for sample in range(-1, samples):
                order = ('serialized', 'reserved') if sample % 2 == 0 else ('reserved', 'serialized')
                measured = {}
                with tempfile.TemporaryDirectory(prefix='upload-benchmark-', dir=workspace_root) as raw:
                    for name in order:
                        kind = SerializedUploadStorage if name == 'serialized' else LocalFileStorage
                        measured[name] = await measure(
                            kind, Path(raw) / name, payload, concurrency, read_delay, scan_delay,
                        )
                assert measured['serialized']['records'] == measured['reserved']['records']
                for result in measured.values():
                    result.pop('records')
                if sample >= 0:
                    pairs.append({'sample': sample, 'order': order, **measured})
            before = median(pair['serialized']['elapsed_seconds'] for pair in pairs)
            after = median(pair['reserved']['elapsed_seconds'] for pair in pairs)
            rows.append({
                'scenario': scenario,
                'concurrency': concurrency,
                'read_delay_seconds_per_chunk': read_delay,
                'scan_delay_seconds_per_pass': scan_delay,
                'paired_samples': samples,
                'serialized_median_seconds': before,
                'reserved_median_seconds': after,
                'elapsed_reduction_percent': (before - after) / before * 100,
                'reserved_faster_count': sum(
                    pair['reserved']['elapsed_seconds'] < pair['serialized']['elapsed_seconds']
                    for pair in pairs
                ),
                'pairs': pairs,
            })
    return {
        'baseline_source_commit': 'b4847d1b83736adbfb467d138df706ebbfae1221',
        'scope': 'LocalFileStorage.save, controlled read/scan waiting, real CDR and local files',
        'bytes_per_file': size,
        'max_concurrent_scans': MAX_CONCURRENT_SCANS,
        'warmup_pairs_per_scenario': 1,
        'metadata_bytes_equal': True,
        'rows': rows,
    }


def main():
    parser = argparse.ArgumentParser(description='Compare serialized and reserved upload quotas')
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--concurrency', default='2,4,8')
    parser.add_argument('--size-mib', type=int, default=1)
    parser.add_argument('--workspace-root', type=Path)
    args = parser.parse_args()
    concurrencies = [int(value) for value in args.concurrency.split(',')]
    if args.samples < 2 or args.size_mib < 1 or not concurrencies or any(value < 1 for value in concurrencies):
        parser.error('samples must be at least two; size and concurrency must be positive')
    if args.workspace_root is not None:
        args.workspace_root.mkdir(parents=True, exist_ok=True)
    print(json.dumps(asyncio.run(benchmark(
        args.samples, concurrencies, args.size_mib, args.workspace_root,
    )), sort_keys=True))


if __name__ == '__main__':
    main()
