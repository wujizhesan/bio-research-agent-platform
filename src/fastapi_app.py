"""FastAPI service adapter for the pluggable research Agent platform."""
from contextlib import asynccontextmanager
import argparse
import hmac
import os
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

try:
    from .api_server import list_run_manifests
    from .database import Database
    from .domain_registry import active_tool_specs
    from .job_manager import JobManager
    from .plugin_manager import PluginManager
except ImportError:
    from api_server import list_run_manifests
    from database import Database
    from domain_registry import active_tool_specs
    from job_manager import JobManager
    from plugin_manager import PluginManager


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
    origins = [item.strip() for item in os.environ.get('CORS_ORIGINS', 'http://localhost:5173').split(',') if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=['GET', 'POST'],
        allow_headers=['Authorization', 'Content-Type'],
    )

    @app.get('/health', tags=['system'])
    async def health():
        try:
            await db.ping()
        except Exception as exc:
            return JSONResponse(status_code=503, content={'status': 'degraded', 'database': 'unavailable', 'error': str(exc)})
        return {'status': 'ok', 'service': API_NAME, 'version': API_VERSION, 'database': 'ok'}

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

    @app.post('/api/v1/jobs', status_code=202, dependencies=[Depends(require_auth)], tags=['jobs'])
    async def submit_job(payload: JobCreate):
        try:
            record = jobs.submit(payload.tool, payload.arguments)
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.upsert_job(record)
        return {'status': 'accepted', 'job': record}

    @app.get('/api/v1/jobs', dependencies=[Depends(require_auth)], tags=['jobs'])
    async def list_jobs(limit: int = Query(default=20, ge=1, le=100)):
        records = jobs.list(limit)
        for record in records:
            await db.upsert_job(record)
        return {'status': 'ok', 'jobs': records}

    @app.get('/api/v1/jobs/{job_id}', dependencies=[Depends(require_auth)], tags=['jobs'])
    async def get_job(job_id: str):
        record = jobs.get(job_id)
        if record is not None:
            await db.upsert_job(record)
        else:
            record = await db.get_job(job_id)
        if record is None:
            raise HTTPException(status_code=404, detail=f'job not found: {job_id}')
        return {'status': 'ok', 'job': record}

    @app.post('/api/v1/jobs/{job_id}/retry', status_code=202, dependencies=[Depends(require_auth)], tags=['jobs'])
    async def retry_job(job_id: str):
        try:
            record = jobs.retry(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await db.upsert_job(record)
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
