"""Run manifest and research file routes for the HTTP API."""

from datetime import datetime, timezone

from fastapi import Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse

try:
    from .api_server import list_run_manifests
    from .auth import Principal
    from .observability import FILE_OPERATIONS, FILE_UPLOAD_BYTES
except ImportError:
    from api_server import list_run_manifests
    from auth import Principal
    from observability import FILE_OPERATIONS, FILE_UPLOAD_BYTES


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
        if project_id:
            await project_access(
                project_id,
                principal,
                {'owner', 'editor'},
            )
        try:
            stored = await storage.save(upload)
        except ValueError as exc:
            FILE_OPERATIONS.labels(
                app.state.storage_backend,
                'upload',
                'rejected',
            ).inc()
            audit.record(
                principal,
                'file.upload_rejected',
                'file',
                None,
                {
                    'filename': storage._safe_filename(upload.filename),
                    'reason': str(exc),
                },
            )
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception:
            FILE_OPERATIONS.labels(
                app.state.storage_backend,
                'upload',
                'error',
            ).inc()
            raise
        finally:
            await upload.close()
        FILE_OPERATIONS.labels(
            app.state.storage_backend,
            'upload',
            'success',
        ).inc()
        FILE_UPLOAD_BYTES.labels(app.state.storage_backend).inc(
            stored.size_bytes
        )
        audit.record(
            principal,
            'file.upload',
            'file',
            stored.file_id,
            {
                'filename': stored.filename,
                'size_bytes': stored.size_bytes,
                'sha256': stored.sha256,
            },
        )
        if project_id:
            await database.assign_file_project(
                file_id=stored.file_id,
                project_id=project_id,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        return {
            'status': 'uploaded',
            'file': {
                **storage.payload(
                    stored,
                    project_root,
                    f'/api/v1/files/{stored.file_id}',
                ),
                'project_id': project_id,
            },
        }

    @app.get('/api/v1/files/{file_id}', tags=['files'])
    async def download_file(
        file_id: str,
        principal: Principal = Depends(require_permission('files:read')),
    ):
        project_id = await database.get_file_project(file_id)
        if project_id:
            await project_access(
                project_id,
                principal,
                {'owner', 'editor', 'viewer'},
            )
        try:
            async_get = getattr(storage, 'aget', None)
            stored = await async_get(file_id) if async_get else storage.get(file_id)
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
        audit.record(
            principal,
            'file.download',
            'file',
            file_id,
            {'filename': stored.filename},
        )
        return FileResponse(
            stored.path,
            media_type=stored.content_type,
            filename=stored.filename,
            headers={'X-File-SHA256': stored.sha256},
        )

    return runs, upload_file, download_file
