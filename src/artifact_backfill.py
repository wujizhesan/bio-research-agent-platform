"""Validate and register historical job artifact manifests."""

import argparse
import asyncio
import hashlib
import json
import mimetypes
import os
from pathlib import Path
import re

try:
    from .database import Database
    from .storage_workspace import S3ObjectReference, StorageIntegrityError
except ImportError:
    from database import Database
    from storage_workspace import S3ObjectReference, StorageIntegrityError


CHUNK_SIZE = 1024 * 1024
HEX_64 = re.compile(r'^[a-f0-9]{64}$')
SAFE_ID = re.compile(r'^[A-Za-z0-9._-]{1,32}$')


class ArtifactValidationError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = str(code)


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    )


def _digest(value):
    return hashlib.sha256(str(value).encode('utf-8')).hexdigest()


def _file_digest(path):
    digest = hashlib.sha256()
    size = 0
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(CHUNK_SIZE), b''):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def _text(value, fallback, limit):
    selected = str(value or '').strip()
    return (selected or fallback)[:limit]


def _integer(value, default=None):
    if value is None or value == '':
        return default
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ArtifactValidationError(
            'invalid_integrity_metadata',
            'artifact size metadata is not an integer',
        ) from exc


def _read_secret(name, default=''):
    direct = str(os.environ.get(name, '') or '').strip()
    file_name = str(os.environ.get(f'{name}_FILE', '') or '').strip()
    if direct and file_name:
        raise RuntimeError(f'configure only one of {name} or {name}_FILE')
    if not file_name:
        return direct or str(default)
    try:
        with open(os.path.abspath(file_name), encoding='utf-8') as handle:
            value = handle.read(65537)
    except OSError as exc:
        raise RuntimeError(f'unable to read {name}_FILE') from exc
    if len(value) > 65536:
        raise RuntimeError(f'{name}_FILE exceeds 65536 bytes')
    return value.strip() or str(default)


