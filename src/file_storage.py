"""Secure local storage for uploaded research inputs."""

import asyncio
import codecs
import gzip
import hashlib
import json
import os
import re
import shutil
from contextvars import copy_context
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from uuid import uuid4

try:
    from .storage_workspace import (
        SHA256_PATTERN,
        S3ObjectReference,
        StorageIntegrityError,
        materialize_storage_references,
    )
except ImportError:
    from storage_workspace import (
        SHA256_PATTERN,
        S3ObjectReference,
        StorageIntegrityError,
        materialize_storage_references,
    )


DEFAULT_ALLOWED_EXTENSIONS = frozenset({
    '.bed', '.csv', '.fa', '.fasta', '.fastq', '.fq', '.fna', '.gff', '.gff3',
    '.gtf', '.html', '.htm', '.json', '.md', '.tsv', '.txt', '.vcf', '.yaml', '.yml',
})
FILE_ID_PATTERN = re.compile(r'^[a-f0-9]{32}$')
CHUNK_SIZE = 1024 * 1024
SNIFF_BYTES = 64 * 1024
UTF8_SAMPLE_LOOKAHEAD_BYTES = 3
METADATA_RESERVE_BYTES = 1024
MAX_CONCURRENT_SCANS = 2
ARCHIVE_SIGNATURES = (
    b'PK\x03\x04', b'PK\x05\x06', b'PK\x07\x08', b'Rar!\x1a\x07',
    b'7z\xbc\xaf\x27\x1c',
)
EXECUTABLE_SIGNATURES = (
    b'MZ', b'\x7fELF', b'\xfe\xed\xfa\xce', b'\xfe\xed\xfa\xcf',
    b'\xce\xfa\xed\xfe', b'\xcf\xfa\xed\xfe',
)
MALWARE_MARKERS = (
    b'X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR',
)
CONTENT_TYPES = {
    '.csv': 'text/csv',
    '.html': 'text/plain',
    '.htm': 'text/plain',
    '.json': 'application/json',
    '.md': 'text/markdown',
    '.tsv': 'text/tab-separated-values',
    '.yaml': 'application/yaml',
    '.yml': 'application/yaml',
}


class _HashingReader:
    def __init__(self, source, digest):
        self.source = source
        self.digest = digest

    def read(self, size=-1):
        chunk = self.source.read(size)
        self.digest.update(chunk)
        return chunk


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    path: Path
    storage_key: str | None = None
    version_id: str | None = None
    security: dict | None = None


