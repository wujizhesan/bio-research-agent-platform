"""FastAPI service adapter for the pluggable research Agent platform."""
from contextlib import asynccontextmanager
import argparse
import json
import os
import asyncio
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

try:
    from .api_server import list_run_manifests
    from .audit_log import AuditLogger
    from .auth import AuthService, AuthenticationError, Principal
    from .database import Database
    from .domain_registry import active_tool_specs
    from .file_storage import LocalFileStorage
    from .job_manager import JobManager
    from .plugin_manager import PluginManager
    from .observability import HTTP_LATENCY, HTTP_REQUESTS, JOB_STATUS, JOB_SUBMISSIONS
    from .redis_job_manager import RedisJobManager
except ImportError:
    from api_server import list_run_manifests
    from audit_log import AuditLogger
    from auth import AuthService, AuthenticationError, Principal
    from database import Database
    from domain_registry import active_tool_specs
    from file_storage import LocalFileStorage
    from job_manager import JobManager
    from plugin_manager import PluginManager
    from observability import HTTP_LATENCY, HTTP_REQUESTS, JOB_STATUS, JOB_SUBMISSIONS
    from redis_job_manager import RedisJobManager


API_NAME = 'bio-research-agent-api'
API_VERSION = '0.2.0'
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / 'output'
ARTIFACT_RESULT_KEYS = frozenset({
    'output_csv', 'output_md', 'output_html', 'result_csv', 'report',
    'manifest_path', 'report_path', 'variant_output_csv', 'sequence_report_path',
})


class JobCreate(BaseModel):
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


class PluginStateUpdate(BaseModel):
    enabled: bool


oauth2_scheme = OAuth2PasswordBearer(tokenUrl='/api/v1/auth/token', auto_error=False)


def _authorized(request: Request, authorization=None):
    if request.url.path == '/health':
        return True
    try:
        AuthService.from_env().authenticate(authorization)
    except (AuthenticationError, ValueError):
        return False
    return True


async def require_auth(request: Request, authorization: str | None = Header(default=None)):
    if not _authorized(request, authorization):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail='authentication required',
            headers={'WWW-Authenticate': 'Bearer'},
        )


def _public_specs(domain=None):
    selected = None if domain in (None, '', 'all') else domain
    return [
        {key: value for key, value in spec.items() if key not in {'function'}}
        for spec in active_tool_specs(selected)
    ]


def _iter_artifact_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ARTIFACT_RESULT_KEYS and isinstance(item, str):
                yield item
            yield from _iter_artifact_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_artifact_values(item)


def _resolve_artifact_path(raw_path, output_root):
    raw = Path(raw_path)
    candidates = [raw] if raw.is_absolute() else [PROJECT_ROOT / raw, output_root / raw]
    root = output_root.resolve()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved == root or root not in resolved.parents:
            continue
        if resolved.is_file():
            return resolved
    return None