class LegacyArtifactBackfill:
    def __init__(
        self,
        database,
        *,
        artifact_root='output',
        s3_bucket='',
        s3_prefix='bio-agent',
        s3_expected_owner='',
        s3_client=None,
        s3_client_factory=None,
        quota_bytes=10 * 1024 ** 3,
    ):
        self.database = database
        self.artifact_root = Path(artifact_root).resolve()
        self.s3_bucket = str(s3_bucket or '').strip()
        self.s3_prefix = str(s3_prefix or '').strip('/')
        self.s3_expected_owner = str(s3_expected_owner or '').strip()
        self.s3_client = s3_client
        self.s3_client_factory = s3_client_factory or self._default_s3_client
        self.quota_bytes = max(int(quota_bytes), 1)

    @staticmethod
    def _default_s3_client():
        try:
            import boto3
        except ImportError as exc:
            raise RuntimeError('boto3 is required to validate S3 artifacts') from exc
        return boto3.client(
            's3',
            endpoint_url=os.environ.get('S3_ENDPOINT_URL') or None,
            region_name=os.environ.get('S3_REGION') or None,
            aws_access_key_id=_read_secret('AWS_ACCESS_KEY_ID') or None,
            aws_secret_access_key=_read_secret('AWS_SECRET_ACCESS_KEY') or None,
            aws_session_token=_read_secret('AWS_SESSION_TOKEN') or None,
        )

    def _identity(self, job, raw, index):
        manifest = raw if isinstance(raw, dict) else {'value': raw}
        manifest_hash = _digest(_canonical(manifest))
        artifact_id = str(manifest.get('artifact_id') or '').strip()
        if not SAFE_ID.fullmatch(artifact_id):
            artifact_id = _digest(
                f"legacy-artifact:{job['job_id']}:{index}:{manifest_hash}"
            )[:32]
        publication_id = str(manifest.get('publication_id') or '').lower().strip()
        if not HEX_64.fullmatch(publication_id):
            publication_id = _digest(
                f"legacy-publication:{job['job_id']}:{artifact_id}"
            )
        execution = job.get('execution')
        execution_key = (
            execution.get('execution_key')
            if isinstance(execution, dict) else None
        )
        execution_key = _text(
            execution_key,
            _digest(f"legacy-execution:{job['job_id']}")[:32],
            64,
        )
        backend = str(manifest.get('storage_backend') or '').lower().strip()[:32]
        reference = str(manifest.get('reference') or '').strip()
        if not backend:
            backend = 's3' if reference.startswith('bio+s3://') else 'local'
        raw_path = manifest.get('path')
        raw_key = manifest.get('storage_key')
        filename_source = manifest.get('filename')
        if not filename_source:
            filename_source = Path(str(raw_path or raw_key or '')).name
        raw_size = manifest.get('size_bytes')
        try:
            size_bytes = _integer(raw_size)
            invalid_size = False
        except ArtifactValidationError:
            size_bytes = None
            invalid_size = True
        raw_sha256 = str(manifest.get('sha256') or '').lower().strip()
        invalid_sha256 = bool(raw_sha256) and not HEX_64.fullmatch(raw_sha256)
        return {
            'publication_id': publication_id,
            '_manifest_publication_id': bool(manifest.get('publication_id')),
            'artifact_id': artifact_id,
            'job_id': str(job['job_id']),
            'project_id': str(job.get('project_id') or 'system-legacy'),
            'execution_key': execution_key,
            'fencing_token': 'legacy-backfill',
            'attempt': max(int(job.get('attempts') or 1), 1),
            'parameter': _text(
                manifest.get('parameter'), f'legacy_output_{index}', 128
            ),
            'kind': _text(manifest.get('kind'), 'file', 32),
            'storage_backend': backend,
            'filename': _text(filename_source, f'artifact-{index}', 255),
            'content_type': (
                _text(manifest.get('content_type'), '', 255) or None
            ),
            'size_bytes': size_bytes,
            '_invalid_size': invalid_size,
            'sha256': raw_sha256 if raw_sha256 and not invalid_sha256 else None,
            '_invalid_sha256': invalid_sha256,
            'path': str(raw_path).strip() if raw_path else None,
            'storage_key': str(raw_key).strip() if raw_key else None,
            'version_id': (
                str(manifest.get('version_id')).strip()[:1024]
                if manifest.get('version_id') else None
            ),
            'reference': reference or None,
            'created_at': str(
                job.get('finished_at') or job.get('created_at') or ''
            ) or None,
        }

    def _local_path(self, value):
        if not value:
            raise ArtifactValidationError(
                'missing_local_path',
                'local artifact manifest does not contain a path',
            )
        selected = Path(str(value))
        candidates = [selected] if selected.is_absolute() else [
            Path.cwd() / selected,
            self.artifact_root / selected,
        ]
        for candidate in candidates:
            candidate_is_symlink = candidate.is_symlink()
            resolved = candidate.resolve()
            try:
                resolved.relative_to(self.artifact_root)
            except ValueError:
                continue
            if resolved.exists():
                return resolved, candidate_is_symlink
        raise ArtifactValidationError(
            'local_artifact_unavailable',
            'local artifact is missing or outside the configured artifact root',
        )

    def _validate_local(self, record):
        path, selected_is_symlink = self._local_path(
            record.get('path') or record.get('reference')
        )
        if selected_is_symlink or path.is_symlink() or not path.is_file():
            raise ArtifactValidationError(
                'unsupported_local_artifact',
                'local artifact must be a regular file and cannot be a symlink',
            )
        sha256, size_bytes = _file_digest(path)
        if record.get('size_bytes') is not None and record['size_bytes'] != size_bytes:
            raise ArtifactValidationError(
                'size_mismatch',
                'local artifact size does not match its manifest',
            )
        if record.get('sha256') and record['sha256'] != sha256:
            raise ArtifactValidationError(
                'checksum_mismatch',
                'local artifact checksum does not match its manifest',
            )
        if record.get('sha256') and not HEX_64.fullmatch(record['sha256']):
            raise ArtifactValidationError(
                'invalid_integrity_metadata',
                'local artifact checksum is not a SHA-256 digest',
            )
        return {
            **record,
            'filename': path.name[:255],
            'content_type': record.get('content_type') or (
                mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
            ),
            'size_bytes': size_bytes,
            'sha256': sha256,
            'path': str(path),
            'reference': str(path),
            'storage_key': None,
            'version_id': None,
        }

    def _s3_reference(self, record):
        if record.get('reference'):
            try:
                reference = S3ObjectReference.parse(record['reference'])
            except StorageIntegrityError as exc:
                raise ArtifactValidationError(
                    'invalid_s3_reference', str(exc)
                ) from exc
            if reference is None:
                raise ArtifactValidationError(
                    'invalid_s3_reference',
                    'S3 artifact reference does not use the bio+s3 scheme',
                )
        else:
            size_bytes = record.get('size_bytes')
            sha256 = record.get('sha256')
            if (
                not record.get('storage_key')
                or not record.get('version_id')
                or size_bytes is None
                or not sha256
                or not self.s3_bucket
            ):
                raise ArtifactValidationError(
                    'unversioned_s3_artifact',
                    'S3 artifact lacks a version-locked integrity reference',
                )
            try:
                reference = S3ObjectReference(
                    self.s3_bucket,
                    record['storage_key'],
                    record['version_id'],
                    sha256,
                    size_bytes,
                )
                reference = S3ObjectReference.parse(reference.serialize())
            except (StorageIntegrityError, ValueError) as exc:
                raise ArtifactValidationError(
                    'invalid_s3_reference', str(exc)
                ) from exc
        if not self.s3_bucket or reference.bucket != self.s3_bucket:
            raise ArtifactValidationError(
                's3_bucket_mismatch',
                'S3 artifact bucket is not the configured bucket',
            )
        if self.s3_prefix and not reference.key.startswith(f'{self.s3_prefix}/'):
            raise ArtifactValidationError(
                's3_prefix_mismatch',
                'S3 artifact key is outside the configured prefix',
            )
        explicit = (
            record.get('storage_key'),
            record.get('version_id'),
            record.get('sha256'),
            record.get('size_bytes'),
        )
        expected = (
            reference.key,
            reference.version_id,
            reference.sha256,
            reference.size_bytes,
        )
        for current, wanted in zip(explicit, expected):
            if current is not None and current != wanted:
                raise ArtifactValidationError(
                    's3_manifest_mismatch',
                    'S3 artifact fields conflict with its versioned reference',
                )
        return reference

    def _validate_s3(self, record):
        reference = self._s3_reference(record)
        if self.s3_client is None:
            self.s3_client = self.s3_client_factory()
        request = {
            'Bucket': reference.bucket,
            'Key': reference.key,
            'VersionId': reference.version_id,
        }
        if self.s3_expected_owner:
            request['ExpectedBucketOwner'] = self.s3_expected_owner
        try:
            head = self.s3_client.head_object(**request)
        except Exception as exc:
            raise ArtifactValidationError(
                's3_artifact_unavailable',
                'versioned S3 artifact is unavailable',
            ) from exc
        remote_version = str(head.get('VersionId') or '')
        remote_sha256 = str(
            (head.get('Metadata') or {}).get('sha256') or ''
        ).lower()
        remote_size = int(head.get('ContentLength') or 0)
        if remote_version != reference.version_id:
            raise ArtifactValidationError(
                's3_version_mismatch',
                'S3 artifact version does not match its reference',
            )
        if remote_sha256 != reference.sha256:
            raise ArtifactValidationError(
                'checksum_mismatch',
                'S3 artifact checksum does not match its reference',
            )
        if remote_size != reference.size_bytes:
            raise ArtifactValidationError(
                'size_mismatch',
                'S3 artifact size does not match its reference',
            )
        return {
            **record,
            'filename': Path(reference.key).name[:255],
            'content_type': _text(
                record.get('content_type') or head.get('ContentType'),
                'application/octet-stream',
                255,
            ),
            'size_bytes': reference.size_bytes,
            'sha256': reference.sha256,
            'path': None,
            'storage_key': reference.key,
            'version_id': reference.version_id,
            'reference': reference.serialize(),
        }

    def validate(self, job, raw, index):
        record = self._identity(job, raw, index)
        if not isinstance(raw, dict):
            raise ArtifactValidationError(
                'invalid_manifest_entry',
                'artifact manifest entry must be an object',
            )
        if record.pop('_invalid_size', False):
            raise ArtifactValidationError(
                'invalid_integrity_metadata',
                'artifact size metadata is not an integer',
            )
        if record.pop('_invalid_sha256', False):
            raise ArtifactValidationError(
                'invalid_integrity_metadata',
                'artifact checksum is not a SHA-256 digest',
            )
        if record['storage_backend'] == 'local':
            return self._validate_local(record)
        if record['storage_backend'] == 's3':
            return self._validate_s3(record)
        raise ArtifactValidationError(
            'unsupported_storage_backend',
            f"unsupported artifact storage backend: {record['storage_backend']}",
        )

    async def run(self, *, apply=False, batch_size=100, max_issues=100):
        summary = {
            'mode': 'apply' if apply else 'check',
            'jobs_scanned': 0,
            'manifest_entries': 0,
            'already_registered': 0,
            'ready_to_register': 0,
            'registered': 0,
            'quarantined': 0,
            'unresolved': 0,
            'issues': [],
        }
        after_job_id = None
        while True:
            jobs = await self.database.list_jobs_with_artifact_manifests(
                after_job_id=after_job_id,
                limit=batch_size,
            )
            if not jobs:
                break
            durable_by_job = await self.database.list_job_artifacts_for_jobs(
                [job['job_id'] for job in jobs]
            )
            for job in jobs:
                summary['jobs_scanned'] += 1
                manifest = job.get('artifacts')
                entries = manifest if isinstance(manifest, list) else [manifest]
                durable = durable_by_job.get(job['job_id'], [])
                by_publication = {
                    item['publication_id']: item for item in durable
                }
                by_artifact = {
                    item['artifact_id']: item for item in durable
                    if item.get('status') == 'committed'
                }
                for index, raw in enumerate(entries):
                    summary['manifest_entries'] += 1
                    identity = self._identity(job, raw, index)
                    current = by_publication.get(identity['publication_id'])
                    if (
                        current is not None
                        and current.get('status') == 'committed'
                    ) or (
                        not identity['_manifest_publication_id']
                        and identity['artifact_id'] in by_artifact
                    ):
                        summary['already_registered'] += 1
                        continue
                    reason = None
                    message = None
                    validated = None
                    if current is not None and current.get('status') != 'quarantined':
                        reason = 'durable_state_conflict'
                        message = (
                            'artifact publication already exists in non-terminal '
                            f"state {current.get('status')}"
                        )
                    else:
                        try:
                            validated = self.validate(job, raw, index)
                        except ArtifactValidationError as exc:
                            reason = exc.code
                            message = str(exc)
                        except Exception as exc:
                            reason = 'validation_failed'
                            message = str(exc)
                    if validated is not None:
                        if apply:
                            try:
                                await self.database.register_legacy_job_artifact(
                                    validated,
                                    quota_bytes=self.quota_bytes,
                                )
                                summary['registered'] += 1
                                continue
                            except Exception as exc:
                                reason = 'registration_failed'
                                message = str(exc)
                        else:
                            summary['ready_to_register'] += 1
                            reason = 'registration_required'
                            message = 'validated artifact is not registered'
                    if apply and (
                        current is None
                        or current.get('status') == 'quarantined'
                    ):
                        try:
                            await self.database.quarantine_legacy_job_artifact(
                                identity,
                                f'{reason}: {message}',
                            )
                            summary['quarantined'] += 1
                        except Exception as exc:
                            reason = 'quarantine_failed'
                            message = str(exc)
                    summary['unresolved'] += 1
                    if len(summary['issues']) < max(int(max_issues), 0):
                        summary['issues'].append({
                            'job_id': job['job_id'],
                            'artifact_index': index,
                            'artifact_id': identity['artifact_id'],
                            'reason': reason,
                            'message': message,
                        })
            after_job_id = jobs[-1]['job_id']
            if len(jobs) < batch_size:
                break
        summary['passed'] = summary['unresolved'] == 0
        return summary