class LocalFileStorage:
    backend = 'local'

    def __init__(
        self,
        root: str | Path,
        max_bytes: int = 50 * 1024 * 1024,
        total_quota_bytes: int = 10 * 1024 * 1024 * 1024,
        max_decompressed_bytes: int = 200 * 1024 * 1024,
        max_compression_ratio: float = 100.0,
        allowed_extensions: frozenset[str] = DEFAULT_ALLOWED_EXTENSIONS,
        security_pipeline=None,
    ):
        if max_bytes < 1:
            raise ValueError('max_bytes must be positive')
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.total_quota_bytes = max(int(total_quota_bytes), 1)
        self.max_decompressed_bytes = max(int(max_decompressed_bytes), 1)
        self.max_compression_ratio = max(float(max_compression_ratio), 1.0)
        self.allowed_extensions = frozenset(item.lower() for item in allowed_extensions)
        self.security_pipeline = security_pipeline
        self.root.mkdir(parents=True, exist_ok=True)
        self._quota_lock = asyncio.Lock()
        self._upload_reservations = {}
        self._reserved_bytes = 0
        self._quota_usage_bytes = 0
        self._scan_slots = asyncio.Semaphore(MAX_CONCURRENT_SCANS)

    def ping(self):
        if not self.root.is_dir() or not os.access(self.root, os.R_OK | os.W_OK):
            raise RuntimeError('local storage is unavailable')

    @staticmethod
    def _safe_filename(filename: str | None) -> str:
        candidate = Path(str(filename or 'upload')).name
        candidate = re.sub(r'[^A-Za-z0-9._-]+', '_', candidate).strip(' .')
        if not candidate or candidate in {'.', '..'}:
            candidate = 'upload'
        if candidate.startswith('.'):
            candidate = f'upload{candidate}'
        return candidate[:180]

    def _resolve_file_path(self, stored: StoredFile) -> Path:
        path = stored.path.resolve()
        try:
            path.relative_to(self.root)
        except ValueError as exc:
            raise ValueError('stored file is outside storage root') from exc
        return path

    def _storage_usage(self, exclude_uploads=()) -> int:
        root = os.fspath(self.root)
        pending = [root]
        total = 0
        while pending:
            directory = pending.pop()
            if directory != root and os.path.islink(directory):
                continue
            directories = []
            files = []
            try:
                with os.scandir(directory) as entries:
                    for entry in entries:
                        try:
                            is_directory = entry.is_dir()
                        except OSError:
                            is_directory = False
                        if is_directory:
                            if directory != root or entry.name not in exclude_uploads:
                                directories.append(entry.path)
                        else:
                            files.append(entry)
            except OSError:
                continue
            for entry in files:
                try:
                    total += entry.stat().st_size
                except OSError:
                    continue
            pending.extend(reversed(directories))
        return total

    @staticmethod
    async def _wait_for_worker(pending):
        try:
            return await asyncio.shield(pending)
        except asyncio.CancelledError:
            # Keep the caller's quota lock or worker slot until the worker exits.
            while not pending.done():
                try:
                    await asyncio.shield(pending)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not pending.cancelled():
                pending.exception()
            raise

    async def _storage_usage_async(self):
        context = copy_context()
        pending = asyncio.get_running_loop().run_in_executor(
            None, context.run, self._storage_usage,
            frozenset(self._upload_reservations),
        )
        return await self._wait_for_worker(pending)

    async def _begin_upload(self, file_id):
        async with self._quota_lock:
            self._quota_usage_bytes = await self._storage_usage_async()
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

    async def _reserve_upload_bytes(self, file_id, size_bytes, message):
        async with self._quota_lock:
            previous = self._upload_reservations[file_id]
            reserved = size_bytes + METADATA_RESERVE_BYTES
            if (
                self._quota_usage_bytes + self._reserved_bytes - previous + reserved
                > self.total_quota_bytes
            ):
                raise ValueError(message)
            self._upload_reservations[file_id] = reserved
            self._reserved_bytes += reserved - previous

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
            current_usage = await self._storage_usage_async()
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
                self._quota_usage_bytes = await self._storage_usage_async()

    async def _run_file_worker(self, function, *args):
        async with self._scan_slots:
            context = copy_context()
            pending = asyncio.get_running_loop().run_in_executor(
                None, context.run, function, *args,
            )
            return await self._wait_for_worker(pending)

    async def _scan_upload(self, target, filename):
        return await self._run_file_worker(
            self.security_pipeline.process, target, filename,
        )

    @staticmethod
    def _validate_text(content: bytes, *, sample_limit: int | None = None) -> str:
        if any(content.startswith(signature) for signature in ARCHIVE_SIGNATURES):
            raise ValueError('archive uploads are not allowed')
        if any(content.startswith(signature) for signature in EXECUTABLE_SIGNATURES):
            raise ValueError('executable content is not allowed')
        if any(marker in content for marker in MALWARE_MARKERS):
            raise ValueError('known malicious test signature detected')
        if b'\x00' in content:
            raise ValueError('binary content does not match the file extension')
        try:
            if sample_limit is None or len(content) <= sample_limit:
                return content.decode('utf-8-sig')
            decoder = codecs.getincrementaldecoder('utf-8-sig')()
            text = decoder.decode(content[:sample_limit])
            if decoder.getstate()[0]:
                for byte in content[sample_limit:sample_limit + UTF8_SAMPLE_LOOKAHEAD_BYTES]:
                    text += decoder.decode(bytes([byte]))
                    if not decoder.getstate()[0]:
                        break
            return text + decoder.decode(b'', final=True)
        except UnicodeDecodeError as exc:
            raise ValueError('uploaded research files must be UTF-8 text') from exc

    def _inspect_gzip(self, target, compressed_size: int) -> str:
        total = 0
        sample = bytearray()
        sample_bytes = SNIFF_BYTES + UTF8_SAMPLE_LOOKAHEAD_BYTES
        scan_tail = b''
        try:
            with gzip.open(target, 'rb') as handle:
                while True:
                    chunk = handle.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > self.max_decompressed_bytes:
                        raise ValueError(
                            'compressed file exceeds decompressed size limit'
                        )
                    if total > compressed_size * self.max_compression_ratio:
                        raise ValueError('compressed file exceeds compression ratio limit')
                    scanned = scan_tail + chunk
                    if any(marker in scanned for marker in MALWARE_MARKERS):
                        raise ValueError('known malicious test signature detected')
                    scan_tail = scanned[-64:]
                    if len(sample) < sample_bytes:
                        sample.extend(chunk[:sample_bytes - len(sample)])
        except (gzip.BadGzipFile, EOFError, OSError) as exc:
            raise ValueError('invalid gzip content') from exc
        text = self._validate_text(bytes(sample), sample_limit=SNIFF_BYTES)
        if not text.lstrip().startswith(('##fileformat=VCF', '#CHROM')):
            raise ValueError('gzip content does not match .vcf.gz')
        return 'application/gzip'

    def _inspect_content(self, target: Path, filename: str, size_bytes: int) -> str:
        with target.open('rb') as handle:
            sample = handle.read(SNIFF_BYTES + UTF8_SAMPLE_LOOKAHEAD_BYTES)
        if filename.lower().endswith('.vcf.gz'):
            if not sample.startswith(b'\x1f\x8b'):
                raise ValueError('content does not match .vcf.gz')
            return self._inspect_gzip(target, size_bytes)
        if sample.startswith(b'\x1f\x8b'):
            raise ValueError('compressed content does not match the file extension')
        text = self._validate_text(sample, sample_limit=SNIFF_BYTES)
        extension = Path(filename).suffix.lower()
        if extension == '.vcf' and not text.lstrip().startswith(
            ('##fileformat=VCF', '#CHROM')
        ):
            raise ValueError('content does not match .vcf')
        return CONTENT_TYPES.get(extension, 'text/plain')

    def _inspect_and_hash(self, target, filename, size_bytes):
        if filename.lower().endswith('.vcf.gz'):
            digest = hashlib.sha256()
            with target.open('rb') as source:
                if source.read(2) != b'\x1f\x8b':
                    raise ValueError('content does not match .vcf.gz')
                source.seek(0)
                content_type = self._inspect_gzip(
                    _HashingReader(source, digest), size_bytes,
                )
            return content_type, digest
        content_type = self._inspect_content(target, filename, size_bytes)
        digest = hashlib.sha256()
        with target.open('rb') as source:
            for chunk in iter(lambda: source.read(CHUNK_SIZE), b''):
                digest.update(chunk)
        return content_type, digest

    def planned_storage_key(self, file_id: str, filename: str) -> str | None:
        if not FILE_ID_PATTERN.fullmatch(str(file_id)):
            raise ValueError('invalid stored file id')
        return None

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
            content_type = await self._run_file_worker(
                self._inspect_content, target, filename, size_bytes,
            )
            security = None
            if self.security_pipeline is not None:
                scan_result = await self._scan_upload(target, filename)
                security = scan_result.as_dict()
                size_bytes = (await self._run_file_worker(target.stat)).st_size
                if size_bytes > self.max_bytes:
                    raise ValueError(
                        'CDR output exceeds maximum upload size'
                    )
                await self._reserve_upload_bytes(
                    file_id, size_bytes, 'CDR output exceeds upload storage quota',
                )
                content_type, digest = await self._run_file_worker(
                    self._inspect_and_hash, target, filename, size_bytes,
                )
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

    def get(self, file_id: str, reference=None) -> StoredFile:
        if reference is not None:
            raise FileNotFoundError(file_id)
        if not FILE_ID_PATTERN.fullmatch(file_id):
            raise FileNotFoundError(file_id)
        directory = (self.root / file_id).resolve()
        try:
            directory.relative_to(self.root)
        except ValueError as exc:
            raise FileNotFoundError(file_id) from exc
        metadata_path = directory / 'metadata.json'
        if not metadata_path.is_file():
            raise FileNotFoundError(file_id)
        try:
            payload = json.loads(metadata_path.read_text(encoding='utf-8'))
            filename = self._safe_filename(payload['filename'])
            stored = StoredFile(
                file_id=file_id,
                filename=filename,
                content_type=str(payload.get('content_type') or 'application/octet-stream'),
                size_bytes=int(payload['size_bytes']),
                sha256=str(payload['sha256']),
                path=directory / filename,
                storage_key=payload.get('storage_key'),
                version_id=payload.get('version_id'),
                security=payload.get('security'),
            )
            path = self._resolve_file_path(stored)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FileNotFoundError(file_id) from exc
        if not path.is_file():
            raise FileNotFoundError(file_id)
        return stored

    async def discard(self, stored: StoredFile):
        if not FILE_ID_PATTERN.fullmatch(stored.file_id):
            raise ValueError('invalid stored file id')
        directory = (self.root / stored.file_id).resolve()
        if directory.parent != self.root:
            raise ValueError('stored file is outside storage root')
        await asyncio.to_thread(shutil.rmtree, directory, True)

    def payload(self, stored: StoredFile, project_root: str | Path, download_url: str) -> dict[str, Any]:
        project_path = Path(project_root).resolve()
        try:
            tool_path = stored.path.resolve().relative_to(project_path).as_posix()
        except ValueError:
            tool_path = str(stored.path.resolve())
        return {
            'file_id': stored.file_id,
            'filename': stored.filename,
            'content_type': stored.content_type,
            'size_bytes': stored.size_bytes,
            'sha256': stored.sha256,
            'path': tool_path,
            'download_url': download_url,
            'storage_key': stored.storage_key,
            'version_id': stored.version_id,
            'security': stored.security,
        }


