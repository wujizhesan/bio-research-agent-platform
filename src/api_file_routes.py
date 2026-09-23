"""Run manifest and research file routes for the HTTP API."""

import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Depends, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from starlette.background import BackgroundTask

try:
    from .api_server import list_run_manifests
    from .auth import Principal
    from .observability import FILE_OPERATIONS, FILE_UPLOAD_BYTES
    from .storage_workspace import StorageIntegrityError
except ImportError:
    from api_server import list_run_manifests
    from auth import Principal
    from observability import FILE_OPERATIONS, FILE_UPLOAD_BYTES
    from storage_workspace import StorageIntegrityError


def register_file_routes(
    app,
    *,
    storage,
    database,
    audit,
    project_root,
    output_root,
    require_permission,
    project_access,
    require_project_ownership=False,
):
    @app.get(
        '/api/v1/runs',
        dependencies=[Depends(require_permission('runs:read'))],
        tags=['runs'],
    )
    async def runs(limit: int = Query(default=20, ge=1, le=100)):
        return {
            'status': 'ok',
            'runs': list_run_manifests(output_root, limit),
        }

    @app.post('/api/v1/files', status_code=201, tags=['files'])
    async def upload_file(
        upload: UploadFile = File(...),
        project_id: str | None = Form(default=None, min_length=1, max_length=64),
        principal: Principal = Depends(require_permission('files:write')),
    ):
        if not project_id and (
            require_project_ownership or 'admin' not in principal.roles
        ):
            raise HTTPException(
                status_code=422,
                detail='project_id is required for file upload',
            )
        if project_id:
            await project_access(
                project_id,
                principal,
                {'owner', 'editor'},
            )
        effective_project_id = project_id or 'system-legacy'
        file_id = uuid4().hex
        filename = storage._safe_filename(upload.filename)
        created_at = datetime.now(timezone.utc).isoformat()
        storage_key = storage.planned_storage_key(file_id, filename)
        declared_size = getattr(upload, 'size', None)
        reservation_bytes = (
            max(int(declared_size), 1)
            if declared_size is not None
            else storage.max_bytes
        )

        async def record_failed_upload(reason):
            try:
                await database.fail_file_upload(
                    file_id,
                    str(reason),
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                return

        try:
            if reservation_bytes > storage.max_bytes:
                raise ValueError(
                    f'file exceeds maximum size of {storage.max_bytes} bytes'
                )
            await database.begin_file_upload(
                file_id=file_id,
                project_id=effective_project_id,
                filename=filename,
                storage_backend=app.state.storage_backend,
                storage_key=storage_key,
                reserved_bytes=reservation_bytes,
                quota_bytes=storage.total_quota_bytes,
                created_at=created_at,
            )
            await database.mark_file_uploading(file_id, created_at)
            stored = await storage.save(upload, file_id=file_id)
        except ValueError as exc:
            try:
                await database.discard_failed_file_upload(
                    file_id,
                    str(exc),
                    datetime.now(timezone.utc).isoformat(),
                )
            except Exception:
                await record_failed_upload(exc)
            FILE_OPERATIONS.labels(
                app.state.storage_backend,
                'upload',
                'rejected',
            ).inc()
            await audit.record(
                principal,
                'file.upload_rejected',
                'file',
                None,
                {
                    'filename': filename,
                    'reason': str(exc),
                },
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            await record_failed_upload(exc)
            FILE_OPERATIONS.labels(
                app.state.storage_backend,
                'upload',
                'error',
            ).inc()
            raise
        finally:
            await upload.close()
        try:
            await database.activate_file_upload(
                file_id,
                {
                    'filename': stored.filename,
                    'storage_key': stored.storage_key,
                    'version_id': stored.version_id,
                    'sha256': stored.sha256,
                    'size_bytes': stored.size_bytes,
                },
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as exc:
            rollback_status = 'discarded'
            try:
                await storage.discard(stored)
            except Exception as rollback_exc:
                rollback_status = 'orphaned'
                await record_failed_upload(rollback_exc)
            else:
                try:
                    await database.discard_failed_file_upload(
                        file_id,
                        str(exc),
                        datetime.now(timezone.utc).isoformat(),
                    )
                except Exception:
                    rollback_status = 'accounting_pending'
            FILE_OPERATIONS.labels(
                app.state.storage_backend,
                'upload',
                'ownership_error',
            ).inc()
            await audit.record(
                principal,
                'file.upload_ownership_failed',
                'file',
                stored.file_id,
                {
                    'project_id': effective_project_id,
                    'rollback_status': rollback_status,
                },
            )
            raise HTTPException(
                status_code=503,
                detail='file ownership could not be committed',
            ) from exc
        FILE_OPERATIONS.labels(
            app.state.storage_backend,
            'upload',
            'success',
        ).inc()
        FILE_UPLOAD_BYTES.labels(app.state.storage_backend).inc(
            stored.size_bytes
        )
        await audit.record(
            principal,
            'file.upload',
            'file',
            stored.file_id,
            {
                'filename': stored.filename,
                'size_bytes': stored.size_bytes,
                'sha256': stored.sha256,
                'security': stored.security,
                'project_id': effective_project_id,
            },
        )
        return {
            'status': 'uploaded',
            'file': {
                **storage.payload(
                    stored,
                    project_root,
                    f'/api/v1/files/{stored.file_id}',
                ),
                'project_id': effective_project_id,
            },
        }

    @app.delete('/api/v1/files/{file_id}', status_code=202, tags=['files'])
    async def delete_file(
        file_id: str,
        idempotency_key: str | None = Header(
            default=None,
            alias='Idempotency-Key',
            max_length=128,
        ),
        principal: Principal = Depends(require_permission('files:write')),
    ):
        file_record = await database.get_file_record(file_id)
        if file_record is None:
            raise HTTPException(status_code=404, detail=f'file not found: {file_id}')
        await project_access(file_record['project_id'], principal, {'owner'})
        if idempotency_key is not None:
            normalized = idempotency_key.strip()
            if not normalized:
                raise HTTPException(
                    status_code=400,
                    detail='idempotency key must be a non-empty string',
                )
            scope = json.dumps(
                [principal.subject, file_id, normalized],
                ensure_ascii=False,
                separators=(',', ':'),
            ).encode('utf-8')
            request_id = f'scoped:{hashlib.sha256(scope).hexdigest()}'
        else:
            request_id = uuid4().hex
        try:
            file_record = await database.request_file_deletion(
                file_id,
                principal.subject,
                request_id,
                datetime.now(timezone.utc).isoformat(),
            )
        except ValueError as exc:
            if str(exc) == 'file upload not found':
                raise HTTPException(
                    status_code=404,
                    detail=f'file not found: {file_id}',
                ) from exc
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        FILE_OPERATIONS.labels(
            app.state.storage_backend,
            'delete',
            'requested',
        ).inc()
        await audit.record(
            principal,
            'file.delete_requested',
            'file',
            file_id,
            {
                'project_id': file_record['project_id'],
                'request_id': file_record.get('delete_request_id'),
                'status': file_record.get('status'),
            },
        )
        return {
            'status': 'deletion_requested',
            'file': file_record,
        }

    @app.get('/api/v1/files/{file_id}/deletion', tags=['files'])
    async def file_deletion_status(
        file_id: str,
        principal: Principal = Depends(require_permission('files:read')),
    ):
        file_record = await database.get_file_record(file_id)
        if file_record is None or not file_record.get('delete_request_id'):
            raise HTTPException(status_code=404, detail=f'file deletion not found: {file_id}')
        await project_access(file_record['project_id'], principal, {'owner'})
        events = await database.list_storage_deletion_events('file', file_id)
        return {'status': 'ok', 'file': file_record, 'events': events}

    @app.post(
        '/api/v1/files/{file_id}/deletion/retry',
        status_code=202,
        tags=['files'],
    )
    async def retry_file_deletion(
        file_id: str,
        idempotency_key: str | None = Header(
            default=None,
            alias='Idempotency-Key',
            max_length=128,
        ),
        principal: Principal = Depends(require_permission('files:write')),
    ):
        file_record = await database.get_file_record(file_id)
        if file_record is None or not file_record.get('delete_request_id'):
            raise HTTPException(status_code=404, detail=f'file deletion not found: {file_id}')
        await project_access(file_record['project_id'], principal, {'owner'})
        normalized = str(idempotency_key or '').strip()
        request_id = uuid4().hex
        if idempotency_key is not None:
            if not normalized:
                raise HTTPException(
                    status_code=400,
                    detail='idempotency key must be a non-empty string',
                )
            scope = json.dumps(
                [principal.subject, file_id, 'retry', normalized],
                ensure_ascii=False,
                separators=(',', ':'),
            ).encode('utf-8')
            request_id = f'scoped:{hashlib.sha256(scope).hexdigest()}'
        try:
            file_record = await database.retry_file_deletion(
                file_id,
                principal.subject,
                request_id,
                datetime.now(timezone.utc).isoformat(),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=404,
                detail=f'file deletion not found: {file_id}',
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        deduplicated = bool(file_record.pop('_deduplicated', False))
        if not deduplicated:
            await audit.record(
                principal,
                'file.delete_retried',
                'file',
                file_id,
                {'project_id': file_record['project_id'], 'request_id': request_id},
            )
        return {'status': 'deletion_requested', 'file': file_record}

    @app.get('/api/v1/files/{file_id}', tags=['files'])
    async def download_file(
        file_id: str,
        storage_reference: str | None = Query(default=None, max_length=4096),
        principal: Principal = Depends(require_permission('files:read')),
    ):
        file_record = await database.get_file_record(file_id)
        project_id = (
            file_record['project_id']
            if file_record is not None
            else await database.get_file_project(file_id)
        )
        if not project_id:
            if 'admin' not in principal.roles:
                raise HTTPException(
                    status_code=403,
                    detail='unscoped file access denied',
                )
        else:
            await project_access(
                project_id,
                principal,
                {'owner', 'editor', 'viewer'},
            )
        if file_record is not None and file_record['status'] != 'active':
            raise HTTPException(status_code=404, detail=f'file not found: {file_id}')
        try:
            async_get = getattr(storage, 'aget', None)
            stored = (
                await async_get(file_id, reference=storage_reference)
                if async_get else storage.get(file_id, reference=storage_reference)
            )
        except FileNotFoundError as exc:
            FILE_OPERATIONS.labels(
                app.state.storage_backend,
                'download',
                'not_found',
            ).inc()
            raise HTTPException(
                status_code=404,
                detail=f'file not found: {file_id}',
            ) from exc
        except StorageIntegrityError as exc:
            FILE_OPERATIONS.labels(
                app.state.storage_backend,
                'download',
                'integrity_error',
            ).inc()
            raise HTTPException(
                status_code=502,
                detail='stored file failed integrity verification',
            ) from exc
        except Exception:
            FILE_OPERATIONS.labels(
                app.state.storage_backend,
                'download',
                'error',
            ).inc()
            raise
        FILE_OPERATIONS.labels(
            app.state.storage_backend,
            'download',
            'success',
        ).inc()
        await audit.record(
            principal,
            'file.download',
            'file',
            file_id,
            {'filename': stored.filename},
        )
        release = getattr(storage, 'release', None)
        return FileResponse(
            stored.path,
            media_type=stored.content_type,
            filename=stored.filename,
            headers={
                'X-File-SHA256': stored.sha256,
                **(
                    {'X-File-Version': stored.version_id}
                    if stored.version_id else {}
                ),
            },
            background=BackgroundTask(release, stored) if release else None,
        )

    return runs, upload_file, download_file
