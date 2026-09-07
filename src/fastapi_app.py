"""FastAPI service adapter for the pluggable research Agent platform."""
import argparse
import logging
import os
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.security import OAuth2PasswordRequestForm
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from starlette.responses import Response

try:
    from .api_contracts import (
        A2A_PROTOCOL_VERSION,
        FrontendErrorReport,
        PluginStateUpdate,
        ProjectCreate,
        ProjectMemberCreate,
    )
    from .api_a2a_routes import register_a2a_routes
    from .api_job_routes import register_job_routes
    from .api_dependencies import ApiDependencies
    from .api_file_routes import register_file_routes
    from .api_runtime import build_api_runtime
    from .auth import AuthService, AuthenticationError, Principal
    from .domain_registry import active_tool_specs
    from .observability import (
        HTTP_ACTIVE,
        FRONTEND_ERRORS,
        HTTP_LATENCY,
        HTTP_REQUESTS,
        bind_context,
        configure_logging,
        current_context,
        log_event,
        request_id,
        trace_id,
    )
except ImportError:
    from api_contracts import (
        A2A_PROTOCOL_VERSION,
        FrontendErrorReport,
        PluginStateUpdate,
        ProjectCreate,
        ProjectMemberCreate,
    )
    from api_a2a_routes import register_a2a_routes
    from api_job_routes import register_job_routes
    from api_dependencies import ApiDependencies
    from api_file_routes import register_file_routes
    from api_runtime import build_api_runtime
    from auth import AuthService, AuthenticationError, Principal
    from domain_registry import active_tool_specs
    from observability import (
        HTTP_ACTIVE,
        FRONTEND_ERRORS,
        HTTP_LATENCY,
        HTTP_REQUESTS,
        bind_context,
        configure_logging,
        current_context,
        log_event,
        request_id,
        trace_id,
    )


API_NAME = 'bio-research-agent-api'
API_VERSION = '0.2.0'
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / 'output'


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