class S3FileStorage(LocalFileStorage):
    backend = 's3'

    def __init__(
        self,
        root: str | Path,
        bucket: str,
        prefix: str = 'bio-agent',
        endpoint_url: str | None = None,
        region_name: str | None = None,
        expected_bucket_owner: str | None = None,
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
        session_token: str | None = None,
        max_bytes: int = 50 * 1024 * 1024,
        total_quota_bytes: int = 10 * 1024 * 1024 * 1024,
        max_decompressed_bytes: int = 200 * 1024 * 1024,
        max_compression_ratio: float = 100.0,
        allowed_extensions: frozenset[str] = DEFAULT_ALLOWED_EXTENSIONS,
        security_pipeline=None,
        client=None,
    ):
        super().__init__(
            root,
            max_bytes=max_bytes,
            total_quota_bytes=total_quota_bytes,
            max_decompressed_bytes=max_decompressed_bytes,
            max_compression_ratio=max_compression_ratio,
            allowed_extensions=allowed_extensions,
            security_pipeline=security_pipeline,
        )
        if not bucket or not bucket.strip():
            raise ValueError('S3_BUCKET is required when STORAGE_BACKEND=s3')
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError('boto3 is required when STORAGE_BACKEND=s3') from exc
        self.bucket = bucket.strip()
        self.prefix = prefix.strip('/')
        self.expected_bucket_owner = str(expected_bucket_owner or '').strip() or None
        self.client = client or boto3.client(
            's3',
            endpoint_url=endpoint_url or None,
            region_name=region_name or None,
            aws_access_key_id=access_key_id or os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=secret_access_key or os.environ.get('AWS_SECRET_ACCESS_KEY'),
            aws_session_token=session_token or os.environ.get('AWS_SESSION_TOKEN'),
        )

    def _bucket_request(self, **values):
        request = {'Bucket': self.bucket, **values}
        if self.expected_bucket_owner:
            request['ExpectedBucketOwner'] = self.expected_bucket_owner
        return request

    def ping(self):
        super().ping()
        self.client.head_bucket(**self._bucket_request())

    def _object_key(self, file_id, filename):
        parts = [item for item in (self.prefix, file_id, filename) if item]
        return '/'.join(parts)

    def planned_storage_key(self, file_id: str, filename: str) -> str:
        if not FILE_ID_PATTERN.fullmatch(str(file_id)):
            raise ValueError('invalid stored file id')
        return self._object_key(str(file_id), self._safe_filename(filename))

    async def save(self, upload: Any, file_id: str | None = None) -> StoredFile:
        stored = await super().save(upload, file_id=file_id)
        storage_key = self._object_key(stored.file_id, stored.filename)
        try:
            await asyncio.to_thread(
                self.client.upload_file,
                str(stored.path),
                self.bucket,
                storage_key,
                ExtraArgs={
                    'ContentType': stored.content_type,
                    'Metadata': {
                        'file-id': stored.file_id,
                        'sha256': stored.sha256,
                        'security-status': str(
                            (stored.security or {}).get('status', 'unscanned')
                        ),
                    },
                    **(
                        {'ExpectedBucketOwner': self.expected_bucket_owner}
                        if self.expected_bucket_owner else {}
                    ),
                },
            )
            head = await asyncio.to_thread(
                self.client.head_object,
                **self._bucket_request(Key=storage_key),
            )
            version_id = str(head.get('VersionId') or '')
            remote_sha256 = str((head.get('Metadata') or {}).get('sha256') or '').lower()
            if not version_id or version_id == 'null':
                raise StorageIntegrityError('S3 upload did not return a durable VersionId')
            if remote_sha256 != stored.sha256:
                raise StorageIntegrityError('S3 upload checksum metadata verification failed')
            if int(head.get('ContentLength') or 0) != stored.size_bytes:
                raise StorageIntegrityError('S3 upload size verification failed')
        except Exception:
            for child in stored.path.parent.iterdir():
                child.unlink(missing_ok=True)
            stored.path.parent.rmdir()
            raise
        metadata_path = stored.path.parent / 'metadata.json'
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        metadata['storage_key'] = storage_key
        metadata['version_id'] = version_id
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        stored.path.unlink(missing_ok=True)
        return replace(stored, storage_key=storage_key, version_id=version_id)

    def _find_remote_object(self, file_id):
        response = self.client.list_objects_v2(
            **self._bucket_request(
                Prefix=f'{self.prefix}/{file_id}/' if self.prefix else f'{file_id}/',
            ),
        )
        objects = response.get('Contents') or []
        for item in objects:
            key = item.get('Key')
            if key and not key.endswith('/'):
                return key
        raise FileNotFoundError(file_id)

    def get(self, file_id: str, reference=None) -> StoredFile:
        if not FILE_ID_PATTERN.fullmatch(file_id):
            raise FileNotFoundError(file_id)
        directory = self.root / file_id
        metadata_path = directory / 'metadata.json'
        try:
            parsed_reference = S3ObjectReference.parse(reference) if reference else None
            if parsed_reference is not None:
                expected_prefix = self._object_key(file_id, '')
                if (
                    parsed_reference.bucket != self.bucket
                    or not parsed_reference.key.startswith(f'{expected_prefix}/')
                ):
                    raise StorageIntegrityError('storage reference does not match the requested file')
                storage_key = parsed_reference.key
                filename = self._safe_filename(Path(storage_key).name)
                version_id = parsed_reference.version_id
                sha256 = parsed_reference.sha256
                size_bytes = parsed_reference.size_bytes
                head = self.client.head_object(**self._bucket_request(
                    Key=storage_key,
                    VersionId=version_id,
                ))
                content_type = str(head.get('ContentType') or 'application/octet-stream')
                security_status = (head.get('Metadata') or {}).get('security-status')
                security = {'status': security_status} if security_status else None
            elif metadata_path.is_file():
                metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                storage_key = str(metadata['storage_key'])
                filename = self._safe_filename(metadata['filename'])
                version_id = str(metadata['version_id'])
                sha256 = str(metadata['sha256']).lower()
                size_bytes = int(metadata['size_bytes'])
                content_type = str(metadata.get('content_type') or 'application/octet-stream')
                security = metadata.get('security')
            else:
                storage_key = self._find_remote_object(file_id)
                filename = self._safe_filename(Path(storage_key).name)
                head = self.client.head_object(**self._bucket_request(Key=storage_key))
                version_id = str(head.get('VersionId') or '')
                sha256 = str((head.get('Metadata') or {}).get('sha256') or '').lower()
                size_bytes = int(head.get('ContentLength') or 0)
                content_type = str(head.get('ContentType') or 'application/octet-stream')
                security_status = (head.get('Metadata') or {}).get('security-status')
                security = {'status': security_status} if security_status else None
                if not version_id or version_id == 'null' or not SHA256_PATTERN.fullmatch(sha256):
                    raise StorageIntegrityError('remote object lacks version or checksum metadata')
                directory.mkdir(parents=True, exist_ok=True)
                metadata_path.write_text(json.dumps({
                    'file_id': file_id,
                    'filename': filename,
                    'content_type': content_type,
                    'size_bytes': size_bytes,
                    'sha256': sha256,
                    'storage_key': storage_key,
                    'version_id': version_id,
                    'security': security,
                }, ensure_ascii=False), encoding='utf-8')
            reference = S3ObjectReference(
                self.bucket,
                storage_key,
                version_id,
                sha256,
                size_bytes,
            )
            download_root = self.root / '.downloads' / uuid4().hex
            target = Path(materialize_storage_references(
                reference.serialize(),
                download_root,
                client=self.client,
                configured_bucket=self.bucket,
                configured_prefix=self.prefix,
                expected_owner=self.expected_bucket_owner or '',
            ))
            stored = StoredFile(
                file_id=file_id,
                filename=filename,
                content_type=content_type,
                size_bytes=size_bytes,
                sha256=sha256,
                path=target,
                storage_key=storage_key,
                version_id=version_id,
                security=security,
            )
            return stored
        except Exception:
            if 'download_root' in locals():
                shutil.rmtree(download_root, ignore_errors=True)
            raise

    def payload(self, stored, project_root, download_url):
        payload = super().payload(stored, project_root, download_url)
        reference = S3ObjectReference(
            self.bucket,
            stored.storage_key,
            stored.version_id,
            stored.sha256,
            stored.size_bytes,
        )
        payload['path'] = reference.serialize()
        payload['download_url'] = (
            f'{download_url}?{urlencode({"storage_reference": reference.serialize()})}'
        )
        return payload

    def release(self, stored):
        downloads_root = (self.root / '.downloads').resolve()
        path = stored.path.resolve()
        if downloads_root in path.parents:
            relative = path.relative_to(downloads_root)
            if relative.parts:
                shutil.rmtree(downloads_root / relative.parts[0], ignore_errors=True)

    async def discard(self, stored):
        try:
            if stored.storage_key:
                request = self._bucket_request(Key=stored.storage_key)
                if stored.version_id:
                    request['VersionId'] = stored.version_id
                await asyncio.to_thread(self.client.delete_object, **request)
        finally:
            await super().discard(stored)

    async def aget(self, file_id: str, reference=None) -> StoredFile:
        return await asyncio.to_thread(self.get, file_id, reference)
