"""Reconcile abandoned artifact publications."""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import shutil
import tempfile
from uuid import uuid4

try:
    from .artifact_store import S3ArtifactStore, unpack_execution_result
    from .database import Database
    from .settings import PlatformSettings
except ImportError:
    from artifact_store import S3ArtifactStore, unpack_execution_result
    from database import Database
    from settings import PlatformSettings


def _not_found(exc):
    response = getattr(exc, 'response', None)
    code = str(((response or {}).get('Error') or {}).get('Code') or '')
    status = ((response or {}).get('ResponseMetadata') or {}).get('HTTPStatusCode')
    return code in {'404', 'NoSuchKey', 'NoSuchVersion', 'NotFound'} or status == 404


def _heartbeat_path():
    return Path(os.environ.get(
        'ARTIFACT_RECOVERY_HEARTBEAT_PATH',
        str(Path(tempfile.gettempdir()) / 'artifact-recovery-heartbeat'),
    ))


class ArtifactRecoveryService:
    def __init__(
        self,
        database,
        artifact_store,
        artifact_root='output',
        file_root='output/uploads',
    ):
        self.database = database
        self.artifact_store = artifact_store
        self.artifact_root = Path(artifact_root).resolve()
        self.file_root = Path(file_root).resolve()

    async def reconcile(self, record, dry_run=False):
        recovery_token = str(record.get('recovery_token') or '')
        if not dry_run and not recovery_token:
            raise RuntimeError('artifact must be claimed before recovery')
        explicit_deletion = bool(record.get('delete_request_id'))
        if not explicit_deletion:
            execution = await self.database.get_execution_result(
                record['execution_key']
            )
            if execution and execution.get('status') == 'completed':
                _result, artifacts = unpack_execution_result(execution.get('result'))
                durable_publications = {
                    str(item.get('publication_id'))
                    for item in artifacts
                    if isinstance(item, dict) and item.get('publication_id')
                }
                if record['publication_id'] in durable_publications:
                    if not dry_run:
                        await self.database.finish_job_artifact_recovery(
                            record['publication_id'],
                            recovery_token,
                            'committed',
                        )
                    return 'committed'
        retention_until = record.get('retention_until')
        if record.get('status') == 'retained' and retention_until:
            try:
                retained_until = datetime.fromisoformat(
                    str(retention_until).replace('Z', '+00:00')
                )
            except ValueError:
                retained_until = None
            if retained_until is not None and retained_until > datetime.now(timezone.utc):
                return 'retained'
        if dry_run:
            return 'reclaimable'
        if record['storage_backend'] == 'local':
            return await self._reclaim_local(record)
        if record['storage_backend'] == 's3':
            return await self._reclaim_s3(record)
        await self.database.finish_job_artifact_recovery(
            record['publication_id'],
            recovery_token,
            'delete_failed' if explicit_deletion else 'orphaned',
            error='unsupported artifact storage backend',
        )
        return 'delete_failed' if explicit_deletion else 'orphaned'

    async def _reclaim_local(self, record):
        failure_status = (
            'delete_failed' if record.get('delete_request_id') else 'orphaned'
        )
        target = Path(str(record.get('path') or '')).resolve()
        if target == self.artifact_root or self.artifact_root not in target.parents:
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                failure_status,
                error='local artifact path is not a strict child of the configured root',
            )
            return failure_status
        try:
            if target.is_dir():
                await asyncio.to_thread(shutil.rmtree, target)
            else:
                await asyncio.to_thread(target.unlink, missing_ok=True)
            if await asyncio.to_thread(target.exists):
                raise RuntimeError('local artifact still exists after deletion')
        except Exception as exc:
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                failure_status,
                error=str(exc),
            )
            return failure_status
        await self.database.finish_job_artifact_recovery(
            record['publication_id'],
            record['recovery_token'],
            'deleted',
        )
        return 'deleted'

    async def _reclaim_s3(self, record):
        explicit_deletion = bool(record.get('delete_request_id'))
        failure_status = 'delete_failed' if explicit_deletion else 'orphaned'
        store = self.artifact_store
        if not isinstance(store, S3ArtifactStore):
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                failure_status,
                error='S3 artifact store is unavailable',
            )
            return failure_status
        key = str(record.get('storage_key') or '')
        allowed_prefix = '/'.join(
            item for item in (store.prefix, 'artifacts') if item
        ) + '/'
        if not key or not key.startswith(allowed_prefix):
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                failure_status,
                error='artifact storage key is outside the configured prefix',
            )
            return failure_status
        version_id = str(record.get('version_id') or '')
        if explicit_deletion and (not version_id or version_id == 'null'):
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                failure_status,
                error='explicit artifact deletion requires an immutable version id',
            )
            return failure_status
        request = store._request(Key=key)
        if version_id:
            request['VersionId'] = version_id
        try:
            head = await asyncio.to_thread(store.client.head_object, **request)
        except Exception as exc:
            if _not_found(exc):
                await self.database.finish_job_artifact_recovery(
                    record['publication_id'],
                    record['recovery_token'],
                    'deleted',
                )
                return 'deleted'
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                failure_status,
                error=str(exc),
            )
            return failure_status
        expected_size = record.get('size_bytes')
        expected_sha256 = str(record.get('sha256') or '').lower()
        metadata = head.get('Metadata') or {}
        observed_version_id = str(head.get('VersionId') or '')
        if (
            (version_id and observed_version_id and observed_version_id != version_id)
            or (expected_size is not None and int(head.get('ContentLength', -1)) != int(expected_size))
            or (expected_sha256 and str(metadata.get('sha256') or '').lower() != expected_sha256)
        ):
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                failure_status,
                error='artifact object identity or integrity does not match manifest',
            )
            return failure_status
        version_id = version_id or observed_version_id
        if not version_id or version_id == 'null':
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                failure_status,
                error='artifact object is not version locked',
            )
            return failure_status
        retain_until = head.get('ObjectLockRetainUntilDate')
        legal_hold = str(head.get('ObjectLockLegalHoldStatus') or '').upper()
        if legal_hold == 'ON' or (
            retain_until is not None
            and retain_until > datetime.now(timezone.utc)
        ):
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                'retained',
                error='artifact object is protected by object lock',
                retention_until=(
                    retain_until.isoformat()
                    if hasattr(retain_until, 'isoformat') else None
                ),
            )
            return 'retained'
        try:
            await asyncio.to_thread(
                store.client.delete_object,
                **store._request(Key=key, VersionId=version_id),
            )
        except Exception as exc:
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                'retained' if retain_until else failure_status,
                error=str(exc),
                retention_until=(
                    retain_until.isoformat()
                    if hasattr(retain_until, 'isoformat') else None
                ),
            )
            return 'retained' if retain_until else failure_status
        try:
            await asyncio.to_thread(
                store.client.head_object,
                **store._request(Key=key, VersionId=version_id),
            )
        except Exception as exc:
            if not _not_found(exc):
                await self.database.finish_job_artifact_recovery(
                    record['publication_id'],
                    record['recovery_token'],
                    failure_status,
                    error=f'artifact deletion could not be confirmed: {exc}',
                )
                return failure_status
        else:
            await self.database.finish_job_artifact_recovery(
                record['publication_id'],
                record['recovery_token'],
                failure_status,
                error='artifact object still exists after deletion',
            )
            return failure_status
        await self.database.finish_job_artifact_recovery(
            record['publication_id'],
            record['recovery_token'],
            'deleted',
        )
        return 'deleted'

    async def reconcile_file(self, record, dry_run=False):
        recovery_token = str(record.get('recovery_token') or '')
        if not dry_run and not recovery_token:
            raise RuntimeError('file must be claimed before recovery')
        retention_until = record.get('retention_until')
        if record.get('status') == 'retained' and retention_until:
            try:
                retained_until = datetime.fromisoformat(
                    str(retention_until).replace('Z', '+00:00')
                )
            except ValueError:
                retained_until = None
            if retained_until is not None and retained_until > datetime.now(timezone.utc):
                return 'retained'
        if dry_run:
            return 'reclaimable'
        explicit_deletion = bool(record.get('delete_request_id'))
        failure_status = 'delete_failed' if explicit_deletion else 'orphaned'
        if record.get('storage_backend') == 'local':
            directory = (self.file_root / str(record['file_id'])).resolve()
            if directory.parent != self.file_root:
                await self.database.finish_file_recovery(
                    record['file_id'],
                    recovery_token,
                    failure_status,
                    error='local file path is outside the configured root',
                )
                return failure_status
            try:
                await asyncio.to_thread(shutil.rmtree, directory, True)
                if await asyncio.to_thread(directory.exists):
                    raise RuntimeError('local file directory still exists after deletion')
            except Exception as exc:
                await self.database.finish_file_recovery(
                    record['file_id'], recovery_token, failure_status, error=str(exc)
                )
                return failure_status
            await self.database.finish_file_recovery(
                record['file_id'], recovery_token, 'deleted'
            )
            return 'deleted'
        if record.get('storage_backend') != 's3':
            await self.database.finish_file_recovery(
                record['file_id'],
                recovery_token,
                failure_status,
                error='unsupported file storage backend',
            )
            return failure_status
        store = self.artifact_store
        if not isinstance(store, S3ArtifactStore):
            await self.database.finish_file_recovery(
                record['file_id'],
                recovery_token,
                failure_status,
                error='S3 file store is unavailable',
            )
            return failure_status
        key = str(record.get('storage_key') or '')
        allowed_prefix = '/'.join(
            item for item in (store.prefix, str(record['file_id'])) if item
        ) + '/'
        if not key or not key.startswith(allowed_prefix):
            await self.database.finish_file_recovery(
                record['file_id'],
                recovery_token,
                failure_status,
                error='file storage key is outside the configured prefix',
            )
            return failure_status
        version_id = str(record.get('version_id') or '')
        if explicit_deletion and (not version_id or version_id == 'null'):
            await self.database.finish_file_recovery(
                record['file_id'],
                recovery_token,
                failure_status,
                error='explicit file deletion requires an immutable version id',
            )
            return failure_status
        request = store._request(Key=key)
        if version_id:
            request['VersionId'] = version_id
        try:
            head = await asyncio.to_thread(store.client.head_object, **request)
        except Exception as exc:
            if _not_found(exc):
                await self.database.finish_file_recovery(
                    record['file_id'], recovery_token, 'deleted'
                )
                return 'deleted'
            await self.database.finish_file_recovery(
                record['file_id'], recovery_token, failure_status, error=str(exc)
            )
            return failure_status
        expected_size = record.get('size_bytes')
        expected_sha256 = str(record.get('sha256') or '').lower()
        metadata = head.get('Metadata') or {}
        observed_version_id = str(head.get('VersionId') or '')
        if (
            (version_id and observed_version_id and observed_version_id != version_id)
            or (expected_size is not None and int(head.get('ContentLength', -1)) != int(expected_size))
            or (expected_sha256 and str(metadata.get('sha256') or '').lower() != expected_sha256)
        ):
            await self.database.finish_file_recovery(
                record['file_id'],
                recovery_token,
                failure_status,
                error='file object identity or integrity does not match manifest',
            )
            return failure_status
        version_id = version_id or observed_version_id
        if not version_id or version_id == 'null':
            await self.database.finish_file_recovery(
                record['file_id'],
                recovery_token,
                failure_status,
                error='file object is not version locked',
            )
            return failure_status
        retain_until = head.get('ObjectLockRetainUntilDate')
        legal_hold = str(head.get('ObjectLockLegalHoldStatus') or '').upper()
        if legal_hold == 'ON' or (
            retain_until is not None
            and retain_until > datetime.now(timezone.utc)
        ):
            await self.database.finish_file_recovery(
                record['file_id'],
                recovery_token,
                'retained',
                error='file object is protected by object lock',
                retention_until=(
                    retain_until.isoformat()
                    if hasattr(retain_until, 'isoformat') else None
                ),
            )
            return 'retained'
        try:
            await asyncio.to_thread(
                store.client.delete_object,
                **store._request(Key=key, VersionId=version_id),
            )
        except Exception as exc:
            await self.database.finish_file_recovery(
                record['file_id'],
                recovery_token,
                'retained' if retain_until else failure_status,
                error=str(exc),
                retention_until=(
                    retain_until.isoformat()
                    if hasattr(retain_until, 'isoformat') else None
                ),
            )
            return 'retained' if retain_until else failure_status
        try:
            await asyncio.to_thread(
                store.client.head_object,
                **store._request(Key=key, VersionId=version_id),
            )
        except Exception as exc:
            if not _not_found(exc):
                await self.database.finish_file_recovery(
                    record['file_id'],
                    recovery_token,
                    failure_status,
                    error=f'file deletion could not be confirmed: {exc}',
                )
                return failure_status
        else:
            await self.database.finish_file_recovery(
                record['file_id'],
                recovery_token,
                failure_status,
                error='file object still exists after deletion',
            )
            return failure_status
        await self.database.finish_file_recovery(
            record['file_id'], recovery_token, 'deleted'
        )
        return 'deleted'

    async def run_once(
        self,
        grace_seconds=3600,
        limit=100,
        lease_seconds=300,
        dry_run=False,
    ):
        cutoff = (
            datetime.now(timezone.utc) - timedelta(seconds=max(grace_seconds, 0))
        ).isoformat()
        if dry_run:
            records = await self.database.list_recoverable_job_artifacts(
                cutoff,
                limit,
            )
            files = await self.database.list_recoverable_files(cutoff, limit)
        else:
            records = await self.database.claim_recoverable_job_artifacts(
                uuid4().hex,
                cutoff,
                lease_seconds=lease_seconds,
                limit=limit,
            )
            files = await self.database.claim_recoverable_files(
                uuid4().hex,
                cutoff,
                lease_seconds=lease_seconds,
                limit=limit,
            )
        outcomes = {
            'committed': 0,
            'deleted': 0,
            'retained': 0,
            'orphaned': 0,
            'delete_failed': 0,
            'reclaimable': 0,
            'errors': 0,
        }
        for record in records:
            try:
                outcome = await self.reconcile(record, dry_run=dry_run)
            except Exception as exc:
                outcomes['errors'] += 1
                print(json.dumps({
                    'event': 'artifact.recovery_failed',
                    'publication_id': record.get('publication_id'),
                    'error_type': type(exc).__name__,
                    'error': str(exc)[:512],
                }, ensure_ascii=False, sort_keys=True), flush=True)
                continue
            outcomes[outcome] += 1
        for record in files:
            try:
                outcome = await self.reconcile_file(record, dry_run=dry_run)
            except Exception as exc:
                outcomes['errors'] += 1
                print(json.dumps({
                    'event': 'file.recovery_failed',
                    'file_id': record.get('file_id'),
                    'error_type': type(exc).__name__,
                    'error': str(exc)[:512],
                }, ensure_ascii=False, sort_keys=True), flush=True)
                continue
            outcomes[outcome] += 1
        return {
            'scanned': len(records) + len(files),
            'artifact_scanned': len(records),
            'file_scanned': len(files),
            **outcomes,
        }