def _register_core_routes(
    app,
    *,
    db,
    auth,
    login_rate_limiter,
    audit,
    plugins,
    require_permission,
    project_access,
):
    @app.get('/health', tags=['system'])
    async def health():
        try:
            await db.ping()
        except Exception as exc:
            return JSONResponse(
                status_code=503,
                content={
                    'status': 'degraded',
                    'database': 'unavailable',
                    'error': str(exc),
                },
            )
        return {
            'status': 'ok',
            'service': API_NAME,
            'version': API_VERSION,
            'database': 'ok',
            'job_backend': app.state.job_backend,
            'storage_backend': app.state.storage_backend,
            'observability': {
                'request_id': current_context().get('request_id'),
                'trace_id': current_context().get('trace_id'),
                'metrics': '/metrics',
            },
        }

    @app.post('/api/v1/auth/token', tags=['auth'])
    async def issue_token(
        request: Request,
        form_data: OAuth2PasswordRequestForm = Depends(),
    ):
        client_host = request.client.host if request.client else 'unknown'
        rate_key = f'{client_host}:{form_data.username.strip().lower()}'
        if not login_rate_limiter.allow(rate_key):
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail='login rate limit exceeded',
                headers={'Retry-After': str(login_rate_limiter.window_seconds)},
            )
        try:
            payload = auth.issue_token(form_data.username, form_data.password)
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=str(exc),
                headers={'WWW-Authenticate': 'Bearer'},
            ) from exc
        login_rate_limiter.reset(rate_key)
        principal_data = payload['principal']
        audit.record(
            Principal(
                principal_data['sub'],
                tuple(principal_data['roles']),
                principal_data['auth_type'],
            ),
            'auth.login',
            'auth',
            metadata={'auth_type': 'jwt'},
        )
        return payload

    @app.get(
        '/metrics',
        dependencies=[Depends(require_permission('metrics:read'))],
        tags=['system'],
    )
    async def metrics():
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    @app.post('/api/v1/telemetry/frontend-errors', status_code=202, tags=['system'])
    async def report_frontend_error(
        payload: FrontendErrorReport,
        request: Request,
        principal: Principal = Depends(require_permission('telemetry:write')),
    ):
        with bind_context(job_id=payload.job_id, plugin=payload.plugin_id):
            FRONTEND_ERRORS.inc()
            log_event(
                'frontend.error.captured',
                level=logging.ERROR,
                boundary_name=payload.boundary_name,
                error_name=payload.error_name,
                message=payload.message,
                component_stack=payload.component_stack,
                client_trace_id=payload.trace_id,
                path=payload.path,
                occurred_at=payload.occurred_at,
                reporter_subject=principal.subject,
            )
        return {'status': 'accepted', 'trace_id': request.state.trace_id}

    @app.get(
        '/api/v1/plugins',
        dependencies=[Depends(require_permission('catalog:read'))],
        tags=['catalog'],
    )
    async def plugins_catalog():
        return {'status': 'ok', 'plugins': plugins.list()}

    @app.get(
        '/api/v1/tools',
        dependencies=[Depends(require_permission('catalog:read'))],
        tags=['catalog'],
    )
    async def tools_catalog(domain: str = Query(default='all')):
        try:
            return {'status': 'ok', 'tools': _public_specs(domain)}
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post('/api/v1/plugins/validate', tags=['catalog'])
    async def validate_plugin_candidate(
        payload: dict[str, Any],
        principal: Principal = Depends(require_permission('plugins:write')),
    ):
        report = plugins.validate_candidate(payload)
        permissions = report.get('permissions') or {}
        audit.record(
            principal,
            'plugin.validate',
            'plugin',
            payload.get('key'),
            {
                'compatible': report['compatible'],
                'security_approved': permissions.get('approved'),
                'denied_permissions': permissions.get('denied'),
            },
        )
        return {'status': 'ok', 'validation': report}

    @app.post('/api/v1/plugins/health', tags=['catalog'])
    async def check_all_plugin_health(
        principal: Principal = Depends(require_permission('plugins:write')),
    ):
        checked = plugins.check_health()
        audit.record(
            principal,
            'plugin.health_check',
            'plugin',
            metadata={'count': len(checked)},
        )
        return {'status': 'ok', 'plugins': checked}

    @app.post('/api/v1/plugins/{domain}/health', tags=['catalog'])
    async def check_plugin_health(
        domain: str,
        principal: Principal = Depends(require_permission('plugins:write')),
    ):
        try:
            checked = plugins.check_health(domain)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        audit.record(
            principal,
            'plugin.health_check',
            'plugin',
            domain,
            {
                'healthy': checked.get('health', {}).get('healthy'),
                'activation': checked.get('activation'),
            },
        )
        return {'status': 'ok', 'plugin': checked}

    @app.get(
        '/api/v1/capabilities',
        dependencies=[Depends(require_permission('catalog:read'))],
        tags=['catalog'],
    )
    async def capabilities_catalog():
        tool_count = len(active_tool_specs())
        return {
            'status': 'ok',
            'service': API_NAME,
            'version': API_VERSION,
            'tool_count': tool_count,
            'interfaces': {
                'rest': {
                    'status': 'available',
                    'protocol': 'HTTP REST',
                    'docs': '/docs',
                    'openapi': '/openapi.json',
                },
                'sse': {
                    'status': 'available',
                    'protocol': 'Server-Sent Events',
                    'endpoint': '/api/v1/jobs/{job_id}/events',
                },
                'mcp': {
                    'status': (
                        'available' if find_spec('mcp') is not None
                        else 'dependency_missing'
                    ),
                    'protocol': 'Model Context Protocol',
                    'transport': 'stdio',
                    'entrypoint': 'bio-agent-mcp',
                    'tool_count': tool_count,
                },
                'embedded': {
                    'status': 'available',
                    'protocol': 'Python call',
                    'entrypoint': 'src.domain_registry.run_tool',
                },
                'a2a': {
                    'status': 'available',
                    'protocol': f'A2A JSON-RPC {A2A_PROTOCOL_VERSION}',
                    'endpoint': '/a2a',
                    'agent_card': '/.well-known/agent-card.json',
                    'methods': [
                        'message/send',
                        'message/stream',
                        'tasks/get',
                        'tasks/cancel',
                    ],
                },
            },
        }

    @app.post('/api/v1/projects', status_code=201, tags=['projects'])
    async def create_project(
        payload: ProjectCreate,
        principal: Principal = Depends(require_permission('projects:write')),
    ):
        project_id = uuid4().hex
        created_at = datetime.now(timezone.utc).isoformat()
        project = await db.create_project(
            project_id,
            payload.name,
            payload.description,
            principal.subject,
            created_at,
        )
        audit.record(
            principal,
            'project.create',
            'project',
            project_id,
            {'name': payload.name},
        )
        return {'status': 'created', 'project': project}

    @app.get('/api/v1/projects', tags=['projects'])
    async def list_projects(
        limit: int = Query(default=20, ge=1, le=100),
        principal: Principal = Depends(require_permission('projects:read')),
    ):
        projects = await db.list_projects(principal.subject, limit)
        return {'status': 'ok', 'projects': projects}

    @app.get('/api/v1/projects/{project_id}', tags=['projects'])
    async def get_project(
        project_id: str,
        principal: Principal = Depends(require_permission('projects:read')),
    ):
        project = await project_access(
            project_id,
            principal,
            {'owner', 'editor', 'viewer'},
        )
        return {'status': 'ok', 'project': project}

    @app.get('/api/v1/projects/{project_id}/members', tags=['projects'])
    async def list_project_members(
        project_id: str,
        principal: Principal = Depends(require_permission('projects:read')),
    ):
        await project_access(project_id, principal, {'owner', 'editor', 'viewer'})
        members = await db.list_project_members(project_id)
        return {'status': 'ok', 'members': members}

    @app.post(
        '/api/v1/projects/{project_id}/members',
        status_code=201,
        tags=['projects'],
    )
    async def add_project_member(
        project_id: str,
        payload: ProjectMemberCreate,
        principal: Principal = Depends(require_permission('members:write')),
    ):
        project = await project_access(project_id, principal, {'owner'})
        if payload.role == 'owner' and payload.subject != project['owner_subject']:
            raise HTTPException(
                status_code=400,
                detail='project owner cannot be reassigned',
            )
        member = await db.upsert_project_member(
            project_id,
            payload.subject,
            payload.role,
            datetime.now(timezone.utc).isoformat(),
        )
        audit.record(
            principal,
            'project.member_upsert',
            'project',
            project_id,
            {'subject': payload.subject, 'role': payload.role},
        )
        return {'status': 'ok', 'member': member}

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
        audit.record(
            principal,
            'plugin.state_change',
            'plugin',
            domain,
            {'enabled': payload.enabled},
        )
        return {'status': 'ok', 'plugin': plugin}


