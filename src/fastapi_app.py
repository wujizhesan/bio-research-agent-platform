"""FastAPI service adapter for the pluggable research Agent platform."""
from contextlib import asynccontextmanager
import argparse
import hmac
import json
import os
import asyncio
from pathlib import Path
from time import monotonic
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

try:
    from .api_server import list_run_manifests
    from .database import Database
    from .domain_registry import active_tool_specs
    from .job_manager import JobManager
    from .plugin_manager import PluginManager
    from .observability import HTTP_LATENCY, HTTP_REQUESTS, JOB_STATUS, JOB_SUBMISSIONS
except ImportError:
    from api_server import list_run_manifests
    from database import Database
    from domain_registry import active_tool_specs
    from job_manager import JobManager
    from plugin_manager import PluginManager
    from observability import HTTP_LATENCY, HTTP_REQUESTS, JOB_STATUS, JOB_SUBMISSIONS


API_NAME = 'bio-research-agent-api'
API_VERSION = '0.2.0'
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / 'output'


class JobCreate(BaseModel):
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)


def _authorized(request: Request, authorization=None):
    configured = os.environ.get('CADD_API_TOKEN')
    if not configured or request.url.path == '/health':
        return True
    scheme, _, supplied = (authorization or '').partition(' ')
    return scheme.lower() == 'bearer' and bool(supplied.strip()) and hmac.compare_digest(
        supplied.strip(), configured
    )


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


def create_app(job_manager=None, plugin_manager=None, database=None):
    owned_job_manager = job_manager is None
    owned_plugin_manager = plugin_manager is None
    owned_database = database is None
    jobs = job_manager or JobManager(store_path=OUTPUT_ROOT / 'jobs.sqlite3')
    plugins = plugin_manager or PluginManager(state_path=OUTPUT_ROOT / 'plugin_state.json')
    db = database or Database()

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
    app.state.plugin_manager = plugins
    app.state.database = db
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

    @app.get('/health', tags=['system'])
    async def health():
        try:
            await db.ping()
        except Exception as exc:
            return JSONResponse(status_code=503, content={'status': 'degraded', 'database': 'unavailable', 'error': str(exc)})
        return {'status': 'ok', 'service': API_NAME, 'version': API_VERSION, 'database': 'ok'}

    @app.get('/metrics', dependencies=[Depends(require_auth)], tags=['system'])
    async def metrics():
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.get('/api/v1/plugins', dependencies=[Depends(require_auth)], tags=['catalog'])
    async def plugins_catalog():
        return {'status': 'ok', 'plugins': plugins.list()}

    @app.get('/api/v1/tools', dependencies=[Depends(require_auth)], tags=['catalog'])
    async def tools_catalog(domain: str = Query(default='all')):
        try:
            return {'status': 'ok', 'tools': _public_specs(domain)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get('/api/v1/runs', dependencies=[Depends(require_auth)], tags=['runs'])
    async def runs(limit: int = Query(default=20, ge=1, le=100)):
        return {'status': 'ok', 'runs': list_run_manifests(OUTPUT_ROOT, limit)}

    async def read_job(job_id):
        record = jobs.get(job_id)
        if record is not None:
            await db.upsert_job(record)
            JOB_STATUS.labels(record['tool'], record['status']).set(1)
            return record
        return await db.get_job(job_id)

    @app.post('/api/v1/jobs', status_code=202, dependencies=[Depends(require_auth)], tags=['jobs'])
    async def submit_job(
        payload: JobCreate,
        idempotency_key: str | None = Header(default=None, alias='Idempotency-Key'),
    ):
        try:
            record = jobs.submit(payload.tool, payload.arguments, idempotency_key=idempotency_key)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.upsert_job(record)
        if not record.get('deduplicated'):
            JOB_SUBMISSIONS.labels(payload.tool).inc()
        JOB_STATUS.labels(payload.tool, record['status']).set(1)
        return {'status': 'deduplicated' if record.get('deduplicated') else 'accepted', 'job': record}

    @app.get('/api/v1/jobs', dependencies=[Depends(require_auth)], tags=['jobs'])
    async def list_jobs(limit: int = Query(default=20, ge=1, le=100)):
        records = jobs.list(limit)
        for record in records:
            await db.upsert_job(record)
            JOB_STATUS.labels(record['tool'], record['status']).set(1)
        return {'status': 'ok', 'jobs': records}

    @app.get('/api/v1/jobs/{job_id}', dependencies=[Depends(require_auth)], tags=['jobs'])
    async def get_job(job_id: str):
        record = await read_job(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')
        return {'status': 'ok', 'job': record}

    @app.get('/api/v1/jobs/{job_id}/events', dependencies=[Depends(require_auth)], tags=['jobs'])
    async def job_events(
        job_id: str,
        interval_seconds: float = Query(default=0.2, ge=0.05, le=5),
        timeout_seconds: float = Query(default=60, ge=1, le=300),
    ):
        if await read_job(job_id) is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')

        async def stream():
            last_signature = None
            deadline = monotonic() + timeout_seconds
            while True:
                record = await read_job(job_id)
                if record is None:
                    payload = {'status': 'error', 'error': f'job not found: {job_id}'}
                    yield f'event: error\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n'
                    return
                signature = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
                if signature != last_signature:
                    payload = {'status': 'ok', 'job': record}
                    yield f'event: job\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n'
                    last_signature = signature
                if record.get('status') in {'completed', 'failed', 'cancelled'}:
                    return
                if monotonic() >= deadline:
                    payload = {'status': 'timeout', 'job': record}
                    yield f'event: timeout\ndata: {json.dumps(payload, ensure_ascii=False, default=str)}\n\n'
                    return
                await asyncio.sleep(interval_seconds)

        return StreamingResponse(
            stream(),
            media_type='text/event-stream',
            headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
        )

    @app.post('/api/v1/jobs/{job_id}/cancel', status_code=202, dependencies=[Depends(require_auth)], tags=['jobs'])
    async def cancel_job(job_id: str):
        try:
            record = jobs.cancel(job_id)
        except ValueError as exc:
            status_code = 404 if str(exc).startswith('job not found:') else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        await db.upsert_job(record)
        JOB_STATUS.labels(record['tool'], record['status']).set(1)
        response_status = 'already_terminal' if record['status'] in {'completed', 'failed', 'cancelled'} else 'cancelled' if record['status'] == 'cancelled' else 'cancellation_requested'
        return {'status': response_status, 'job': record}

    @app.post('/api/v1/jobs/{job_id}/retry', status_code=202, dependencies=[Depends(require_auth)], tags=['jobs'])
    async def retry_job(job_id: str):
        try:
            record = jobs.retry(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.upsert_job(record)
        JOB_SUBMISSIONS.labels(record['tool']).inc()
        JOB_STATUS.labels(record['tool'], record['status']).set(1)
        return {'status': 'accepted', 'job': record}

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
