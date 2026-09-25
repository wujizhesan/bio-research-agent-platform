"""REST and SSE routes for platform job lifecycle management."""

import asyncio
import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from urllib.parse import quote
from uuid import uuid4

from fastapi import Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

try:
    from .api_contracts import (
        JobCreate,
        JobResolution,
        iter_artifact_values,
        resolve_artifact_path,
    )
    from .auth import Principal
    from .observability import JOB_STATUS, JOB_SUBMISSIONS, log_event
    from .run_context import bind_run_actor
    from .storage_workspace import S3ObjectReference, StorageIntegrityError
except ImportError:
    from api_contracts import JobCreate, JobResolution, iter_artifact_values, resolve_artifact_path
    from auth import Principal
    from observability import JOB_STATUS, JOB_SUBMISSIONS, log_event
    from run_context import bind_run_actor
    from storage_workspace import S3ObjectReference, StorageIntegrityError


@dataclass(frozen=True)
class JobRouteHandlers:
    read_job: object
    submit_job: object


def _sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _scoped_idempotency_key(subject, project_id, value):
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError('idempotency key must be a non-empty string')
    normalized = value.strip()
    if len(normalized) > 128:
        raise ValueError('idempotency key is too long')
    scope = json.dumps(
        [str(subject), str(project_id or ''), normalized],
        ensure_ascii=False,
        separators=(',', ':'),
    ).encode('utf-8')
    return f'scoped:{hashlib.sha256(scope).hexdigest()}'


def _job_payload_hash(record):
    payload = {
        'tool': record.get('tool'),
        'arguments': record.get('_arguments', {}),
        'resources': record.get('resources', {}),
        'priority': int(record.get('priority', 0)),
        'project_id': record.get('project_id'),
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(',', ':'),
        default=str,
    ).encode('utf-8')
    return hashlib.sha256(encoded).hexdigest()


def _register_job_query_routes(
    app,
    *,
    jobs,
    database,
    require_permission,
    project_access,
    job_access,
    expose_job,
    read_job,
    canonicalize_artifact_batch,
):
    @app.get('/api/v1/scheduler/resources', tags=['jobs'])
    async def scheduler_resources(
        principal: Principal = Depends(require_permission('jobs:read')),
    ):
        status_reader = getattr(jobs, 'resource_status', None)
        if status_reader is None:
            raise HTTPException(status_code=501, detail='scheduler resource status is unavailable')
        return {'status': 'ok', 'scheduler': status_reader()}

    @app.get('/api/v1/jobs', tags=['jobs'])
    async def list_jobs(
        limit: int = Query(default=20, ge=1, le=100),
        project_id: str | None = Query(default=None, min_length=1, max_length=64),
        principal: Principal = Depends(require_permission('jobs:read')),
    ):
        if project_id:
            await project_access(
                project_id,
                principal,
                {'owner', 'editor', 'viewer'},
            )
        visible_projects = None
        if 'admin' not in principal.roles:
            visible_projects = {
                item['project_id']
                for item in await database.list_projects(principal.subject, 100)
            }

        def visible(item):
            item_project = item.get('project_id')
            if project_id and item_project != project_id:
                return None
            if not item_project and visible_projects is not None:
                return None
            if (
                item_project
                and visible_projects is not None
                and item_project not in visible_projects
            ):
                return None
            return item

        async def prepare(records):
            exposed = [await expose_job(record) for record in records]
            canonical = await canonicalize_artifact_batch(exposed)
            return [item for item in map(visible, canonical) if item is not None]

        if app.state.job_backend == 'redis':
            records = await database.list_jobs_for_principal(
                principal.subject,
                is_admin='admin' in principal.roles,
                project_id=project_id,
                limit=limit,
            )
            if records:
                records = await prepare(records)
                for record in records:
                    JOB_STATUS.labels(record['tool'], record['status']).set(1)
                return {'status': 'ok', 'jobs': records}
        records = jobs.list(limit)
        records = await prepare(records)
        for record in records:
            await database.upsert_job(record)
            JOB_STATUS.labels(record['tool'], record['status']).set(1)
        return {'status': 'ok', 'jobs': records}

    @app.get('/api/v1/jobs/{job_id}', tags=['jobs'])
    async def get_job(
        job_id: str,
        principal: Principal = Depends(require_permission('jobs:read')),
    ):
        record = await read_job(job_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f'job not found: {job_id}',
            )
        await job_access(job_id, principal, {'owner', 'editor', 'viewer'})
        return {'status': 'ok', 'job': record}