def create_app(job_manager=None, plugin_manager=None, database=None, file_storage=None, audit_log=None):
    owned_job_manager = job_manager is None
    owned_plugin_manager = plugin_manager is None
    owned_database = database is None
    if job_manager is not None:
        jobs = job_manager
    elif os.environ.get('JOB_BACKEND', 'local').lower() == 'redis':
        jobs = RedisJobManager(
            redis_url=os.environ.get('REDIS_URL', 'redis://127.0.0.1:6379/0'),
            namespace=os.environ.get('REDIS_NAMESPACE', 'bioagent'),
        )
    else:
        jobs = JobManager(store_path=OUTPUT_ROOT / 'jobs.sqlite3')
    plugins = plugin_manager or PluginManager(state_path=OUTPUT_ROOT / 'plugin_state.json')
    db = database or Database()
    auth = AuthService.from_env()
    audit = audit_log or AuditLogger(OUTPUT_ROOT / 'audit.jsonl')
    configured_upload_root = os.environ.get('UPLOAD_ROOT')
    upload_root = Path(configured_upload_root) if configured_upload_root else OUTPUT_ROOT / 'uploads'
    if not upload_root.is_absolute():
        upload_root = PROJECT_ROOT / upload_root
    storage = file_storage or LocalFileStorage(
        upload_root,
        max_bytes=int(os.environ.get('UPLOAD_MAX_BYTES', str(50 * 1024 * 1024))),
    )

    @asynccontextmanager
    async def lifespan(_app):
        await db.init_schema()
        yield
        if owned_job_manager:
            jobs.shutdown()
        if owned_database:
            await db.close()

    app = FastAPI(
        title='Bio Research Agent API',
        version=API_VERSION,
        description='Async API for pluggable CADD, omics, sequence and research workflows.',
        lifespan=lifespan,
    )
    app.state.job_manager = jobs
    app.state.job_backend = getattr(jobs, 'backend', 'local')
    app.state.plugin_manager = plugins
    app.state.database = db
    app.state.file_storage = storage
    app.state.audit_log = audit
    app.state.auth_service = auth
    origins = [item.strip() for item in os.environ.get(
        'CORS_ORIGINS',
        'http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174',
    ).split(',') if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=['GET', 'POST'],
        allow_headers=['Authorization', 'Content-Type', 'Idempotency-Key'],
    )

    @app.middleware('http')
    async def metrics_middleware(request: Request, call_next):
        from time import perf_counter
        started = perf_counter()
        response = None
        try:
            response = await call_next(request)
            return response
        finally:
            if request.url.path != '/metrics':
                route = request.scope.get('route')
                path = getattr(route, 'path', request.url.path)
                status_code = str(response.status_code if response is not None else 500)
                HTTP_REQUESTS.labels(request.method, path, status_code).inc()
                HTTP_LATENCY.labels(request.method, path).observe(perf_counter() - started)

    async def current_principal(token: str | None = Depends(oauth2_scheme)):
        authorization = f'Bearer {token}' if token else None
        try:
            return auth.authenticate(authorization)
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={'WWW-Authenticate': 'Bearer'},
            ) from exc

    def require_permission(permission):
        async def dependency(principal: Principal = Depends(current_principal)):
            if not auth.has_permission(principal, permission):
                raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail='insufficient permissions')
            return principal
        return dependency

    @app.get('/health', tags=['system'])
    async def health():
        try:
            await db.ping()
        except Exception as exc:
            return JSONResponse(status_code=503, content={'status': 'degraded', 'database': 'unavailable', 'error': str(exc)})
        return {
            'status': 'ok',
            'service': API_NAME,
            'version': API_VERSION,
            'database': 'ok',
            'job_backend': app.state.job_backend,
        }

    @app.post('/api/v1/auth/token', tags=['auth'])
    async def issue_token(form_data: OAuth2PasswordRequestForm = Depends()):
        try:
            payload = auth.issue_token(form_data.username, form_data.password)
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={'WWW-Authenticate': 'Bearer'},
            ) from exc
        principal_data = payload['principal']
        audit.record(
            Principal(principal_data['sub'], tuple(principal_data['roles']), principal_data['auth_type']),
            'auth.login',
            'auth',
            metadata={'auth_type': 'jwt'},
        )
        return payload

    @app.get('/metrics', dependencies=[Depends(require_permission('metrics:read'))], tags=['system'])
    async def metrics():
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get('/api/v1/plugins', dependencies=[Depends(require_permission('catalog:read'))], tags=['catalog'])
    async def plugins_catalog():
        return {'status': 'ok', 'plugins': plugins.list()}

    @app.get('/api/v1/tools', dependencies=[Depends(require_permission('catalog:read'))], tags=['catalog'])
    async def tools_catalog(domain: str = Query(default='all')):
        try:
            return {'status': 'ok', 'tools': _public_specs(domain)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get('/api/v1/runs', dependencies=[Depends(require_permission('runs:read'))], tags=['runs'])
    async def runs(limit: int = Query(default=20, ge=1, le=100)):
        return {'status': 'ok', 'runs': list_run_manifests(OUTPUT_ROOT, limit)}

    @app.post('/api/v1/files', status_code=201, tags=['files'])
    async def upload_file(
        upload: UploadFile = File(...),
        principal: Principal = Depends(require_permission('files:write')),
    ):
        try:
            stored = await storage.save(upload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        finally:
            await upload.close()
        audit.record(
            principal,
            'file.upload',
            'file',
            stored.file_id,
            {'filename': stored.filename, 'size_bytes': stored.size_bytes, 'sha256': stored.sha256},
        )
        return {
            'status': 'uploaded',
            'file': storage.payload(stored, PROJECT_ROOT, f'/api/v1/files/{stored.file_id}'),
        }

    @app.get('/api/v1/files/{file_id}', tags=['files'])
    async def download_file(file_id: str, principal: Principal = Depends(require_permission('files:read'))):
        try:
            stored = storage.get(file_id)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=404, detail=f'file not found: {file_id}') from exc
        audit.record(principal, 'file.download', 'file', file_id, {'filename': stored.filename})
        return FileResponse(
            stored.path,
            media_type=stored.content_type,
            filename=stored.filename,
            headers={'X-File-SHA256': stored.sha256},
        )

    async def read_job(job_id):
        if app.state.job_backend == 'redis':
            try:
                record = jobs.get(job_id)
            except Exception:
                record = None
            if record is not None:
                JOB_STATUS.labels(record['tool'], record['status']).set(1)
                return record
            record = await db.get_job(job_id)
            if record is not None:
                JOB_STATUS.labels(record['tool'], record['status']).set(1)
            return record
        record = jobs.get(job_id)
        if record is not None:
            await db.upsert_job(record)
            JOB_STATUS.labels(record['tool'], record['status']).set(1)
            return record
        return await db.get_job(job_id)

    @app.post('/api/v1/jobs', status_code=202, tags=['jobs'])
    async def submit_job(
        payload: JobCreate,
        idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
        principal: Principal = Depends(require_permission('jobs:write')),
    ):
        try:
            record = jobs.submit(payload.tool, payload.arguments, idempotency_key=idempotency_key)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.upsert_job(record)
        if not record.get('deduplicated'):
            JOB_SUBMISSIONS.labels(payload.tool).inc()
        JOB_STATUS.labels(payload.tool, record['status']).set(1)
        audit.record(
            principal,
            'job.submit',
            'job',
            record['job_id'],
            {'tool': payload.tool, 'deduplicated': bool(record.get('deduplicated'))},
        )
        return {'status': 'deduplicated' if record.get('deduplicated') else 'accepted', 'job': record}

    @app.get('/api/v1/jobs', dependencies=[Depends(require_permission('jobs:read'))], tags=['jobs'])
    async def list_jobs(limit: int = Query(default=20, ge=1, le=100)):
        if app.state.job_backend == 'redis':
            records = await db.list_jobs(limit)
            if records:
                for record in records:
                    JOB_STATUS.labels(record['tool'], record['status']).set(1)
                return {'status': 'ok', 'jobs': records}
        records = jobs.list(limit)
        for record in records:
            await db.upsert_job(record)
            JOB_STATUS.labels(record['tool'], record['status']).set(1)
        return {'status': 'ok', 'jobs': records}

    @app.get('/api/v1/jobs/{job_id}', dependencies=[Depends(require_permission('jobs:read'))], tags=['jobs'])
    async def get_job(job_id: str):
        record = await read_job(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')
        return {'status': 'ok', 'job': record}

    @app.get('/api/v1/jobs/{job_id}/artifacts', tags=['jobs'])
    async def download_job_artifact(
        job_id: str,
        artifact_path: str = Query(min_length=1, alias='path'),
        principal: Principal = Depends(require_permission('jobs:read')),
    ):
        record = await read_job(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')
        allowed_paths = {
            _resolve_artifact_path(path, OUTPUT_ROOT)
            for path in _iter_artifact_values(record.get('result'))
        }
        target = _resolve_artifact_path(artifact_path, OUTPUT_ROOT)
        if target is None or target not in allowed_paths:
            raise HTTPException(status_code=404, detail='artifact not found for job')
        audit.record(principal, 'job.artifact_download', 'job', job_id, {'filename': target.name})
        return FileResponse(target, filename=target.name, headers={'X-Job-ID': job_id})

    @app.get('/api/v1/jobs/{job_id}/events', dependencies=[Depends(require_permission('jobs:read'))], tags=['jobs'])
    async def job_events(
        job_id: str,
        interval_seconds: float = Query(default=0.2, ge=0.05, le=5),
        timeout_seconds: float = Query(default=60, ge=1, le=300),
    ):
        if await read_job(job_id) is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')
        subscriber = None
        if app.state.job_backend == 'redis':
            subscribe = getattr(jobs, 'subscribe_job_events', None)
            if subscribe is not None:
                subscriber = subscribe(job_id)

        async def stream():
            last_signature = None
            deadline = monotonic() + timeout_seconds
            try:
                while True:
                    record = None
                    if subscriber is not None:
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
                        payload = {'status': 'error', 'error': f'job not found: {job_id}'}
                        yield f'event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                        return
                    signature = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                    emitted = signature != last_signature
                    if emitted:
                        payload = {'status': 'ok', 'job': record}
                        yield f'event: job\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n'
                        last_signature = signature
                    if record.get('status') in {'completed', 'failed', 'cancelled'}:
                        return
                    if monotonic() >= deadline:
                        payload = {'status': 'timeout', 'job': record}
                        yield f'event: timeout\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n'
                        return
                    if not emitted:
                        yield ': keep-alive\n\n'
                    if subscriber is None:
                        await asyncio.sleep(interval_seconds)
            finally:
                if subscriber is not None:
                    await asyncio.to_thread(subscriber.close)

        return StreamingResponse(
            stream(),
            media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    @app.post('/api/v1/jobs/{job_id}/cancel', status_code=202, tags=['jobs'])
    async def cancel_job(job_id: str, principal: Principal = Depends(require_permission('jobs:write'))):
        try:
            record = jobs.cancel(job_id)
        except ValueError as exc:
            status_code = 404 if str(exc).startswith('job not found:') else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        await db.upsert_job(record)
        JOB_STATUS.labels(record['tool'], record['status']).set(1)
        audit.record(principal, 'job.cancel', 'job', job_id, {'status': record['status']})
        response_status = 'already_terminal' if record['status'] in {'completed', 'failed', 'cancelled'} else 'cancelled' if record['status'] == 'cancelled' else 'cancellation_requested'
        return {'status': response_status, 'job': record}

    @app.post('/api/v1/jobs/{job_id}/retry', status_code=202, tags=['jobs'])
    async def retry_job(job_id: str, principal: Principal = Depends(require_permission('jobs:write'))):
        try:
            record = jobs.retry(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.upsert_job(record)
        JOB_SUBMISSIONS.labels(record['tool']).inc()
        JOB_STATUS.labels(record['tool'], record['status']).set(1)
        audit.record(principal, 'job.retry', 'job', record['job_id'], {'retry_of': job_id})
        return {'status': 'accepted', 'job': record}

    @app.post('/api/v1/plugins/{domain}/state', tags=['catalog'])
    async def update_plugin_state(
        domain: str,
        payload: PluginStateUpdate,
        principal: Principal = Depends(require_permission('plugins:write')),
    ):
        try:
            plugin = plugins.set_enabled(domain, payload.enabled)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit.record(principal, 'plugin.state_change', 'plugin', domain, {'enabled': payload.enabled})
        return {'status': 'ok', 'plugin': plugin}

    return app


app = create_app()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the FastAPI research Agent service')
    parser.add_argument('--host', default=os.environ.get('API_HOST', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=int(os.environ.get('API_PORT', '8000')))
    args = parser.parse_args(argv)
    import uvicorn
    uvicorn.run(app, host=args.host, port=args.port)


if __name__ == '__main__':
    main()
