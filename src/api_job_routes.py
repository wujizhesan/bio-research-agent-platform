"""REST and SSE routes for platform job lifecycle management."""

import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from time import monotonic

from fastapi import Depends, Header, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse

try:
    from .api_contracts import (
        JobCreate,
        iter_artifact_values,
        resolve_artifact_path,
    )
    from .auth import Principal
    from .observability import JOB_STATUS, JOB_SUBMISSIONS
    from .run_context import bind_run_actor
except ImportError:
    from api_contracts import JobCreate, iter_artifact_values, resolve_artifact_path
    from auth import Principal
    from observability import JOB_STATUS, JOB_SUBMISSIONS
    from run_context import bind_run_actor


@dataclass(frozen=True)
class JobRouteHandlers:
    read_job: object
    submit_job: object


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

        async def visible(record):
            item = await expose_job(record)
            item_project = item.get('project_id')
            if project_id and item_project != project_id:
                return None
            if (
                item_project
                and visible_projects is not None
                and item_project not in visible_projects
            ):
                return None
            return item

        if app.state.job_backend == 'redis':
            records = await database.list_jobs(limit)
            if records:
                records = [
                    item
                    for item in [await visible(record) for record in records]
                    if item is not None
                ]
                for record in records:
                    JOB_STATUS.labels(record['tool'], record['status']).set(1)
                return {'status': 'ok', 'jobs': records}
        records = jobs.list(limit)
        records = [
            item
            for item in [await visible(record) for record in records]
            if item is not None
        ]
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
    output_root,
    audit,
    require_permission,
    job_access,
    issue_stream_ticket,
    stream_ticket_ttl,
    stream_principal,
    read_job,
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

    @app.get('/api/v1/jobs/{job_id}/artifacts', tags=['jobs'])
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
        audit.record(
            principal,
            'job.artifact_download',
            'job',
            job_id,
            {'filename': target.name},
        )
        return FileResponse(
            target,
            filename=target.name,
            headers={'X-Job-ID': job_id},
        )

    @app.get('/api/v1/jobs/{job_id}/events', tags=['jobs'])
    async def job_events(
        job_id: str,
        interval_seconds: float = Query(default=0.2, ge=0.05, le=5),
        timeout_seconds: float = Query(default=60, ge=1, le=300),
        principal: Principal = Depends(stream_principal),
    ):
        if await read_job(job_id) is None:
            raise HTTPException(
                status_code=404,
                detail=f'job not found: {job_id}',
            )
        await job_access(job_id, principal, {'owner', 'editor', 'viewer'})
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
                    if subscriber is None:
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
            status_code = 404 if str(exc).startswith('job not found:') else 400
            raise HTTPException(status_code=status_code, detail=str(exc)) from exc
        await database.upsert_job(record)
        JOB_STATUS.labels(record['tool'], record['status']).set(1)
        audit.record(
            principal,
            'job.cancel',
            'job',
            job_id,
            {'status': record['status']},
        )
        if record['status'] in {'completed', 'failed', 'cancelled'}:
            response_status = 'already_terminal'
        elif record['status'] == 'cancelled':
            response_status = 'cancelled'
        else:
            response_status = 'cancellation_requested'
        return {'status': response_status, 'job': record}

    @app.post('/api/v1/jobs/{job_id}/retry', status_code=202, tags=['jobs'])
    async def retry_job(
        job_id: str,
        principal: Principal = Depends(require_permission('jobs:write')),
    ):
        project_id = await job_access(job_id, principal, {'owner', 'editor'})
        try:
            with bind_run_actor(principal):
                record = jobs.retry(job_id)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await database.upsert_job(record)
        if project_id:
            await database.assign_job_project(
                record['job_id'],
                project_id,
                datetime.now(timezone.utc).isoformat(),
            )
            record = await expose_job(record)
        JOB_SUBMISSIONS.labels(record['tool']).inc()
        JOB_STATUS.labels(record['tool'], record['status']).set(1)
        audit.record(
            principal,
            'job.retry',
            'job',
            record['job_id'],
            {'retry_of': job_id},
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
):
    async def read_job(job_id):
        if app.state.job_backend == 'redis':
            try:
                record = jobs.get(job_id)
            except Exception:
                record = None
            if record is not None:
                JOB_STATUS.labels(record['tool'], record['status']).set(1)
                return await expose_job(record)
            record = await database.get_job(job_id)
            if record is not None:
                JOB_STATUS.labels(record['tool'], record['status']).set(1)
            return await expose_job(record)
        record = jobs.get(job_id)
        if record is not None:
            await database.upsert_job(record)
            JOB_STATUS.labels(record['tool'], record['status']).set(1)
            return await expose_job(record)
        return await expose_job(await database.get_job(job_id))

    _register_job_query_routes(
        app,
        jobs=jobs,
        database=database,
        require_permission=require_permission,
        project_access=project_access,
        job_access=job_access,
        expose_job=expose_job,
        read_job=read_job,
    )

    @app.post('/api/v1/jobs', status_code=202, tags=['jobs'])
    async def submit_job(
        payload: JobCreate,
        idempotency_key: str | None = Header(
            default=None,
            alias='Idempotency-Key',
        ),
        principal: Principal = Depends(require_permission('jobs:write')),
    ):
        if payload.project_id:
            await project_access(
                payload.project_id,
                principal,
                {'owner', 'editor'},
            )
        try:
            with bind_run_actor(principal):
                record = jobs.submit(
                    payload.tool,
                    payload.arguments,
                    idempotency_key=idempotency_key,
                    resources=(
                        payload.resources.model_dump()
                        if payload.resources is not None
                        else None
                    ),
                    priority=payload.priority,
                )
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        await database.upsert_job(record)
        if payload.project_id:
            await database.assign_job_project(
                record['job_id'],
                payload.project_id,
                datetime.now(timezone.utc).isoformat(),
            )
            record = await expose_job(record)
        if not record.get('deduplicated'):
            JOB_SUBMISSIONS.labels(payload.tool).inc()
        JOB_STATUS.labels(payload.tool, record['status']).set(1)
        audit.record(
            principal,
            'job.submit',
            'job',
            record['job_id'],
            {
                'tool': payload.tool,
                'resources': record.get('resources'),
                'priority': record.get('priority', 0),
                'deduplicated': bool(record.get('deduplicated')),
            },
        )
        response_status = (
            'deduplicated' if record.get('deduplicated') else 'accepted'
        )
        return {'status': response_status, 'job': record}

    _register_job_event_routes(
        app,
        jobs=jobs,
        output_root=output_root,
        audit=audit,
        require_permission=require_permission,
        job_access=job_access,
        issue_stream_ticket=issue_stream_ticket,
        stream_ticket_ttl=stream_ticket_ttl,
        stream_principal=stream_principal,
        read_job=read_job,
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