def _arguments(argv=None):
    parser = argparse.ArgumentParser(
        description='Validate and register historical job artifacts',
    )
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--batch-size', type=int, default=100)
    parser.add_argument('--max-issues', type=int, default=100)
    parser.add_argument(
        '--artifact-root',
        default=os.environ.get('PLUGIN_ARTIFACT_ROOT', 'output'),
    )
    return parser.parse_args(argv)


async def _run(arguments):
    role = str(os.environ.get('DATABASE_ROLE', 'internal')).lower().strip()
    if arguments.apply and role not in {'internal', 'maintenance', 'migration'}:
        raise RuntimeError('artifact backfill requires a maintenance database role')
    database = Database(_read_secret(
        'DATABASE_URL',
        'sqlite+aiosqlite:///./output/bio-agent.db',
    ))
    try:
        runner = LegacyArtifactBackfill(
            database,
            artifact_root=arguments.artifact_root,
            s3_bucket=os.environ.get('S3_BUCKET', ''),
            s3_prefix=os.environ.get('S3_PREFIX', 'bio-agent'),
            s3_expected_owner=os.environ.get('S3_EXPECTED_BUCKET_OWNER', ''),
            quota_bytes=int(os.environ.get(
                'UPLOAD_TOTAL_QUOTA_BYTES', 10 * 1024 ** 3
            )),
        )
        return await runner.run(
            apply=arguments.apply,
            batch_size=arguments.batch_size,
            max_issues=arguments.max_issues,
        )
    finally:
        await database.close()


def main(argv=None):
    arguments = _arguments(argv)
    try:
        summary = asyncio.run(_run(arguments))
    except Exception as exc:
        print(json.dumps({
            'passed': False,
            'fatal': str(exc),
        }, ensure_ascii=False, sort_keys=True))
        return 2
    print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    return 0 if summary['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