def _register_job_event_routes(
    app,
    *,
    jobs,
    database,
    output_root,
    audit,
    require_permission,
    job_access,
    issue_stream_ticket,
    stream_ticket_ttl,
    stream_principal,
    read_job,
    allow_legacy_artifact_paths,
):
    @app.post('/api/v1/jobs/{job_id}/events/ticket', tags=['jobs'])
    async def job_events_ticket(
        job_id: str,
        principal: Principal = Depends(require_permission('jobs:read')),
    ):
        if await read_job(job_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f'job not found: {job_id}',
            )
        await job_access(job_id, principal, {'owner', 'editor', 'viewer'})
        return {
            'status': 'ok',
            'ticket': issue_stream_ticket(job_id, principal),
            'expires_in': stream_ticket_ttl,
        }

    @app.get(
        '/api/v1/jobs/{job_id}/artifacts',
        tags=['jobs'],
        deprecated=True,
        include_in_schema=allow_legacy_artifact_paths,
    )
    async def download_job_artifact(
        job_id: str,
        artifact_path: str = Query(min_length=1, alias='path'),
        principal: Principal = Depends(require_permission('jobs:read')),
    ):
        record = await read_job(job_id)
        if record is None:
            raise HTTPException(
                status_code=404,
                detail=f'job not found: {job_id}',
            )
        await job_access(job_id, principal, {'owner', 'editor', 'viewer'})
        if not allow_legacy_artifact_paths:
            raise HTTPException(
                status_code=410,
                detail=(
                    'legacy path-based artifact downloads are disabled; '
                    'use the artifact_id endpoint'
                ),
                headers={
                    'Deprecation': 'true',
                    'Sunset': 'Thu, 31 Dec 2026 23:59:59 GMT',
                },
            )
        allowed_paths = {
            resolve_artifact_path(path, output_root)
            for path in iter_artifact_values(record.get('result'))
        }
        target = resolve_artifact_path(artifact_path, output_root)
        if target is None or target not in allowed_paths:
            raise HTTPException(
                status_code=404,
                detail='artifact not found for job',
            )
        publications = await database.list_job_artifacts(job_id)
        if any(
            resolve_artifact_path(item.get('path') or '', output_root) == target
            and item.get('status') != 'committed'
            for item in publications
            if item.get('path')
        ):
            raise HTTPException(
                status_code=404,
                detail='artifact not found for job',
            )
        await audit.record(
            principal,
            'job.artifact_download',
            'job',
            job_id,
            {'filename': target.name, 'legacy_path': True},
        )
        return FileResponse(
            target,
            filename=target.name,
            content_disposition_type='attachment',
            headers={
                'X-Job-ID': job_id,
                'Content-Security-Policy': "sandbox; default-src 'none'",
                'Deprecation': 'true',
                'Sunset': 'Thu, 31 Dec 2026 23:59:59 GMT',
                'Link': (
                    f'</api/v1/jobs/{job_id}/artifacts/{{artifact_id}}>; '
                    'rel="successor-version"'
                ),
            },
        )

    @app.get('/api/v1/jobs/{job_id}/artifacts/{artifact_id}', tags=['jobs'])
    async def download_published_job_artifact(
        job_id: str,
        artifact_id: str,
        principal: Principal = Depends(require_permission('jobs:read')),
    ):
        if len(artifact_id) != 32 or any(
            character not in '0123456789abcdef' for character in artifact_id
        ):
            raise HTTPException(status_code=404, detail='artifact not found for job')
        record = await read_job(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')
        await job_access(job_id, principal, {'owner', 'editor', 'viewer'})
        artifact = await database.get_job_artifact(
            job_id,
            artifact_id,
            statuses={'committed'},
        )
        if artifact is None:
            raise HTTPException(status_code=404, detail='artifact not found for job')
        filename = Path(str(artifact.get('filename') or 'artifact')).name
        backend = str(artifact.get('storage_backend') or '')
        await audit.record(
            principal,
            'job.artifact_download',
            'job',
            job_id,
            {'artifact_id': artifact_id, 'filename': filename},
        )
        headers = {
            'X-Job-ID': job_id,
            'X-Artifact-SHA256': str(artifact.get('sha256') or ''),
            'Content-Security-Policy': "sandbox; default-src 'none'",
            'Content-Disposition': (
                "attachment; filename*=UTF-8''" + quote(filename, safe='')
            ),
        }
        if backend == 'local':
            target = resolve_artifact_path(artifact.get('path') or '', output_root)
            if target is None or not target.is_file():
                raise HTTPException(status_code=404, detail='artifact object is unavailable')
            expected_size = int(artifact.get('size_bytes') or -1)
            expected_sha256 = str(artifact.get('sha256') or '').lower()
            actual_sha256 = await asyncio.to_thread(_sha256_file, target)
            if target.stat().st_size != expected_size or actual_sha256 != expected_sha256:
                raise HTTPException(
                    status_code=502,
                    detail='artifact integrity verification failed',
                )
            return FileResponse(
                target,
                media_type=str(artifact.get('content_type') or 'application/octet-stream'),
                filename=filename,
                content_disposition_type='attachment',
                headers={key: value for key, value in headers.items() if key != 'Content-Disposition'},
            )
        if backend != 's3' or app.state.storage_backend != 's3':
            raise HTTPException(status_code=503, detail='artifact storage is unavailable')
        storage = app.state.file_storage
        key = str(artifact.get('storage_key') or '')
        version_id = str(artifact.get('version_id') or '')
        expected_sha256 = str(artifact.get('sha256') or '').lower()
        expected_size = int(artifact.get('size_bytes') or -1)
        try:
            reference = S3ObjectReference.parse(artifact.get('reference') or '')
        except StorageIntegrityError as exc:
            raise HTTPException(status_code=409, detail='artifact manifest is invalid') from exc
        allowed_prefix = '/'.join(item for item in (
            str(getattr(storage, 'prefix', '') or '').strip('/'),
            'artifacts',
        ) if item) + '/'
        if (
            reference is None
            or reference.bucket != storage.bucket
            or reference.key != key
            or reference.version_id != version_id
            or reference.sha256 != expected_sha256
            or reference.size_bytes != expected_size
            or not key.startswith(allowed_prefix)
        ):
            raise HTTPException(status_code=409, detail='artifact manifest is incomplete')
        request = {'Bucket': storage.bucket, 'Key': key, 'VersionId': version_id}
        if getattr(storage, 'expected_bucket_owner', None):
            request['ExpectedBucketOwner'] = storage.expected_bucket_owner
        try:
            response = await asyncio.to_thread(storage.client.get_object, **request)
            metadata = response.get('Metadata') or {}
            if (
                str(metadata.get('sha256') or '').lower() != expected_sha256
                or int(response.get('ContentLength') or -1) != expected_size
                or str(response.get('VersionId') or version_id) != version_id
            ):
                response['Body'].close()
                raise HTTPException(status_code=502, detail='artifact integrity verification failed')
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=404, detail='artifact object is unavailable') from exc

        async def stream_object():
            body = response['Body']
            try:
                while True:
                    chunk = await asyncio.to_thread(body.read, 1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                await asyncio.to_thread(body.close)

        return StreamingResponse(
            stream_object(),
            media_type=str(artifact.get('content_type') or 'application/octet-stream'),
            headers=headers,
        )

    @app.delete(
        '/api/v1/jobs/{job_id}/artifacts/{artifact_id}',
        status_code=202,
        tags=['jobs'],
    )
    async def delete_published_job_artifact(
        job_id: str,
        artifact_id: str,
        idempotency_key: str | None = Header(
            default=None,
            alias='Idempotency-Key',
            max_length=128,
        ),
        principal: Principal = Depends(require_permission('jobs:write')),
    ):
        if len(artifact_id) != 32 or any(
            character not in '0123456789abcdef' for character in artifact_id
        ):
            raise HTTPException(status_code=404, detail='artifact not found for job')
        if await read_job(job_id) is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')
        await job_access(job_id, principal, {'owner'})
        try:
            request_id = _scoped_idempotency_key(
                principal.subject,
                f'{job_id}:{artifact_id}',
                idempotency_key,
            ) or uuid4().hex
            artifact = await database.request_job_artifact_deletion(
                job_id,
                artifact_id,
                principal.subject,
                request_id,
                datetime.now(timezone.utc).isoformat(),
            )
        except ValueError as exc:
            if str(exc) == 'artifact publication not found':
                raise HTTPException(
                    status_code=404,
                    detail='artifact not found for job',
                ) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        await audit.record(
            principal,
            'job.artifact_delete_requested',
            'job_artifact',
            artifact_id,
            {
                'job_id': job_id,
                'request_id': artifact.get('delete_request_id'),
                'status': artifact.get('status'),
            },
        )
        return {
            'status': 'deletion_requested',
            'artifact': artifact,
        }

    @app.get(
        '/api/v1/jobs/{job_id}/artifacts/{artifact_id}/deletion',
        tags=['jobs'],
    )
    async def job_artifact_deletion_status(
        job_id: str,
        artifact_id: str,
        principal: Principal = Depends(require_permission('jobs:read')),
    ):
        if len(artifact_id) != 32 or any(
            character not in '0123456789abcdef' for character in artifact_id
        ):
            raise HTTPException(status_code=404, detail='artifact not found for job')
        if await read_job(job_id) is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')
        await job_access(job_id, principal, {'owner'})
        artifact = await database.get_job_artifact(job_id, artifact_id)
        if artifact is None or not artifact.get('delete_request_id'):
            raise HTTPException(status_code=404, detail='artifact deletion not found')
        events = await database.list_storage_deletion_events(
            'job_artifact',
            artifact['publication_id'],
        )
        return {'status': 'ok', 'artifact': artifact, 'events': events}

    @app.post(
        '/api/v1/jobs/{job_id}/artifacts/{artifact_id}/deletion/retry',
        status_code=202,
        tags=['jobs'],
    )
    async def retry_job_artifact_deletion(
        job_id: str,
        artifact_id: str,
        idempotency_key: str | None = Header(
            default=None,
            alias='Idempotency-Key',
            max_length=128,
        ),
        principal: Principal = Depends(require_permission('jobs:write')),
    ):
        if len(artifact_id) != 32 or any(
            character not in '0123456789abcdef' for character in artifact_id
        ):
            raise HTTPException(status_code=404, detail='artifact not found for job')
        if await read_job(job_id) is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')
        await job_access(job_id, principal, {'owner'})
        try:
            request_id = _scoped_idempotency_key(
                principal.subject,
                f'{job_id}:{artifact_id}:retry',
                idempotency_key,
            ) or uuid4().hex
            artifact = await database.retry_job_artifact_deletion(
                job_id,
                artifact_id,
                principal.subject,
                request_id,
                datetime.now(timezone.utc).isoformat(),
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail='artifact deletion not found') from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        deduplicated = bool(artifact.pop('_deduplicated', False))
        if not deduplicated:
            await audit.record(
                principal,
                'job.artifact_delete_retried',
                'job_artifact',
                artifact_id,
                {'job_id': job_id, 'request_id': request_id},
            )
        return {'status': 'deletion_requested', 'artifact': artifact}

    @app.get('/api/v1/jobs/{job_id}/events', tags=['jobs'])
    async def job_events(
        job_id: str,
        interval_seconds: float = Query(default=0.2, ge=0.05, le=5),
        timeout_seconds: float = Query(default=60, ge=1, le=300),
        last_event_id: str | None = Header(
            default=None,
            alias='Last-Event-ID',
            max_length=128,
            pattern=r'^(?:r-[0-9]+|[0-9]+-[0-9]+)$',
        ),
        query_last_event_id: str | None = Query(
            default=None,
            alias='last_event_id',
            max_length=128,
            pattern=r'^(?:r-[0-9]+|[0-9]+-[0-9]+)$',
        ),
        principal: Principal = Depends(stream_principal),
    ):
        if await read_job(job_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f'job not found: {job_id}',
            )
        await job_access(job_id, principal, {'owner', 'editor', 'viewer'})
        subscriber = None
        event_reader = None
        durable_event_reader = None
        durable_mode = False
        if app.state.job_backend == 'redis':
            event_reader = getattr(jobs, 'read_job_events', None)
            durable_event_reader = getattr(database, 'list_job_events', None)
            if durable_event_reader is not None:
                try:
                    durable_mode = await database.get_job(job_id) is not None
                except Exception as exc:
                    log_event(
                        'job.sse_durable_lookup_failed',
                        level=logging.ERROR,
                        job_id=job_id,
                        error_type=type(exc).__name__,
                    )
            if event_reader is None:
                subscribe = getattr(jobs, 'subscribe_job_events', None)
                if subscribe is not None:
                    subscriber = subscribe(job_id)

        async def stream():
            last_signature = None
            event_cursor = last_event_id or query_last_event_id or '0-0'
            if not durable_mode and event_cursor.startswith('r-'):
                event_cursor = '0-0'
            redis_wake_cursor = (
                event_cursor if not event_cursor.startswith('r-') else '0-0'
            )
            pending_durable_until = 0.0
            pending_durable_revision = 0
            durable_revision = 0
            redis_wake_error_reported = False
            durable_replay_error_reported = False
            deadline = monotonic() + timeout_seconds
            next_access_check = monotonic() + 1.0
            try:
                while True:
                    now = monotonic()
                    if now >= next_access_check:
                        try:
                            await job_access(
                                job_id,
                                principal,
                                {'owner', 'editor', 'viewer'},
                            )
                        except HTTPException as exc:
                            if exc.status_code not in {403, 404}:
                                raise
                            payload = {
                                'status': 'access_revoked',
                                'error': 'job access revoked',
                            }
                            yield (
                                'event: access_revoked\n'
                                f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
                            )
                            return
                        next_access_check = now + 1.0
                    if durable_mode:
                        durable_failed = False
                        try:
                            durable_events = await durable_event_reader(
                                job_id,
                                after_event_id=event_cursor,
                                limit=100,
                            )
                            durable_replay_error_reported = False
                        except Exception as exc:
                            durable_events = []
                            durable_failed = True
                            if not durable_replay_error_reported:
                                log_event(
                                    'job.sse_durable_replay_failed',
                                    level=logging.ERROR,
                                    job_id=job_id,
                                    error_type=type(exc).__name__,
                                )
                            durable_replay_error_reported = True
                        for durable_event in durable_events:
                            revision = int(durable_event['revision'])
                            durable_revision = max(durable_revision, revision)
                            durable_event_id = f'r-{revision}'
                            durable_record = durable_event['job']
                            event_cursor = durable_event_id
                            wake_event_id = str(durable_event.get('event_id') or '')
                            wake_parts = wake_event_id.split('-')
                            if len(wake_parts) == 2 and all(
                                part.isdigit() for part in wake_parts
                            ):
                                if tuple(map(int, wake_parts)) > tuple(
                                    map(int, redis_wake_cursor.split('-'))
                                ):
                                    redis_wake_cursor = wake_event_id
                            signature = json.dumps(
                                durable_record,
                                ensure_ascii=False,
                                sort_keys=True,
                                default=str,
                            )
                            if signature == last_signature:
                                continue
                            payload = {
                                'status': 'ok',
                                'job': durable_record,
                                **(
                                    {'replay_gap': True}
                                    if durable_event.get('replay_gap') else {}
                                ),
                            }
                            yield (
                                f'id: {durable_event_id}\n'
                                'event: job\n'
                                'data: '
                                + json.dumps(
                                    payload, ensure_ascii=False, default=str
                                )
                                + '\n\n'
                            )
                            last_signature = signature
                            if durable_event.get('terminal'):
                                return
                        if durable_events:
                            if durable_revision >= pending_durable_revision:
                                pending_durable_until = 0.0
                            continue
                        if durable_failed or last_signature is None or monotonic() >= deadline:
                            try:
                                record = (
                                    await read_job(job_id)
                                    if durable_failed else await database.get_job(job_id)
                                )
                            except Exception as exc:
                                log_event(
                                    'job.sse_durable_snapshot_failed',
                                    level=logging.ERROR,
                                    job_id=job_id,
                                    error_type=type(exc).__name__,
                                )
                                record = await read_job(job_id)
                            if record is None:
                                yield (
                                    'event: error\n'
                                    'data: '
                                    + json.dumps({
                                        'status': 'error',
                                        'error': f'job not found: {job_id}',
                                    }, ensure_ascii=False)
                                    + '\n\n'
                                )
                                return
                            signature = json.dumps(
                                record,
                                ensure_ascii=False,
                                sort_keys=True,
                                default=str,
                            )
                            if signature != last_signature:
                                yield (
                                    'event: job\n'
                                    'data: '
                                    + json.dumps(
                                        {'status': 'ok', 'job': record},
                                        ensure_ascii=False,
                                        default=str,
                                    )
                                    + '\n\n'
                                )
                                last_signature = signature
                            if record.get('status') in {
                                'completed', 'failed', 'cancelled', 'indeterminate',
                            }:
                                return
                            if monotonic() >= deadline:
                                yield (
                                    'event: timeout\n'
                                    'data: '
                                    + json.dumps(
                                        {'status': 'timeout', 'job': record},
                                        ensure_ascii=False,
                                        default=str,
                                    )
                                    + '\n\n'
                                )
                                return
                        else:
                            yield ': keep-alive\n\n'
                        if pending_durable_until > monotonic():
                            await asyncio.sleep(min(
                                max(interval_seconds, 0.2),
                                pending_durable_until - monotonic(),
                            ))
                            continue
                        started_wait = monotonic()
                        woke = False
                        if event_reader is not None:
                            try:
                                wake_events = await asyncio.to_thread(
                                    event_reader,
                                    job_id,
                                    redis_wake_cursor,
                                    1000,
                                    100,
                                )
                                if wake_events:
                                    redis_wake_cursor = wake_events[-1][0]
                                    woke = True
                                    wake_revision = max(
                                        int(event.get('revision') or 0)
                                        for _, event in wake_events
                                    )
                                    if wake_revision > durable_revision or not wake_revision:
                                        pending_durable_revision = wake_revision
                                        pending_durable_until = monotonic() + 1.0
                                redis_wake_error_reported = False
                            except Exception as exc:
                                if not redis_wake_error_reported:
                                    log_event(
                                        'job.sse_redis_wake_failed',
                                        level=logging.WARNING,
                                        job_id=job_id,
                                        error_type=type(exc).__name__,
                                    )
                                redis_wake_error_reported = True
                        if not woke:
                            await asyncio.sleep(max(
                                0,
                                max(float(interval_seconds), 1.0)
                                - (monotonic() - started_wait),
                            ))
                        continue
                    record = None
                    if event_reader is not None:
                        events = await asyncio.to_thread(
                            event_reader,
                            job_id,
                            event_cursor,
                            1000,
                            100,
                        )
                        for record_event_id, event_record in events:
                            event_cursor = record_event_id
                            signature = json.dumps(
                                event_record,
                                ensure_ascii=False,
                                sort_keys=True,
                                default=str,
                            )
                            if signature == last_signature:
                                continue
                            payload = {'status': 'ok', 'job': event_record}
                            yield (
                                f'id: {record_event_id}\n'
                                'event: job\n'
                                'data: '
                                + json.dumps(
                                    payload,
                                    ensure_ascii=False,
                                    default=str,
                                )
                                + '\n\n'
                            )
                            last_signature = signature
                            if event_record.get('status') in {
                                'completed', 'failed', 'cancelled', 'indeterminate',
                            }:
                                return
                        if events:
                            continue
                        if durable_event_reader is not None:
                            try:
                                durable_events = await durable_event_reader(
                                    job_id,
                                    after_event_id=event_cursor,
                                    limit=100,
                                )
                            except Exception:
                                durable_events = []
                            for durable_event in durable_events:
                                durable_event_id = durable_event['event_id']
                                durable_record = durable_event['job']
                                event_cursor = durable_event_id
                                signature = json.dumps(
                                    durable_record,
                                    ensure_ascii=False,
                                    sort_keys=True,
                                    default=str,
                                )
                                if signature == last_signature:
                                    continue
                                payload = {
                                    'status': 'ok',
                                    'job': durable_record,
                                    **(
                                        {'replay_gap': True}
                                        if durable_event.get('replay_gap') else {}
                                    ),
                                }
                                yield (
                                    f'id: {durable_event_id}\n'
                                    'event: job\n'
                                    'data: '
                                    + json.dumps(
                                        payload,
                                        ensure_ascii=False,
                                        default=str,
                                    )
                                    + '\n\n'
                                )
                                last_signature = signature
                                if durable_event.get('terminal'):
                                    return
                            if durable_events:
                                continue
                    elif subscriber is not None:
                        message = await asyncio.to_thread(
                            subscriber.get_message,
                            ignore_subscribe_messages=True,
                            timeout=1,
                        )
                        if message and message.get('type') == 'message':
                            raw = message.get('data')
                            if isinstance(raw, bytes):
                                raw = raw.decode('utf-8')
                            record = json.loads(raw)
                    if record is None:
                        record = await read_job(job_id)
                    if record is None:
                        payload = {
                            'status': 'error',
                            'error': f'job not found: {job_id}',
                        }
                        yield (
                            'event: error\n'
                            f'data: {json.dumps(payload, ensure_ascii=False)}\n\n'
                        )
                        return
                    signature = json.dumps(
                        record,
                        ensure_ascii=False,
                        sort_keys=True,
                        default=str,
                    )
                    emitted = signature != last_signature
                    if emitted:
                        payload = {'status': 'ok', 'job': record}
                        yield (
                            'event: job\n'
                            'data: '
                            + json.dumps(
                                payload,
                                ensure_ascii=False,
                                default=str,
                            )
                            + '\n\n'
                        )
                        last_signature = signature
                    if record.get('status') in {
                        'completed',
                        'failed',
                        'cancelled',
                        'indeterminate',
                    }:
                        return
                    if monotonic() >= deadline:
                        payload = {'status': 'timeout', 'job': record}
                        yield (
                            'event: timeout\n'
                            'data: '
                            + json.dumps(
                                payload,
                                ensure_ascii=False,
                                default=str,
                            )
                            + '\n\n'
                        )
                        return
                    if not emitted:
                        yield ': keep-alive\n\n'
                    if subscriber is None and event_reader is None:
                        await asyncio.sleep(interval_seconds)
            finally:
                if subscriber is not None:
                    await asyncio.to_thread(subscriber.close)

        return StreamingResponse(
            stream(),
            media_type='text/event-stream',
            headers={
                'Cache-Control': 'no-cache',
                'X-Accel-Buffering': 'no',
            },
        )


def _register_job_mutation_routes(
    app,
    *,
    jobs,
    database,
    audit,
    require_permission,
    job_access,
    expose_job,
):
    @app.post('/api/v1/jobs/{job_id}/cancel', status_code=202, tags=['jobs'])
    async def cancel_job(
        job_id: str,
        principal: Principal = Depends(require_permission('jobs:write')),
    ):
        await job_access(job_id, principal, {'owner', 'editor'})
        try:
            record = jobs.cancel(job_id)
        except ValueError as exc:
            record = (
                await database.cancel_deferred_job(job_id)
                if app.state.job_backend == 'redis'
                and str(exc).startswith('job not found:')
                else None
            )
            if record is None:
                status_code = 404 if str(exc).startswith('job not found:') else 400
                raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        else:
            if app.state.job_backend == 'redis':
                persisted = await database.request_job_cancel(
                    job_id,
                    known_waiting=(
                        record['status'] == 'cancelled'
                        and (record.get('scheduling') or {}).get('status')
                        == 'waiting_for_external_service'
                    ),
                )
                if persisted is not None and persisted['status'] == 'cancelled':
                    record = persisted
            else:
                await database.upsert_job(record)
        JOB_STATUS.labels(record['tool'], record['status']).set(1)
        await audit.record(
            principal,
            'job.cancel',
            'job',
            job_id,
            {'status': record['status']},
        )
        if record['status'] == 'cancelled':
            response_status = 'cancelled'
        elif record['status'] in {'completed', 'failed', 'indeterminate'}:
            response_status = 'already_terminal'
        else:
            response_status = 'cancellation_requested'
        return {'status': response_status, 'job': record}

    @app.post('/api/v1/jobs/{job_id}/resolve', tags=['jobs'])
    async def resolve_indeterminate_job(
        job_id: str,
        payload: JobResolution,
        principal: Principal = Depends(require_permission('jobs:approve')),
    ):
        await job_access(job_id, principal, {'owner', 'editor'})
        resolver = getattr(jobs, 'resolve_indeterminate', None)
        if resolver is None:
            raise HTTPException(
                status_code=501,
                detail='job resolution is unavailable for this backend',
            )
        try:
            record = resolver(
                job_id,
                payload.decision,
                payload.reason,
                principal.subject,
                evidence=payload.evidence,
            )
        except ValueError as exc:
            status_code = 404 if str(exc).startswith('job not found:') else 409
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        durable_reader = getattr(jobs, 'durable_record', None)
        durable = durable_reader(job_id) if durable_reader else None
        await database.upsert_job(durable or record)
        JOB_STATUS.labels(record['tool'], record['status']).set(1)
        await audit.record(
            principal,
            'job.resolve',
            'job',
            job_id,
            {
                'decision': payload.decision,
                'reason': payload.reason,
                'evidence_keys': sorted(payload.evidence),
                'status': record['status'],
            },
        )
        return {'status': 'resolved', 'job': await expose_job(record)}

    @app.post('/api/v1/jobs/{job_id}/retry', status_code=202, tags=['jobs'])
    async def retry_job(
        job_id: str,
        migrate_implementation: bool = Query(default=False),
        principal: Principal = Depends(require_permission('jobs:write')),
    ):
        project_id = await job_access(job_id, principal, {'owner', 'editor'})
        source_status = (jobs.get(job_id) or {}).get('status')
        try:
            with bind_run_actor(principal):
                durable_dispatch = (
                    app.state.job_backend == 'redis'
                    and (
                        hasattr(jobs, 'prepare_durable_retry')
                        or hasattr(jobs, 'prepare_retry')
                    )
                    and hasattr(database, 'stage_job')
                )
                record = (
                    (
                        jobs.prepare_durable_retry
                        if hasattr(jobs, 'prepare_durable_retry')
                        else jobs.prepare_retry
                    )(
                        job_id,
                        migrate_implementation=migrate_implementation,
                    )
                    if durable_dispatch
                    else jobs.retry(job_id)
                )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if durable_dispatch:
            durable_reader = getattr(jobs, 'durable_record', None)
            durable = (
                record
                if record.get('_execution_key')
                else durable_reader(record['job_id']) if durable_reader else None
            )
            await database.stage_job(
                durable or record,
                project_id=project_id,
                ownership_created_at=datetime.now(timezone.utc).isoformat(),
            )
            record = await database.get_job(record['job_id']) or record
        else:
            if project_id:
                await database.upsert_job_with_project(
                    record,
                    project_id,
                    datetime.now(timezone.utc).isoformat(),
                )
            else:
                await database.upsert_job(record)
        if project_id:
            record = await expose_job(record)
        JOB_SUBMISSIONS.labels(record['tool']).inc()
        JOB_STATUS.labels(record['tool'], record['status']).set(1)
        await audit.record(
            principal,
            'job.retry',
            'job',
            record['job_id'],
            {
                'retry_of': job_id,
                'migrate_implementation': migrate_implementation,
                'source_status': source_status,
            },
        )
        return {'status': 'accepted', 'job': record}

def register_job_routes(
    app,
    *,
    jobs,
    database,
    audit,
    output_root,
    require_permission,
    project_access,
    job_access,
    expose_job,
    issue_stream_ticket,
    stream_ticket_ttl,
    stream_principal,
    require_project_ownership=False,
    allow_legacy_artifact_paths=True,
):
    async def canonicalize_artifacts(record):
        if record is None or record.get('status') != 'completed':
            return record
        artifacts = await database.list_job_artifacts(
            record['job_id'],
            statuses={'committed'},
        )
        return {**record, 'artifacts': artifacts}

    async def canonicalize_artifact_batch(records):
        completed_ids = [
            record['job_id']
            for record in records
            if record is not None and record.get('status') == 'completed'
        ]
        manifests = await database.list_job_artifacts_for_jobs(
            completed_ids,
            statuses={'committed'},
        )
        return [
            {
                **record,
                'artifacts': manifests.get(record['job_id'], []),
            }
            if record is not None and record.get('status') == 'completed'
            else record
            for record in records
            if record is not None
        ]

    async def read_job(job_id):
        if app.state.job_backend == 'redis':
            try:
                record = jobs.get(job_id)
            except Exception:
                record = None
            if record is not None:
                JOB_STATUS.labels(record['tool'], record['status']).set(1)
                return await canonicalize_artifacts(await expose_job(record))
            record = await database.get_job(job_id)
            if record is not None:
                JOB_STATUS.labels(record['tool'], record['status']).set(1)
            return await canonicalize_artifacts(await expose_job(record))
        record = jobs.get(job_id)
        if record is not None:
            await database.upsert_job(record)
            JOB_STATUS.labels(record['tool'], record['status']).set(1)
            return await canonicalize_artifacts(await expose_job(record))
        return await canonicalize_artifacts(
            await expose_job(await database.get_job(job_id))
        )

    _register_job_query_routes(
        app,
        jobs=jobs,
        database=database,
        require_permission=require_permission,
        project_access=project_access,
        job_access=job_access,
        expose_job=expose_job,
        read_job=read_job,
        canonicalize_artifact_batch=canonicalize_artifact_batch,
    )

    @app.get('/api/v1/workers', tags=['jobs'])
    async def list_workers(
        _principal: Principal = Depends(require_permission('jobs:read')),
    ):
        reader = getattr(jobs, 'list_workers', None)
        workers = await asyncio.to_thread(reader) if reader is not None else []
        return {'status': 'ok', 'workers': workers}

    @app.post('/api/v1/jobs', status_code=202, tags=['jobs'])
    async def submit_job(
        payload: JobCreate,
        idempotency_key: str | None = Header(
            default=None,
            alias='Idempotency-Key',
        ),
        principal: Principal = Depends(require_permission('jobs:write')),
    ):
        if not payload.project_id and (
            require_project_ownership or 'admin' not in principal.roles
        ):
            raise HTTPException(
                status_code=422,
                detail='project_id is required for job submission',
            )
        if payload.project_id:
            await project_access(
                payload.project_id,
                principal,
                {'owner', 'editor'},
            )
        effective_project_id = payload.project_id or 'system-legacy'
        try:
            scoped_idempotency_key = _scoped_idempotency_key(
                principal.subject,
                effective_project_id,
                idempotency_key,
            )
            with bind_run_actor(principal):
                durable_dispatch = (
                    app.state.job_backend == 'redis'
                    and (
                        hasattr(jobs, 'prepare_durable')
                        or hasattr(jobs, 'prepare')
                    )
                    and hasattr(database, 'stage_job')
                )
                submitter = (
                    jobs.prepare_durable
                    if durable_dispatch and hasattr(jobs, 'prepare_durable')
                    else jobs.prepare if durable_dispatch else jobs.submit
                )
                record = submitter(
                    payload.tool,
                    payload.arguments,
                    idempotency_key=scoped_idempotency_key,
                    resources=(
                        payload.resources.model_dump()
                        if payload.resources is not None
                        else None
                    ),
                    priority=payload.priority,
                    project_id=effective_project_id,
                )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if durable_dispatch:
            durable_reader = getattr(jobs, 'durable_record', None)
            durable = (
                record
                if record.get('_execution_key')
                else durable_reader(record['job_id']) if durable_reader else None
            )
            durable = durable or record
            try:
                staged = await database.stage_job(
                    durable,
                    project_id=effective_project_id,
                    ownership_created_at=datetime.now(timezone.utc).isoformat(),
                    idempotency_subject=(
                        principal.subject if scoped_idempotency_key else None
                    ),
                    idempotency_key=scoped_idempotency_key,
                    idempotency_payload_hash=(
                        _job_payload_hash(durable)
                        if scoped_idempotency_key else None
                    ),
                ) or {
                    'job_id': record['job_id'],
                    'deduplicated': False,
                }
            except (TypeError, ValueError) as exc:
                discard = getattr(jobs, 'discard_prepared', None)
                if discard is not None:
                    discard(record['job_id'], scoped_idempotency_key)
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except Exception:
                discard = getattr(jobs, 'discard_prepared', None)
                if discard is not None:
                    discard(record['job_id'], scoped_idempotency_key)
                raise
            if staged.get('deduplicated'):
                existing_job_id = staged['job_id']
                if existing_job_id != record['job_id']:
                    discard = getattr(jobs, 'discard_prepared', None)
                    if discard is not None:
                        discard(
                            record['job_id'],
                            scoped_idempotency_key,
                            existing_job_id,
                        )
                existing = await database.get_job(existing_job_id)
                if existing is None:
                    raise RuntimeError('idempotent job record is unavailable')
                record = {**existing, 'deduplicated': True}
            else:
                record = await database.get_job(record['job_id']) or record
        else:
            await database.upsert_job_with_project(
                record,
                effective_project_id,
                datetime.now(timezone.utc).isoformat(),
            )
        record = await expose_job(record)
        if not record.get('deduplicated'):
            JOB_SUBMISSIONS.labels(payload.tool).inc()
        JOB_STATUS.labels(payload.tool, record['status']).set(1)
        await audit.record(
            principal,
            'job.submit',
            'job',
            record['job_id'],
            {
                'tool': payload.tool,
                'resources': record.get('resources'),
                'priority': record.get('priority', 0),
                'deduplicated': bool(record.get('deduplicated')),
                'project_id': effective_project_id,
            },
        )
        response_status = (
            'deduplicated' if record.get('deduplicated') else 'accepted'
        )
        return {'status': response_status, 'job': record}

    _register_job_event_routes(
        app,
        jobs=jobs,
        database=database,
        output_root=output_root,
        audit=audit,
        require_permission=require_permission,
        job_access=job_access,
        issue_stream_ticket=issue_stream_ticket,
        stream_ticket_ttl=stream_ticket_ttl,
        stream_principal=stream_principal,
        read_job=read_job,
        allow_legacy_artifact_paths=allow_legacy_artifact_paths,
    )
    _register_job_mutation_routes(
        app,
        jobs=jobs,
        database=database,
        audit=audit,
        require_permission=require_permission,
        job_access=job_access,
        expose_job=expose_job,
    )

    return JobRouteHandlers(read_job=read_job, submit_job=submit_job)
