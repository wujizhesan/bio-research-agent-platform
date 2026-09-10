"""Secure local storage for uploaded research inputs."""

import asyncio
from dataclasses import dataclass
from dataclasses import replace
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
from typing import Any
from uuid import uuid4


DEFAULT_ALLOWED_EXTENSIONS = frozenset({
    '.bed', '.csv', '.fa', '.fasta', '.fastq', '.fq', '.fna', '.gff', '.gff3',
    '.gtf', '.html', '.htm', '.json', '.md', '.tsv', '.txt', '.vcf', '.yaml', '.yml',
})
FILE_ID_PATTERN = re.compile(r'^[a-f0-9]{32}$')
CHUNK_SIZE = 1024 * 1024
SNIFF_BYTES = 64 * 1024
METADATA_RESERVE_BYTES = 1024
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


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    path: Path
    storage_key: str | None = None
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

    def _storage_usage(self) -> int:
        total = 0
        for directory, _names, filenames in os.walk(self.root):
            for filename in filenames:
                try:
                    total += (Path(directory) / filename).stat().st_size
                except OSError:
                    continue
        return total

    @staticmethod
    def _validate_text(content: bytes) -> str:
        if any(content.startswith(signature) for signature in ARCHIVE_SIGNATURES):
            raise ValueError('archive uploads are not allowed')
        if any(content.startswith(signature) for signature in EXECUTABLE_SIGNATURES):
            raise ValueError('executable content is not allowed')
        if any(marker in content for marker in MALWARE_MARKERS):
            raise ValueError('known malicious test signature detected')
        if b'\x00' in content:
            raise ValueError('binary content does not match the file extension')
        try:
            return content.decode('utf-8-sig')
        except UnicodeDecodeError as exc:
            raise ValueError('uploaded research files must be UTF-8 text') from exc

    def _inspect_gzip(self, target: Path, compressed_size: int) -> str:
        total = 0
        sample = bytearray()
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
                    if len(sample) < SNIFF_BYTES:
                        sample.extend(chunk[:SNIFF_BYTES - len(sample)])
        except (gzip.BadGzipFile, EOFError, OSError) as exc:
            raise ValueError('invalid gzip content') from exc
        text = self._validate_text(bytes(sample))
        if not text.lstrip().startswith(('##fileformat=VCF', '#CHROM')):
            raise ValueError('gzip content does not match .vcf.gz')
        return 'application/gzip'

    def _inspect_content(self, target: Path, filename: str, size_bytes: int) -> str:
        with target.open('rb') as handle:
            sample = handle.read(SNIFF_BYTES)
        if filename.lower().endswith('.vcf.gz'):
            if not sample.startswith(b'\x1f\x8b'):
                raise ValueError('content does not match .vcf.gz')
            return self._inspect_gzip(target, size_bytes)
        if sample.startswith(b'\x1f\x8b'):
            raise ValueError('compressed content does not match the file extension')
        text = self._validate_text(sample)
        extension = Path(filename).suffix.lower()
        if extension == '.vcf' and not text.lstrip().startswith(
            ('##fileformat=VCF', '#CHROM')
        ):
            raise ValueError('content does not match .vcf')
        return CONTENT_TYPES.get(extension, 'text/plain')

    async def save(self, upload: Any) -> StoredFile:
        filename = self._safe_filename(getattr(upload, 'filename', None))
        extension = Path(filename).suffix.lower()
        is_vcf_gzip = filename.lower().endswith('.vcf.gz')
        if extension not in self.allowed_extensions and not is_vcf_gzip:
            allowed = ', '.join(sorted(self.allowed_extensions | {'.vcf.gz'}))
            raise ValueError(f'unsupported file type: {extension or "none"}; allowed: {allowed}')

        async with self._quota_lock:
            current_usage = self._storage_usage()
            file_id = uuid4().hex
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
                    'security': stored.security,
                }, ensure_ascii=False), encoding='utf-8')
                return stored
            except Exception:
                if directory.exists():
                    for child in directory.iterdir():
                        child.unlink(missing_ok=True)
                    directory.rmdir()
                raise

    def get(self, file_id: str) -> StoredFile:
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
                security=payload.get('security'),
            )
            path = self._resolve_file_path(stored)
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise FileNotFoundError(file_id) from exc
        if not path.is_file():
            raise FileNotFoundError(file_id)
        return stored

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
        access_key_id: str | None = None,
        secret_access_key: str | None = None,
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
        self.client = client or boto3.client(
            's3',
            endpoint_url=endpoint_url or None,
            region_name=region_name or None,
            aws_access_key_id=access_key_id or os.environ.get('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=secret_access_key or os.environ.get('AWS_SECRET_ACCESS_KEY'),
        )

    def _object_key(self, file_id, filename):
        parts = [item for item in (self.prefix, file_id, filename) if item]
        return '/'.join(parts)

    async def save(self, upload: Any) -> StoredFile:
        stored = await super().save(upload)
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
                },
            )
        except Exception:
            for child in stored.path.parent.iterdir():
                child.unlink(missing_ok=True)
            stored.path.parent.rmdir()
            raise
        metadata_path = stored.path.parent / 'metadata.json'
        metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
        metadata['storage_key'] = storage_key
        metadata_path.write_text(json.dumps(metadata, ensure_ascii=False), encoding='utf-8')
        return replace(stored, storage_key=storage_key)

    def _find_remote_object(self, file_id):
        response = self.client.list_objects_v2(
            Bucket=self.bucket,
            Prefix=f'{self.prefix}/{file_id}/' if self.prefix else f'{file_id}/',
        )
        objects = response.get('Contents') or []
        for item in objects:
            key = item.get('Key')
            if key and not key.endswith('/'):
                return key
        raise FileNotFoundError(file_id)

    def get(self, file_id: str) -> StoredFile:
        try:
            return super().get(file_id)
        except FileNotFoundError:
            pass
        storage_key = self._find_remote_object(file_id)
        filename = self._safe_filename(Path(storage_key).name)
        directory = self.root / file_id
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / filename
        try:
            head = self.client.head_object(Bucket=self.bucket, Key=storage_key)
            self.client.download_file(self.bucket, storage_key, str(target))
            digest = hashlib.sha256()
            with target.open('rb') as source:
                for chunk in iter(lambda: source.read(CHUNK_SIZE), b''):
                    digest.update(chunk)
            stored = StoredFile(
                file_id=file_id,
                filename=filename,
                content_type=str(head.get('ContentType') or 'application/octet-stream'),
                size_bytes=int(head.get('ContentLength') or target.stat().st_size),
                sha256=str((head.get('Metadata') or {}).get('sha256') or digest.hexdigest()),
                path=target,
                storage_key=storage_key,
                security=(head.get('Metadata') or {}).get('security-status') and {
                    'status': (head.get('Metadata') or {}).get('security-status')
                },
            )
            (directory / 'metadata.json').write_text(json.dumps({
                'file_id': stored.file_id,
                'filename': stored.filename,
                'content_type': stored.content_type,
                'size_bytes': stored.size_bytes,
                'sha256': stored.sha256,
                'storage_key': stored.storage_key,
                'security': stored.security,
            }, ensure_ascii=False), encoding='utf-8')
            return stored
        except Exception:
            if directory.exists():
                for child in directory.iterdir():
                    child.unlink(missing_ok=True)
                directory.rmdir()
            raise

    async def aget(self, file_id: str) -> StoredFile:
        return await asyncio.to_thread(self.get, file_id)