async def _run(args):
    settings = PlatformSettings.from_env().validate('maintenance')
    database = Database(settings.database_url)
    store = S3ArtifactStore(
        settings.s3_bucket,
        prefix=settings.s3_prefix,
        endpoint_url=settings.s3_endpoint_url or None,
        region_name=settings.s3_region or None,
        expected_bucket_owner=settings.s3_expected_bucket_owner or None,
        access_key_id=settings.aws_access_key_id or None,
        secret_access_key=settings.aws_secret_access_key or None,
        session_token=settings.aws_session_token or None,
    ) if settings.storage_backend == 's3' else None
    service = ArtifactRecoveryService(
        database,
        store,
        artifact_root=os.environ.get('PLUGIN_ARTIFACT_ROOT', 'output'),
        file_root=settings.upload_root,
    )
    try:
        while True:
            result = await service.run_once(
                grace_seconds=args.grace_seconds,
                limit=args.limit,
                lease_seconds=args.lease_seconds,
                dry_run=args.dry_run,
            )
            print(json.dumps(result, ensure_ascii=False, sort_keys=True), flush=True)
            _heartbeat_path().write_text(
                datetime.now(timezone.utc).isoformat(),
                encoding='utf-8',
            )
            if args.loop_seconds <= 0 or args.dry_run:
                break
            await asyncio.sleep(args.loop_seconds)
    finally:
        await database.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Reconcile abandoned job artifacts')
    parser.add_argument(
        '--grace-seconds',
        type=int,
        default=int(os.environ.get('ARTIFACT_RECOVERY_GRACE_SECONDS', '3600')),
    )
    parser.add_argument(
        '--lease-seconds',
        type=int,
        default=int(os.environ.get('ARTIFACT_RECOVERY_LEASE_SECONDS', '300')),
    )
    parser.add_argument(
        '--limit',
        type=int,
        default=int(os.environ.get('ARTIFACT_RECOVERY_BATCH_SIZE', '100')),
    )
    parser.add_argument(
        '--loop-seconds',
        type=int,
        default=int(os.environ.get('ARTIFACT_RECOVERY_INTERVAL_SECONDS', '0')),
    )
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--check', action='store_true')
    args = parser.parse_args(argv)
    if args.check:
        heartbeat = _heartbeat_path()
        maximum_age = max(
            int(os.environ.get('ARTIFACT_RECOVERY_HEALTH_MAX_AGE_SECONDS', '660')),
            1,
        )
        if not heartbeat.is_file():
            raise SystemExit(1)
        age = datetime.now(timezone.utc).timestamp() - heartbeat.stat().st_mtime
        raise SystemExit(0 if age <= maximum_age else 1)
    asyncio.run(_run(args))


if __name__ == '__main__':
    main()
