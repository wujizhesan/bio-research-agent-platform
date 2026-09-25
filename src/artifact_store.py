"""Durable publication for generated job artifacts."""

from dataclasses import dataclass
import gzip
import hashlib
import mimetypes
import os
from pathlib import Path
import re
import shutil
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


def _sha256(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
            size += len(chunk)
            digest.update(chunk)
    return digest.hexdigest(), size


def _reservation_size(path, archive=False):
    path = Path(path)
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


def _archive_directory(source, target, root_name):
    source = Path(source)
    with target.open('wb') as raw:
        with gzip.GzipFile(fileobj=raw, mode='wb', filename='', mtime=0) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode='w',
                format=tarfile.PAX_FORMAT,
            ) as archive:
                entries = [source, *sorted(source.rglob('*'))]
                for entry in entries:
                    relative = entry.relative_to(source)
                    arcname = Path(root_name) / relative
                    info = archive.gettarinfo(str(entry), arcname.as_posix())
                    info.uid = 0
                    info.gid = 0
                    info.uname = ''
                    info.gname = ''
                    info.mtime = 0
                    info.mode = 0o755 if entry.is_dir() else 0o644
                    if entry.is_file():
                        with entry.open('rb') as handle:
                            archive.addfile(info, handle)
                    else:
                        archive.addfile(info)


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

    def plan(self, staged, target, parameter, kind, context, index):
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
            'reserved_bytes': _reservation_size(staged),
        }

    def publish(self, staged, target, parameter, kind, context, index):
        staged = Path(staged)
        target = Path(target)
        _reject_symlinks(staged)
        if target.exists():
            raise FileExistsError(f'artifact target already exists: {target.name}')
        plan = self.plan(staged, target, parameter, kind, context, index)
        target.parent.mkdir(parents=True, exist_ok=True)
        os.replace(staged, target)
        if target.is_dir():
            digest = hashlib.sha256()
            size = 0
            for entry in sorted(target.rglob('*')):
                if not entry.is_file():
                    continue
                relative = entry.relative_to(target).as_posix().encode('utf-8')
                file_hash, file_size = _sha256(entry)
                digest.update(relative)
                digest.update(b'\0')
                digest.update(file_hash.encode('ascii'))
                size += file_size
            sha256 = digest.hexdigest()
            content_type = 'application/x-directory'
        else:
            sha256, size = _sha256(target)
            content_type = mimetypes.guess_type(target.name)[0] or 'application/octet-stream'
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

    def plan(self, staged, target, parameter, kind, context, index):
        staged = Path(staged)
        target = Path(target)
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
            ),
        }

    def publish(self, staged, target, parameter, kind, context, index):
        staged = Path(staged)
        target = Path(target)
        _reject_symlinks(staged)
        plan = self.plan(staged, target, parameter, kind, context, index)
        artifact_id = plan['artifact_id']
        packaged = None
        upload_path = staged
        filename = plan['filename']
        content_type = mimetypes.guess_type(filename)[0] or 'application/octet-stream'
        if staged.is_dir():
            packaged = staged.parent / f'.{artifact_id}.tar.gz'
            _archive_directory(staged, packaged, target.name)
            upload_path = packaged
            content_type = 'application/gzip'
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
