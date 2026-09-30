import argparse
from contextlib import suppress
import gzip
import hashlib
from io import BytesIO
import json
import os
from pathlib import Path
import platform
from statistics import median
import tarfile
import tempfile
from time import perf_counter
from unittest.mock import patch
import mimetypes

from src.artifact_store import (
    CHUNK_SIZE, LocalArtifactStore as ManifestLocalStore,
    S3ArtifactStore as ManifestS3Store, PublishedArtifactHandle,
    S3ObjectReference, StorageIntegrityError, _ArchiveChecksumWriter,
    _artifact_id, _publication_id, _safe_segment, _remove,
)
from src.execution_semantics import (
    ArtifactCommit, ArtifactTransaction, StagedArtifact, _replace_paths,
)


BASELINE_SOURCE_COMMIT = 'aaf2498e1d67ec68bc559e353c895d4ca286de84'


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

def _archive_directory(source, target, root_name, compresslevel=9):
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
    except Exception:
        with suppress(OSError):
            target.unlink(missing_ok=True)
        raise
    return writer.digest.hexdigest(), writer.size_bytes


class LegacyLocalStore(ManifestLocalStore):
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


class LegacyS3Store(ManifestS3Store):
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
            sha256, size = _archive_directory(
                staged, packaged, target.name, compresslevel=1,
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


class LegacyTransaction(ArtifactTransaction):
    def _validated_artifacts(self):
        selected = []
        for artifact in self.artifacts:
            if not artifact.staged.exists():
                if artifact.required:
                    raise RuntimeError(
                        f'required artifact was not generated: {artifact.parameter}'
                    )
                continue
            if artifact.staged.is_symlink():
                raise RuntimeError(
                    f'artifact cannot be a symbolic link: {artifact.parameter}'
                )
            if artifact.directory:
                if not artifact.staged.is_dir():
                    raise RuntimeError(
                        f'artifact must be a directory: {artifact.parameter}'
                    )
                files = [
                    entry for entry in artifact.staged.rglob('*')
                    if entry.is_file() and not entry.is_symlink()
                ]
                if not files:
                    if artifact.required:
                        raise RuntimeError(
                            f'required artifact directory is empty: {artifact.parameter}'
                        )
                    continue
            else:
                if not artifact.staged.is_file():
                    raise RuntimeError(
                        f'artifact must be a file: {artifact.parameter}'
                    )
                if artifact.staged.stat().st_size < 1:
                    raise RuntimeError(
                        f'artifact file is empty: {artifact.parameter}'
                    )
            selected.append(artifact)
        return tuple(selected)

    def plan(self, store, context):
        return tuple(
            store.plan(
                artifact.staged,
                artifact.target,
                artifact.parameter,
                'directory' if artifact.directory else 'file',
                context,
                artifact.ordinal,
            )
            for artifact in self._validated_artifacts()
        )

    def publish(self, result, store, context):
        handles = []
        replacements = []
        try:
            for artifact in self._validated_artifacts():
                handle = store.publish(
                    artifact.staged,
                    artifact.target,
                    artifact.parameter,
                    'directory' if artifact.directory else 'file',
                    context,
                    artifact.ordinal,
                )
                handles.append(handle)
                replacements.append((
                    str(artifact.staged),
                    str(handle.reference),
                ))
        except Exception:
            for handle in reversed(handles):
                handle.rollback()
            self.rollback()
            raise
        self._finished = True
        return ArtifactCommit(_replace_paths(result, replacements), handles)


class MemoryS3Client:
    def __init__(self):
        self.objects = {}

    def upload_file(self, filename, bucket, key, ExtraArgs):
        self.objects[(bucket, key)] = {
            'body': Path(filename).read_bytes(),
            'metadata': ExtraArgs['Metadata'],
        }

    def head_object(self, Bucket, Key, **kwargs):
        item = self.objects[(Bucket, Key)]
        return {
            'VersionId': 'benchmark-version', 'ContentLength': len(item['body']),
            'Metadata': item['metadata'],
        }

    def delete_object(self, Bucket, Key, VersionId, **kwargs):
        assert VersionId == 'benchmark-version'
        self.objects.pop((Bucket, Key))


def make_dataset(staged, file_count):
    staged.mkdir()
    (staged / 'empty-results').mkdir()
    expected = {}
    for index in range(file_count):
        directory = staged / f'group-{index % 8}'
        name = f'part-{index:05d}.tsv'
        if index == file_count - 1:
            directory = directory / ('nested-' + 'x' * 120)
            name = '科研结果.tsv'
        directory.mkdir(parents=True, exist_ok=True)
        payload = b'' if index == 0 else (f'chr1\t{index}\tGene-{index}\t0.125\n'.encode() * 32)[:512]
        path = directory / name
        path.write_bytes(payload)
        expected[path.relative_to(staged).as_posix()] = (hashlib.sha256(payload).hexdigest(), len(payload))
    directories = {'.', *(entry.relative_to(staged).as_posix() for entry in staged.rglob('*') if entry.is_dir())}
    return expected, directories


def verify_local(target, expected, directories, record):
    actual = {
        entry.relative_to(target).as_posix(): _sha256(entry)
        for entry in target.rglob('*') if entry.is_file()
    }
    assert actual == expected
    assert {'.', *(entry.relative_to(target).as_posix() for entry in target.rglob('*') if entry.is_dir())} == directories
    digest = hashlib.sha256()
    for relative in sorted(expected, key=Path):
        file_hash, file_size = expected[relative]
        digest.update(relative.encode('utf-8'))
        digest.update(b'\0')
        digest.update(file_hash.encode('ascii'))
    assert record['sha256'] == digest.hexdigest()
    assert record['size_bytes'] == sum(size for _, size in expected.values())


def verify_s3(body, expected, directories, record):
    assert record['sha256'] == hashlib.sha256(body).hexdigest()
    assert record['size_bytes'] == len(body)
    actual, actual_directories = {}, set()
    with tarfile.open(fileobj=BytesIO(body), mode='r:gz') as archive:
        for member in archive:
            relative = Path(member.name).relative_to('dataset').as_posix()
            assert member.uid == member.gid == member.mtime == 0
            assert member.uname == member.gname == ''
            assert member.mode == (0o755 if member.isdir() else 0o644)
            if member.isdir():
                actual_directories.add(relative)
            else:
                payload = archive.extractfile(member).read()
                actual[relative] = (hashlib.sha256(payload).hexdigest(), len(payload))
    assert actual == expected
    assert actual_directories == directories


def measure(root, backend, implementation, file_count, count_walks=False):
    staged, target = root / 'staged', root / 'dataset'
    expected, directories = make_dataset(staged, file_count)
    client = MemoryS3Client()
    if implementation == 'baseline':
        store = LegacyLocalStore() if backend == 'local' else LegacyS3Store('benchmark', client=client)
        transaction_class = LegacyTransaction
    else:
        store = ManifestLocalStore() if backend == 'local' else ManifestS3Store('benchmark', client=client)
        transaction_class = ArtifactTransaction
    transaction = transaction_class({}, [StagedArtifact(0, 'output_dir', target, staged, True, True)])
    context = {'job_id': 'benchmark-job', 'execution_key': 'benchmark-execution', 'project_id': 'benchmark-project'}
    counters = {'rglob_calls': 0, 'entries_visited': 0}
    original_rglob = Path.rglob

    def tracked(path, pattern):
        counters['rglob_calls'] += 1
        for entry in original_rglob(path, pattern):
            counters['entries_visited'] += 1
            yield entry

    def publish():
        plans = transaction.plan(store, context)
        publication = transaction.publish({'output_dir': str(staged)}, store, context)
        return plans, publication

    started = perf_counter()
    if count_walks:
        with patch.object(Path, 'rglob', tracked):
            plans, publication = publish()
    else:
        plans, publication = publish()
    elapsed = perf_counter() - started
    assert len(plans) == len(publication.artifacts) == 1
    record = publication.artifacts[0]
    assert plans[0]['reserved_bytes'] == record['reserved_bytes']
    assert record['size_bytes'] <= record['reserved_bytes']
    body = None
    if backend == 'local':
        verify_local(target, expected, directories, record)
        assert publication.result['output_dir'] == str(target)
    else:
        body = client.objects[('benchmark', record['storage_key'])]['body']
        verify_s3(body, expected, directories, record)
        reference = S3ObjectReference.parse(publication.result['output_dir'])
        assert reference.sha256 == record['sha256'] and reference.size_bytes == record['size_bytes']
    assert staged.parent.resolve() == target.parent.resolve() == root.resolve()
    publication.rollback()
    assert not list(root.iterdir()) and not client.objects
    return {
        'elapsed_seconds': elapsed,
        'source_bytes': sum(size for _, size in expected.values()),
        'record': record,
        'body': body,
        'walk_counts': counters if count_walks else None,
    }


def benchmark(samples, file_counts, workspace_root):
    workspace = Path(workspace_root or tempfile.gettempdir()).resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    rows = []
    with tempfile.TemporaryDirectory(prefix='directory-publication-', dir=workspace) as raw:
        root = Path(raw).resolve()
        assert root.parent == workspace
        for backend in ('local', 's3'):
            for file_count in file_counts:
                pairs, walk_counts = [], {}
                for sample in range(-1, samples):
                    order = ['baseline', 'manifest']
                    if sample % 2:
                        order.reverse()
                    pair = {'sample': sample, 'order': order}
                    for name in order:
                        result = measure(root, backend, name, file_count, count_walks=sample == -1)
                        if sample == -1:
                            walk_counts[name] = result.pop('walk_counts')
                        else:
                            assert result.pop('walk_counts') is None
                        pair[name] = result
                    assert pair['baseline'].pop('record') == pair['manifest'].pop('record')
                    assert pair['baseline'].pop('body') == pair['manifest'].pop('body')
                    if sample >= 0:
                        pairs.append(pair)
                assert walk_counts['baseline']['rglob_calls'] == 6
                assert walk_counts['manifest']['rglob_calls'] == 2
                before = median(pair['baseline']['elapsed_seconds'] for pair in pairs)
                after = median(pair['manifest']['elapsed_seconds'] for pair in pairs)
                rows.append({
                    'backend': backend,
                    'file_count': file_count,
                    'source_bytes': pairs[0]['baseline']['source_bytes'],
                    'paired_samples': samples,
                    'baseline_median_seconds': before,
                    'manifest_median_seconds': after,
                    'elapsed_reduction_percent': (before - after) / before * 100,
                    'manifest_faster_count': sum(pair['manifest']['elapsed_seconds'] < pair['baseline']['elapsed_seconds'] for pair in pairs),
                    'walk_counts_from_instrumented_warmup': walk_counts,
                    'pairs': pairs,
                })
    return {
        'baseline_source_commit': BASELINE_SOURCE_COMMIT,
        'platform': platform.platform(),
        'python_version': platform.python_version(),
        'scope': 'Complete ArtifactTransaction.plan and publish with nested synthetic small files, empty files/directories and Unicode/PAX paths; local publication and an in-memory S3 upload/head stub; excludes Redis/DB, HTTP and real S3 network',
        'timer_excludes': 'fixture generation, output verification and rollback cleanup',
        'timed_samples_instrumented': False,
        'warmup_pairs_per_scenario': 1,
        'records_and_archive_bytes_equal': True,
        's3_compresslevel': 1,
        'rows': rows,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--samples', type=int, default=8)
    parser.add_argument('--file-counts', default='32,512,2048')
    parser.add_argument('--workspace-root', type=Path)
    args = parser.parse_args()
    try:
        file_counts = [int(item) for item in args.file_counts.split(',')]
    except ValueError:
        parser.error('file counts must contain positive integers separated by commas')
    if args.samples < 1 or not file_counts or min(file_counts) < 1:
        parser.error('samples and file counts must be positive')
    print(json.dumps(benchmark(args.samples, file_counts, args.workspace_root), indent=2))


if __name__ == '__main__':
    main()
