"""Stable request models and protocol serializers for the HTTP API."""

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field


A2A_PROTOCOL_VERSION = '0.3.0'
ARTIFACT_RESULT_KEYS = frozenset({
    'output_csv', 'output_md', 'output_html', 'result_csv', 'report',
    'manifest_path', 'report_path', 'variant_output_csv', 'sequence_report_path',
})


class JobResources(BaseModel):
    cpu_cores: float = Field(default=1, ge=0.1)
    memory_mb: int = Field(default=512, ge=1)
    gpu_count: int = Field(default=0, ge=0)
    gpu_memory_mb: int = Field(default=0, ge=0)
    labels: list[str] = Field(default_factory=list, max_length=32)


class JobCreate(BaseModel):
    tool: str = Field(min_length=1, max_length=200)
    arguments: dict[str, Any] = Field(default_factory=dict)
    project_id: str | None = Field(default=None, min_length=1, max_length=64)
    resources: JobResources | None = None
    priority: int = Field(default=0, ge=-100, le=100)


class PluginStateUpdate(BaseModel):
    enabled: bool


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=2000)


class ProjectMemberCreate(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    role: Literal['owner', 'editor', 'viewer'] = 'viewer'


def iter_artifact_values(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ARTIFACT_RESULT_KEYS and isinstance(item, str):
                yield item
            yield from iter_artifact_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_artifact_values(item)


def resolve_artifact_path(raw_path, output_root):
    raw = Path(raw_path)
    output_root = Path(output_root)
    project_root = output_root.parent
    candidates = [raw] if raw.is_absolute() else [project_root / raw, output_root / raw]
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


def a2a_response(request_id, result=None, error=None):
    response = {'jsonrpc': '2.0', 'id': request_id}
    if error is not None:
        response['error'] = error
    else:
        response['result'] = result
    return response


def a2a_error(request_id, code, message, data=None):
    error = {'code': code, 'message': message}
    if data is not None:
        error['data'] = data
    return a2a_response(request_id, error=error)


def a2a_task(record, context_id, history=None):
    state = {
        'queued': 'submitted',
        'running': 'working',
        'completed': 'completed',
        'failed': 'failed',
        'cancelled': 'canceled',
    }.get(record.get('status'), 'unknown')
    status_payload = {
        'state': state,
        'timestamp': (
            record.get('finished_at')
            or record.get('started_at')
            or record.get('created_at')
        ),
    }
    if record.get('error'):
        status_payload['message'] = {
            'kind': 'message',
            'role': 'agent',
            'messageId': f"{record['job_id']}-error",
            'parts': [{'kind': 'text', 'text': str(record['error'])}],
        }
    task = {
        'kind': 'task',
        'id': record['job_id'],
        'contextId': context_id,
        'status': status_payload,
        'metadata': {
            'bio.job_id': record['job_id'],
            'bio.tool': record['tool'],
            'bio.trace_id': record.get('trace_id'),
            'bio.request_id': record.get('request_id'),
        },
    }
    if history:
        task['history'] = history
    if record.get('status') == 'completed' and record.get('result') is not None:
        task['artifacts'] = [{
            'artifactId': f"{record['job_id']}-result",
            'name': 'structured-result',
            'parts': [{'kind': 'data', 'data': record['result']}],
        }]
    return task


def a2a_message_text(message):
    parts = message.get('parts')
    if not isinstance(parts, list):
        return ''
    return '\n'.join(
        part['text']
        for part in parts
        if (
            isinstance(part, dict)
            and part.get('kind') == 'text'
            and isinstance(part.get('text'), str)
        )
    ).strip()
