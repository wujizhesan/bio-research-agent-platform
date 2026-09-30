"""Durable publication for generated job artifacts."""

from contextlib import contextmanager, suppress
from dataclasses import dataclass
import gzip
import hashlib
import inspect
import mimetypes
import os
from pathlib import Path
import re
import shutil
import stat
import tarfile

try:
    from .storage_workspace import S3ObjectReference, StorageIntegrityError
except ImportError:
    from storage_workspace import S3ObjectReference, StorageIntegrityError


CHUNK_SIZE = 1024 * 1024
EXECUTION_RESULT_SCHEMA = 'bioagent.execution-result.v1'
_SAFE_SEGMENT = re.compile(r'[^A-Za-z0-9._-]+')


def _safe_segment(value, fallback):
    selected = _SAFE_SEGMENT.sub('_', str(value)).strip(' ._')
    return (selected or fallback)[:180]


def _remove(path):
    path = Path(path)
    try:
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)
    except OSError:
        return


def _stat_fingerprint(info, renamed=False):
    # Renaming the published root changes ctime without changing its contents.
    return (
        info.st_mode, info.st_dev, info.st_ino, info.st_nlink,
        info.st_size, info.st_mtime_ns,
        None if renamed else info.st_ctime_ns,
    )


def _check_stat(actual, expected, renamed=False):
    if _stat_fingerprint(actual, renamed) != _stat_fingerprint(expected, renamed):
        raise StorageIntegrityError('artifact changed during publication')


def _checked_stat(path, expected, renamed=False):
    try:
        actual = path.lstat()
    except OSError as exc:
        raise StorageIntegrityError('artifact changed during publication') from exc
    _check_stat(actual, expected, renamed)


@contextmanager
def _verified_file(path, expected=None):
    if expected is not None:
        _checked_stat(path, expected)
    with Path(path).open('rb') as handle:
        if expected is not None:
            _check_stat(os.fstat(handle.fileno()), expected)
        yield handle
        if expected is not None:
            _check_stat(os.fstat(handle.fileno()), expected)


@dataclass(frozen=True)
class DirectoryEntry:
    relative: Path
    info: os.stat_result


