"""Subprocess entry point for a single isolated research job."""

from time import monotonic_ns, perf_counter

_MODULE_ENTRY_NS = monotonic_ns()

import json
import math
import os
from pathlib import Path
import sys
import traceback

try:
    from .external_service_policy import ServiceRetryDeferredError
    from .observability import bind_context
    from .run_context import bind_run_context
except ImportError:
    from external_service_policy import ServiceRetryDeferredError
    from observability import bind_context
    from run_context import bind_run_context


def _apply_posix_limits(limits):
    if os.name == 'nt':
        return
    import resource

    memory_limit_mb = int(limits.get('memory_limit_mb') or 0)
    if memory_limit_mb:
        memory_bytes = memory_limit_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    cpu_time_seconds = int(limits.get('cpu_time_seconds') or 0)
    if cpu_time_seconds:
        soft = max(int(math.ceil(cpu_time_seconds)), 1)
        resource.setrlimit(resource.RLIMIT_CPU, (soft, soft + 1))


def main(argv=None):
    args = list(argv or sys.argv[1:])
    if len(args) != 2:
        raise SystemExit('usage: job_subprocess.py REQUEST_PATH RESPONSE_PATH')
    request_path, response_path = map(Path, args)
    request = {}
    started_ns = monotonic_ns()
    registry_import_seconds = None
    tool_run_seconds = None
    try:
        request = json.loads(request_path.read_text(encoding='utf-8'))
        _apply_posix_limits(request.get('limits', {}))
        if request.get('execution_domain') == 'knowledge':
            os.environ['BIO_AGENT_EXECUTION_DOMAIN'] = 'knowledge'
        registry_started = perf_counter()
        try:
            from .domain_registry import run_tool
        except ImportError:
            from domain_registry import run_tool
        registry_import_seconds = perf_counter() - registry_started
        context = (
            bind_run_context(request['run_context'])
            if request.get('run_context')
            else bind_context(**request.get('observability', {}))
        )
        with context:
            tool_started = perf_counter()
            try:
                result = run_tool(request['tool'], request.get('arguments', {}))
            finally:
                tool_run_seconds = perf_counter() - tool_started
        payload = {'ok': True, 'result': result}
        exit_code = 0
    except BaseException as exc:
        payload = {
            'ok': False,
            'error': str(exc) or exc.__class__.__name__,
            'type': exc.__class__.__name__,
            'traceback': traceback.format_exc(limit=20),
        }
        if isinstance(exc, ServiceRetryDeferredError):
            payload.update(exc.as_payload())
            payload['error'] = 'external service requested retry later'
        exit_code = 1
    tool = str(request.get('tool') or 'unknown')
    result_status = (
        payload.get('result', {}).get('status')
        if isinstance(payload.get('result'), dict) else None
    )
    finished_ns = monotonic_ns()
    payload['telemetry'] = {
        'domain': tool.split('_', 1)[0] if '_' in tool else 'unknown',
        'tool': tool,
        'status': (
            'error'
            if not payload.get('ok') or result_status in {'error', 'failed', 'missing', 'not_found'}
            else 'success'
        ),
        'duration_seconds': (finished_ns - started_ns) / 1_000_000_000,
        'registry_import_seconds': registry_import_seconds,
        'tool_run_seconds': tool_run_seconds,
        'process_clock_ns': {
            'module_entry': _MODULE_ENTRY_NS,
            'execution_start': started_ns,
            'execution_finished': finished_ns,
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, default=str)
    max_result_bytes = int(request.get('limits', {}).get('max_result_bytes') or 0)
    if max_result_bytes and len(encoded.encode('utf-8')) > max_result_bytes:
        encoded = json.dumps({
            'ok': False,
            'error': f'job result exceeded {max_result_bytes} byte limit',
        })
        exit_code = 1
    response_path.write_text(encoded, encoding='utf-8')
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