def create_app(job_manager=None, plugin_manager=None, database=None, file_storage=None, audit_log=None):
    configure_logging(API_NAME)
    runtime = build_api_runtime(
        PROJECT_ROOT,
        OUTPUT_ROOT,
        job_manager=job_manager,
        plugin_manager=plugin_manager,
        database=database,
        file_storage=file_storage,
        audit_log=audit_log,
    )
    jobs = runtime.jobs
    plugins = runtime.plugins
    db = runtime.database
    storage = runtime.storage
    audit = runtime.audit
    auth = runtime.auth
    login_rate_limiter = runtime.login_rate_limiter

    app = FastAPI(
        title='Bio Research Agent API',
        version=API_VERSION,
        description='Async API for pluggable CADD, omics, sequence and research workflows.',
        lifespan=runtime.lifespan,
    )
    runtime.bind(app)
    origins = [item.strip() for item in os.environ.get(
        'CORS_ORIGINS',
        'http://localhost:5173,http://127.0.0.1:5173,http://localhost:5174,http://127.0.0.1:5174',
    ).split(',') if item.strip()]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=['GET', 'POST'],
        allow_headers=[
            'Authorization', 'Content-Type', 'Idempotency-Key',
            'X-Request-ID', 'X-Trace-ID', 'traceparent',
        ],
        expose_headers=['X-Request-ID', 'X-Trace-ID'],
    )

    @app.middleware('http')
    async def metrics_middleware(request: Request, call_next):
        from time import perf_counter
        started = perf_counter()
        response = None
        current_request_id = request_id(request.headers.get('X-Request-ID'))
        current_trace_id = trace_id(
            request.headers.get('X-Trace-ID'),
            request.headers.get('traceparent'),
        )
        request.state.request_id = current_request_id
        request.state.trace_id = current_trace_id
        HTTP_ACTIVE.labels(request.method).inc()
        with bind_context(
            request_id=current_request_id,
            trace_id=current_trace_id,
        ):
            try:
                response = await call_next(request)
                response.headers['X-Request-ID'] = current_request_id
                response.headers['X-Trace-ID'] = current_trace_id
                return response
            except Exception as exc:
                log_event(
                    'http.request.failed',
                    method=request.method,
                    path=request.url.path,
                    error_type=type(exc).__name__,
                )
                raise
            finally:
                elapsed = perf_counter() - started
                route = request.scope.get('route')
                path = getattr(route, 'path', None) or '/_unmatched'
                status_code = str(response.status_code if response is not None else 500)
                if request.url.path != '/metrics':
                    HTTP_REQUESTS.labels(request.method, path, status_code).inc()
                    HTTP_LATENCY.labels(request.method, path).observe(elapsed)
                HTTP_ACTIVE.labels(request.method).dec()
                log_event(
                    'http.request.completed',
                    method=request.method,
                    path=path,
                    status=status_code,
                    duration_seconds=elapsed,
                )

    dependencies = ApiDependencies(auth, db)
    current_principal = dependencies.current_principal
    project_access = dependencies.project_access
    job_access = dependencies.job_access
    expose_job = dependencies.expose_job
    require_permission = dependencies.require_permission
    issue_stream_ticket = dependencies.issue_stream_ticket
    stream_ticket_ttl = dependencies.stream_ticket_ttl
    stream_principal = dependencies.stream_principal

    _register_core_routes(
        app,
        db=db,
        auth=auth,
        login_rate_limiter=login_rate_limiter,
        audit=audit,
        plugins=plugins,
        require_permission=require_permission,
        project_access=project_access,
    )

    register_file_routes(
        app,
        storage=storage,
        database=db,
        audit=audit,
        project_root=PROJECT_ROOT,
        output_root=OUTPUT_ROOT,
        require_permission=require_permission,
        project_access=project_access,
    )

    job_handlers = register_job_routes(
        app,
        jobs=jobs,
        database=db,
        audit=audit,
        output_root=OUTPUT_ROOT,
        require_permission=require_permission,
        project_access=project_access,
        job_access=job_access,
        expose_job=expose_job,
        issue_stream_ticket=issue_stream_ticket,
        stream_ticket_ttl=stream_ticket_ttl,
        stream_principal=stream_principal,
    )
    read_job = job_handlers.read_job
    submit_job = job_handlers.submit_job

    register_a2a_routes(
        app,
        api_version=API_VERSION,
        auth=auth,
        audit=audit,
        database=db,
        jobs=jobs,
        current_principal=current_principal,
        read_job=read_job,
        submit_job=submit_job,
    )

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
