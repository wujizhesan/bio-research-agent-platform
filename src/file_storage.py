"""Secure local storage for uploaded research inputs."""

from dataclasses import dataclass
import hashlib
import json
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


@dataclass(frozen=True)
class StoredFile:
    file_id: str
    filename: str
    content_type: str
    size_bytes: int
    sha256: str
    path: Path


class LocalFileStorage:
    def __init__(
        self,
        root: str | Path,
        max_bytes: int = 50 * 1024 * 1024,
        allowed_extensions: frozenset[str] = DEFAULT_ALLOWED_EXTENSIONS,
    ):
        if max_bytes < 1:
            raise ValueError('max_bytes must be positive')
        self.root = Path(root).resolve()
        self.max_bytes = max_bytes
        self.allowed_extensions = frozenset(item.lower() for item in allowed_extensions)
        self.root.mkdir(parents=True, exist_ok=True)

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

    async def save(self, upload: Any) -> StoredFile:
        filename = self._safe_filename(getattr(upload, 'filename', None))
        extension = Path(filename).suffix.lower()
        is_vcf_gzip = filename.lower().endswith('.vcf.gz')
        if extension not in self.allowed_extensions and not is_vcf_gzip:
            allowed = ', '.join(sorted(self.allowed_extensions | {'.vcf.gz'}))
            raise ValueError(f'unsupported file type: {extension or "none"}; allowed: {allowed}')

        file_id = uuid4().hex
        directory = self.root / file_id
        directory.mkdir(parents=False, exist_ok=False)
        target = directory / filename
        metadata_path = directory / 'metadata.json'
        size_bytes = 0
        digest = hashlib.sha256()

        try:
            with target.open('wb') as output:
                while True:
                    chunk = await upload.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    size_bytes += len(chunk)
                    if size_bytes > self.max_bytes:
                        raise ValueError(f'file exceeds maximum size of {self.max_bytes} bytes')
                    output.write(chunk)
                    digest.update(chunk)
            if size_bytes == 0:
                raise ValueError('empty files are not allowed')
            stored = StoredFile(
                file_id=file_id,
                filename=filename,
                content_type=getattr(upload, 'content_type', None) or 'application/octet-stream',
                size_bytes=size_bytes,
                sha256=digest.hexdigest(),
                path=target,
            )
            metadata_path.write_text(json.dumps({
                'file_id': stored.file_id,
                'filename': stored.filename,
                'content_type': stored.content_type,
                'size_bytes': stored.size_bytes,
                'sha256': stored.sha256,
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
        }
