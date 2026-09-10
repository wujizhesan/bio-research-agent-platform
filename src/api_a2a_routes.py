"""A2A JSON-RPC transport adapter for platform jobs."""

import asyncio
import json
from time import monotonic
from typing import Any
from uuid import uuid4

from fastapi import Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

try:
    from .api_contracts import (
        A2A_PROTOCOL_VERSION,
        JobCreate,
        a2a_error,
        a2a_message_text,
        a2a_response,
        a2a_task,
    )
    from .auth import Principal
    from .observability import JOB_STATUS
except ImportError:
    from api_contracts import (
        A2A_PROTOCOL_VERSION,
        JobCreate,
        a2a_error,
        a2a_message_text,
        a2a_response,
        a2a_task,
    )
    from auth import Principal
    from observability import JOB_STATUS


def register_a2a_routes(
    app,
    *,
    api_version,
    auth,
    audit,
    database,
    jobs,
    current_principal,
    read_job,
    submit_job,
):
    @app.get('/.well-known/agent-card.json', tags=['a2a'])
    async def agent_card(request: Request):
        card = {
            'protocolVersion': A2A_PROTOCOL_VERSION,
            'name': 'Bio Research Agent',
            'description': (
                'Pluggable bioinformatics research agent for CADD, omics, '
                'imaging, evidence and sequence workflows.'
            ),
            'url': f'{str(request.base_url).rstrip("/")}/a2a',
            'preferredTransport': 'JSONRPC',
            'version': api_version,
            'capabilities': {
                'streaming': True,
                'pushNotifications': False,
                'stateTransitionHistory': False,
            },
            'defaultInputModes': ['text/plain', 'application/json'],
            'defaultOutputModes': ['application/json'],
            'skills': [{
                'id': 'bioinformatics-research',
                'name': 'Bioinformatics research workflows',
                'description': (
                    'Plan and execute traceable multi-domain research workflows.'
                ),
                'tags': [
                    'cadd',
                    'omics',
                    'imaging',
                    'literature',
                    'knowledge',
                    'sequence',
                ],
                'examples': [
                    'Run an RNA-seq analysis and design an mRNA sequence.'
                ],
            }],
        }
        if auth.enabled:
            card['securitySchemes'] = {
                'bearerAuth': {'type': 'http', 'scheme': 'bearer'}
            }
            card['security'] = [{'bearerAuth': []}]
        return card

    @app.post('/a2a', tags=['a2a'])
    async def a2a_rpc(
        payload: dict[str, Any],
        principal: Principal = Depends(current_principal),
    ):
        request_id = payload.get('id')
        if (
            payload.get('jsonrpc') != '2.0'
            or not isinstance(payload.get('method'), str)
        ):
            return a2a_error(request_id, -32600, 'invalid JSON-RPC request')
        params = payload.get('params', {})
        if not isinstance(params, dict):
            return a2a_error(request_id, -32602, 'params must be an object')
        method = payload['method']

        if method == 'message/send':
            if not auth.has_permission(principal, 'jobs:write'):
                return a2a_error(request_id, -32003, 'insufficient permissions')
            message = params.get('message')
            if not isinstance(message, dict) or message.get('role', 'user') != 'user':
                return a2a_error(
                    request_id,
                    -32602,
                    'message with role=user is required',
                )
            if message.get('taskId'):
                return a2a_error(
                    request_id,
                    -32602,
                    'task continuation is not supported by this adapter',
                )
            normalized_message = dict(message)
            normalized_message.setdefault('kind', 'message')
            normalized_message.setdefault('messageId', uuid4().hex)
            message_metadata = (
                message.get('metadata')
                if isinstance(message.get('metadata'), dict)
                else {}
            )
            request_metadata = (
                params.get('metadata')
                if isinstance(params.get('metadata'), dict)
                else {}
            )
            tool = message_metadata.get('tool') or request_metadata.get('tool')
            arguments = message_metadata.get('arguments')
            if arguments is None:
                arguments = request_metadata.get('arguments')
            text = a2a_message_text(message)
            if tool is None:
                tool = 'research_plan'
                arguments = {
                    'task': text or 'Plan a bioinformatics research task.'
                }
            if not isinstance(tool, str) or not tool:
                return a2a_error(
                    request_id,
                    -32602,
                    'metadata.tool must be a non-empty string',
                )
            if not isinstance(arguments, dict):
                return a2a_error(
                    request_id,
                    -32602,
                    'metadata.arguments must be an object',
                )
            try:
                accepted = await submit_job(
                    JobCreate(tool=tool, arguments=arguments),
                    idempotency_key=normalized_message['messageId'],
                    principal=principal,
                )
            except HTTPException as exc:
                return a2a_error(request_id, -32000, str(exc.detail))
            record = accepted['job']
            return a2a_response(
                request_id,
                result={
                    'task': a2a_task(
                        record,
                        f"bio-{record['job_id']}",
                        [normalized_message],
                    )
                },
            )

        if method == 'message/stream':
            if not auth.has_permission(principal, 'jobs:write'):
                return a2a_error(request_id, -32003, 'insufficient permissions')
            sent = await a2a_rpc(
                {
                    'jsonrpc': '2.0',
                    'id': request_id,
                    'method': 'message/send',
                    'params': params,
                },
                principal=principal,
            )
            if 'error' in sent:
                return sent
            initial_task = sent['result']['task']
            task_id = initial_task['id']
            context_id = initial_task['contextId']

            async def stream():
                last_state = None
                deadline = monotonic() + 300
                terminal_states = {'completed', 'failed', 'canceled'}
                while True:
                    record = await read_job(task_id)
                    if record is None:
                        error = a2a_error(
                            request_id,
                            -32001,
                            'task not found',
                        )
                        yield (
                            f'data: {json.dumps(error, ensure_ascii=False)}\n\n'
                        )
                        return
                    task_payload = a2a_task(record, context_id)
                    state = task_payload['status']['state']
                    if state != last_state:
                        if state == 'completed' and task_payload.get('artifacts'):
                            for artifact in task_payload['artifacts']:
                                event = {
                                    'kind': 'artifact-update',
                                    'taskId': task_id,
                                    'contextId': context_id,
                                    'artifact': artifact,
                                    'lastChunk': True,
                                }
                                payload = a2a_response(request_id, result=event)
                                yield (
                                    'data: '
                                    + json.dumps(
                                        payload,
                                        ensure_ascii=False,
                                        default=str,
                                    )
                                    + '\n\n'
                                )
                        status_event = {
                            'kind': 'status-update',
                            'taskId': task_id,
                            'contextId': context_id,
                            'status': task_payload['status'],
                            'final': state in terminal_states,
                        }
                        payload = a2a_response(
                            request_id,
                            result=status_event,
                        )
                        yield (
                            'data: '
                            + json.dumps(
                                payload,
                                ensure_ascii=False,
                                default=str,
                            )
                            + '\n\n'
                        )
                        last_state = state
                        if state in terminal_states:
                            return
                    if monotonic() >= deadline:
                        error = a2a_error(
                            request_id,
                            -32002,
                            'A2A stream timed out',
                        )
                        yield (
                            f'data: {json.dumps(error, ensure_ascii=False)}\n\n'
                        )
                        return
                    yield ': keep-alive\n\n'
                    await asyncio.sleep(0.15)

            return StreamingResponse(
                stream(),
                media_type='text/event-stream',
                headers={
                    'Cache-Control': 'no-cache',
                    'X-Accel-Buffering': 'no',
                },
            )

        if method == 'tasks/get':
            if not auth.has_permission(principal, 'jobs:read'):
                return a2a_error(request_id, -32003, 'insufficient permissions')
            task_id = params.get('id')
            if not isinstance(task_id, str) or not task_id:
                return a2a_error(request_id, -32602, 'id is required')
            record = await read_job(task_id)
            if record is None:
                return a2a_error(
                    request_id,
                    -32001,
                    'task not found',
                    {'taskId': task_id},
                )
            return a2a_response(
                request_id,
                result={'task': a2a_task(record, f'bio-{task_id}')},
            )

        if method == 'tasks/cancel':
            if not auth.has_permission(principal, 'jobs:write'):
                return a2a_error(request_id, -32003, 'insufficient permissions')
            task_id = params.get('id')
            if not isinstance(task_id, str) or not task_id:
                return a2a_error(request_id, -32602, 'id is required')
            try:
                record = jobs.cancel(task_id)
            except ValueError as exc:
                return a2a_error(request_id, -32001, str(exc))
            await database.upsert_job(record)
            JOB_STATUS.labels(record['tool'], record['status']).set(1)
            audit.record(
                principal,
                'job.cancel',
                'job',
                task_id,
                {'status': record['status'], 'transport': 'a2a'},
            )
            return a2a_response(
                request_id,
                result={'task': a2a_task(record, f'bio-{task_id}')},
            )

        return a2a_error(
            request_id,
            -32601,
            f'method not found: {method}',
        )

    return agent_card, a2a_rpc
