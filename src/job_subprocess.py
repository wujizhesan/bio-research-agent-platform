"""Subprocess entry point for a single isolated research job."""

import json
import math
import os
from pathlib import Path
import sys
from time import perf_counter
import traceback

try:
    from .observability import bind_context
except ImportError:
    from observability import bind_context


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
    started = perf_counter()
    try:
        request = json.loads(request_path.read_text(encoding='utf-8'))
        _apply_posix_limits(request.get('limits', {}))
        try:
            from .domain_registry import run_tool
        except ImportError:
            from domain_registry import run_tool
        with bind_context(**request.get('observability', {})):
            result = run_tool(request['tool'], request.get('arguments', {}))
        payload = {'ok': True, 'result': result}
        exit_code = 0
    except BaseException as exc:
        payload = {
            'ok': False,
            'error': str(exc) or exc.__class__.__name__,
            'type': exc.__class__.__name__,
            'traceback': traceback.format_exc(limit=20),
        }
        exit_code = 1
    tool = str(request.get('tool') or 'unknown')
    result_status = (
        payload.get('result', {}).get('status')
        if isinstance(payload.get('result'), dict) else None
    )
    payload['telemetry'] = {
        'domain': tool.split('_', 1)[0] if '_' in tool else 'unknown',
        'tool': tool,
        'status': (
            'error'
            if not payload.get('ok') or result_status in {'error', 'failed', 'missing', 'not_found'}
            else 'success'
        ),
        'duration_seconds': perf_counter() - started,
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