@dataclass(frozen=True)
class DirectoryManifest:
    root: Path
    root_info: os.stat_result
    entries: tuple[DirectoryEntry, ...]

    @classmethod
    def capture(cls, root):
        root = Path(root)
        root_info = root.lstat()
        if stat.S_ISLNK(root_info.st_mode):
            raise StorageIntegrityError('artifact publication rejects symbolic links')
        if not stat.S_ISDIR(root_info.st_mode):
            raise StorageIntegrityError('artifact directory is unavailable')
        entries = []
        root_parts = len(root.parts)
        for path in sorted(root.rglob('*')):
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise StorageIntegrityError('artifact publication rejects symbolic links')
            entries.append(DirectoryEntry(Path(*path.parts[root_parts:]), info))
        return cls(root, root_info, tuple(entries))

    @property
    def has_files(self):
        return any(stat.S_ISREG(entry.info.st_mode) for entry in self.entries)

    def reservation_size(self, archive=False):
        size = sum(
            entry.info.st_size for entry in self.entries
            if stat.S_ISREG(entry.info.st_mode)
        )
        if archive:
            size += max(1024 * 1024, size // 20)
        return max(size, 1)

    def fingerprint(self):
        return (
            self.root, _stat_fingerprint(self.root_info),
            tuple((entry.relative, _stat_fingerprint(entry.info)) for entry in self.entries),
        )

    def matches(self, fingerprint):
        root, root_fingerprint, entries = fingerprint
        return (
            self.root == root
            and _stat_fingerprint(self.root_info) == root_fingerprint
            and len(self.entries) == len(entries)
            and all(
                entry.relative == relative
                and _stat_fingerprint(entry.info) == expected
                for entry, (relative, expected) in zip(self.entries, entries)
            )
        )

    def check_root(self, root):
        if self.root != Path(root):
            raise StorageIntegrityError('artifact manifest does not match directory')
        _checked_stat(Path(root), self.root_info)

    def check_directories(self, root, renamed=False):
        root = Path(root)
        _checked_stat(root, self.root_info, renamed)
        for entry in self.entries:
            if stat.S_ISDIR(entry.info.st_mode):
                _checked_stat(root / entry.relative, entry.info)


def _accepts_directory_manifest(method):
    try:
        parameters = inspect.signature(method).parameters.values()
    except (TypeError, ValueError):
        return False
    return any(
        parameter.kind == inspect.Parameter.VAR_KEYWORD
        or (
            parameter.name == 'directory_manifest'
            and parameter.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        )
        for parameter in parameters
    )


def _supports_directory_manifests(store):
    return (
        getattr(store, 'supports_directory_manifests', False) is True
        and _accepts_directory_manifest(store.plan)
        and _accepts_directory_manifest(store.publish)
    )


def _directory_plan_options(store, manifest):
    return (
        {'directory_manifest': manifest}
        if manifest is not None and _accepts_directory_manifest(store.plan) else {}
    )


def _sha256(path, expected=None):
    digest = hashlib.sha256()
    size = 0
    with _verified_file(Path(path), expected) as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _reservation_size(path, archive=False, directory_manifest=None):
    path = Path(path)
    if directory_manifest is not None:
        directory_manifest.check_root(path)
        return directory_manifest.reservation_size(archive)
    if path.is_file():
        return max(path.stat().st_size, 1)
    size = sum(
        entry.stat().st_size
        for entry in path.rglob('*')
        if entry.is_file() and not entry.is_symlink()
    )
    if archive:
        size += max(1024 * 1024, size // 20)
    return max(size, 1)


def _reject_symlinks(path):
    path = Path(path)
    candidates = [path]
    if path.is_dir():
        candidates.extend(path.rglob('*'))
    if any(candidate.is_symlink() for candidate in candidates):
        raise StorageIntegrityError('artifact publication rejects symbolic links')


class _ArchiveChecksumWriter:
    def __init__(self, handle):
        self.handle = handle
        self.digest = hashlib.sha256()
        self.size_bytes = 0

    def write(self, data):
        written = self.handle.write(data)
        if written != len(data):
            raise StorageIntegrityError('artifact archive write was incomplete')
        if written:
            self.digest.update(data)
            self.size_bytes += written
        return written

    def flush(self):
        self.handle.flush()


def _archive_directory(source, target, root_name, compresslevel=9, directory_manifest=None):
    source = Path(source)
    raw = target.open('wb')
    try:
        with raw:
            writer = _ArchiveChecksumWriter(raw)
            with gzip.GzipFile(
                fileobj=writer, mode='wb', filename='', mtime=0,
                compresslevel=compresslevel,
            ) as compressed:
                with tarfile.open(
                    fileobj=compressed,
                    mode='w',
                    format=tarfile.PAX_FORMAT,
                ) as archive:
                    entries = (
                        [(source, Path(), directory_manifest.root_info), *(
                            (source / item.relative, item.relative, item.info)
                            for item in directory_manifest.entries
                        )] if directory_manifest is not None else
                        [(entry, entry.relative_to(source), None) for entry in [source, *sorted(source.rglob('*'))]]
                    )
                    for entry, relative, expected in entries:
                        arcname = Path(root_name) / relative
                        is_directory = stat.S_ISDIR(expected.st_mode) if expected is not None else entry.is_dir()
                        is_file = stat.S_ISREG(expected.st_mode) if expected is not None else entry.is_file()
                        if expected is not None and not is_file:
                            _checked_stat(entry, expected)
                        if is_file:
                            with _verified_file(entry, expected) as handle:
                                info = archive.gettarinfo(
                                    str(entry), arcname.as_posix(),
                                    fileobj=handle if expected is not None else None,
                                )
                                _normalize_tarinfo(info, is_directory)
                                archive.addfile(info, handle)
                        else:
                            info = archive.gettarinfo(str(entry), arcname.as_posix())
                            _normalize_tarinfo(info, is_directory)
                            archive.addfile(info)
        if directory_manifest is not None:
            directory_manifest.check_directories(source)
    except Exception:
        with suppress(OSError):
            target.unlink(missing_ok=True)
        raise
    return writer.digest.hexdigest(), writer.size_bytes


def _normalize_tarinfo(info, is_directory):
    info.uid = 0
    info.gid = 0
    info.uname = ''
    info.gname = ''
    info.mtime = 0
    info.mode = 0o755 if is_directory else 0o644


def _artifact_id(context, parameter, index):
    identity = ':'.join((
        str(context.get('job_id') or ''),
        str(context.get('execution_key') or ''),
        str(parameter),
        str(index),
    ))
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()[:32]


def _publication_id(context, artifact_id):
    identity = ':'.join((
        str(context.get('job_id') or ''),
        str(context.get('execution_key') or ''),
        str(context.get('fencing_token') or ''),
        str(context.get('attempt') or ''),
        str(artifact_id),
    ))
    return hashlib.sha256(identity.encode('utf-8')).hexdigest()


@dataclass
class PublishedArtifactHandle:
    record: dict
    reference: str
    _rollback: object
    _finalize: object
    _closed: bool = False

    def rollback(self):
        if not self._closed:
            self._rollback()
            self._closed = True

    def finalize(self):
        if not self._closed:
            self._finalize()
            self._closed = True


class LocalArtifactStore:
    backend = 'local'
    supports_directory_manifests = True

    def plan(self, staged, target, parameter, kind, context, index, *, directory_manifest=None):
        if directory_manifest is None and Path(staged).is_dir():
            directory_manifest = DirectoryManifest.capture(staged)
        target = Path(target)
        artifact_id = _artifact_id(context, parameter, index)
        return {
            'publication_id': _publication_id(context, artifact_id),
            'artifact_id': artifact_id,
            'parameter': str(parameter),
            'kind': str(kind),
            'filename': target.name,
            'storage_backend': self.backend,
            'path': str(target),
            'reserved_bytes': _reservation_size(staged, directory_manifest=directory_manifest),
        }

    def publish(self, staged, target, parameter, kind, context, index, *, directory_manifest=None):
        staged = Path(staged)
        target = Path(target)
        if directory_manifest is None and staged.is_dir():
            directory_manifest = DirectoryManifest.capture(staged)
        if directory_manifest is not None:
            directory_manifest.check_root(staged)
        else:
            _reject_symlinks(staged)
        if target.exists():
            raise FileExistsError(f'artifact target already exists: {target.name}')
        plan = self.plan(
            staged, target, parameter, kind, context, index,
            **_directory_plan_options(self, directory_manifest),
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        try:
            if directory_manifest is not None:
                digest = hashlib.sha256()
                size = 0
                for entry in directory_manifest.entries:
                    if not stat.S_ISREG(entry.info.st_mode):
                        continue
                    relative = entry.relative.as_posix().encode('utf-8')
                    file_hash, file_size = _sha256(target / entry.relative, entry.info)
                    digest.update(relative)
                    digest.update(b'\0')
                    digest.update(file_hash.encode('ascii'))
                    size += file_size
                directory_manifest.check_directories(target, renamed=True)
                sha256 = digest.hexdigest()
                content_type = 'application/x-directory'
            else:
                sha256, size = _sha256(target)
                content_type = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
        except Exception:
            _remove(target)
            raise
        record = {
            **plan,
            'content_type': content_type,
            'size_bytes': size,
            'sha256': sha256,
            'storage_backend': self.backend,
            'path': str(target),
        }
        return PublishedArtifactHandle(
            record,
            str(target),
            lambda: _remove(target),
            lambda: None,
        )


class S3ArtifactStore:
    backend = 's3'
    supports_directory_manifests = True

    def __init__(
        self,
        bucket,
        prefix='bio-agent',
        endpoint_url=None,
        region_name=None,
        expected_bucket_owner=None,
        access_key_id=None,
        secret_access_key=None,
        session_token=None,
        client=None,
    ):
        if not str(bucket or '').strip():
            raise ValueError('S3_BUCKET is required for artifact publication')
        if client is None:
            try:
                import boto3
            except ImportError as exc:
                raise RuntimeError('boto3 is required for S3 artifact publication') from exc
            client = boto3.client(
                's3',
                endpoint_url=endpoint_url or None,
                region_name=region_name or None,
                aws_access_key_id=access_key_id or None,
                aws_secret_access_key=secret_access_key or None,
                aws_session_token=session_token or None,
            )
        self.bucket = str(bucket).strip()
        self.prefix = str(prefix or '').strip('/')
        self.expected_bucket_owner = str(expected_bucket_owner or '').strip() or None
        self.client = client

    def _request(self, **values):
        request = {'Bucket': self.bucket, **values}
        if self.expected_bucket_owner:
            request['ExpectedBucketOwner'] = self.expected_bucket_owner
        return request

    def plan(self, staged, target, parameter, kind, context, index, *, directory_manifest=None):
        staged = Path(staged)
        target = Path(target)
        if directory_manifest is None and staged.is_dir():
            directory_manifest = DirectoryManifest.capture(staged)
        artifact_id = _artifact_id(context, parameter, index)
        publication_id = _publication_id(context, artifact_id)
        filename = _safe_segment(target.name, f'artifact-{index}')
        if str(kind) == 'directory' or staged.is_dir():
            filename = f'{filename}.tar.gz'
        parts = [item for item in (
            self.prefix,
            'artifacts',
            _safe_segment(context.get('project_id'), 'system'),
            _safe_segment(context.get('job_id'), 'job'),
            artifact_id,
            _safe_segment(
                context.get('fencing_token') or context.get('attempt'),
                'publication',
            ),
            filename,
        ) if item]
        return {
            'publication_id': publication_id,
            'artifact_id': artifact_id,
            'parameter': str(parameter),
            'kind': str(kind),
            'filename': filename,
            'storage_backend': self.backend,
            'storage_key': '/'.join(parts),
            'reserved_bytes': _reservation_size(
                staged,
                archive=str(kind) == 'directory' or staged.is_dir(),
                directory_manifest=directory_manifest,
            ),
        }

    def publish(self, staged, target, parameter, kind, context, index, *, directory_manifest=None):
        staged = Path(staged)
        target = Path(target)
        if directory_manifest is None and staged.is_dir():
            directory_manifest = DirectoryManifest.capture(staged)
        if directory_manifest is not None:
            directory_manifest.check_root(staged)
        else:
            _reject_symlinks(staged)
        plan = self.plan(
            staged, target, parameter, kind, context, index,
            **_directory_plan_options(self, directory_manifest),
        )
        artifact_id = plan['artifact_id']
        packaged = None
        upload_path = staged
        filename = plan['filename']
        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        if staged.is_dir():
            packaged = staged.parent / f'.{artifact_id}.tar.gz'
            sha256, size = _archive_directory(
                staged, packaged, target.name, compresslevel=1,
                directory_manifest=directory_manifest,
            )
            upload_path = packaged
            content_type = 'application/gzip'
        else:
            sha256, size = _sha256(upload_path)
        if size < 1:
            if packaged is not None:
                _remove(packaged)
            raise StorageIntegrityError('S3 artifact publication rejects empty artifacts')
        key = plan['storage_key']
        extra = {
            'ContentType': content_type,
            'Metadata': {
                'artifact-id': artifact_id,
                'job-id': str(context.get('job_id') or ''),
                'sha256': sha256,
                'kind': str(kind),
            },
            **(
                {'ExpectedBucketOwner': self.expected_bucket_owner}
                if self.expected_bucket_owner else {}
            ),
        }
        try:
            self.client.upload_file(
                str(upload_path),
                self.bucket,
                key,
                ExtraArgs=extra,
            )
            head = self.client.head_object(**self._request(Key=key))
            version_id = str(head.get('VersionId') or '')
            remote_sha256 = str(
                (head.get('Metadata') or {}).get('sha256') or ''
            ).lower()
            if not version_id or version_id == 'null':
                raise StorageIntegrityError(
                    'S3 artifact publication requires bucket versioning'
                )
            if remote_sha256 != sha256:
                raise StorageIntegrityError('S3 artifact checksum verification failed')
            if int(head.get('ContentLength') or -1) != size:
                raise StorageIntegrityError('S3 artifact size verification failed')
        except Exception:
            if packaged is not None:
                packaged.unlink(missing_ok=True)
            raise
        reference = S3ObjectReference(
            self.bucket,
            key,
            version_id,
            sha256,
            size,
        ).serialize()
        record = {
            **plan,
            'content_type': content_type,
            'size_bytes': size,
            'sha256': sha256,
            'storage_backend': self.backend,
            'storage_key': key,
            'version_id': version_id,
            'reference': reference,
        }

        def cleanup_local():
            _remove(staged)
            if packaged is not None:
                _remove(packaged)

        def rollback():
            try:
                try:
                    self.client.delete_object(**self._request(
                        Key=key,
                        VersionId=version_id,
                    ))
                except Exception:
                    pass
            finally:
                cleanup_local()

        return PublishedArtifactHandle(record, reference, rollback, cleanup_local)


def artifact_store_from_settings(settings, client=None):
    if settings.storage_backend == 's3':
        return S3ArtifactStore(
            settings.s3_bucket,
            prefix=settings.s3_prefix,
            endpoint_url=settings.s3_endpoint_url or None,
            region_name=settings.s3_region or None,
            expected_bucket_owner=settings.s3_expected_bucket_owner or None,
            access_key_id=settings.aws_access_key_id or None,
            secret_access_key=settings.aws_secret_access_key or None,
            session_token=settings.aws_session_token or None,
            client=client,
        )
    return LocalArtifactStore()


def pack_execution_result(result, artifacts):
    return {
        'schema': EXECUTION_RESULT_SCHEMA,
        'result': result,
        'artifacts': [dict(item) for item in artifacts],
    }


def unpack_execution_result(value):
    if isinstance(value, dict) and value.get('schema') == EXECUTION_RESULT_SCHEMA:
        artifacts = value.get('artifacts')
        return value.get('result'), (
            [dict(item) for item in artifacts if isinstance(item, dict)]
            if isinstance(artifacts, list) else []
        )
    return value, []
