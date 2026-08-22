"""Local HTTP adapter for the unified bioinformatics tool registry."""
import argparse
import hmac
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

try:
    from .domain_registry import run_tool, active_tool_specs
    from .plugin_manager import PluginManager
    from .job_manager import JobManager
except ImportError:
    from domain_registry import run_tool, active_tool_specs
    from plugin_manager import PluginManager
    from job_manager import JobManager


API_NAME = 'cadd-bio-agent-api'
API_VERSION = '0.1.0'
PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / 'output'
JOB_MANAGER = JobManager(store_path=OUTPUT_ROOT / 'jobs.sqlite3')
PLUGIN_MANAGER = PluginManager(state_path=OUTPUT_ROOT / 'plugin_state.json')


def is_authorized(target, headers=None, api_token=None):
    path = urlparse(target).path.rstrip('/') or '/'
    configured = api_token if api_token is not None else os.environ.get('CADD_API_TOKEN')
    if not configured or path in {'/', '/health'}:
        return True
    values = headers or {}
    authorization = values.get('Authorization', '')
    scheme, _, supplied = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not supplied.strip():
        return False
    return hmac.compare_digest(supplied.strip(), str(configured))

def _public_specs(domain=None):
    selected = None if domain in (None, '', 'all') else domain
    return [
        {key: value for key, value in spec.items() if key not in {'function'}}
        for spec in active_tool_specs(selected)
    ]


def _manifest_summary(path, root):
    try:
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    relative = path.resolve().relative_to(root.resolve()).as_posix()
    report = path.with_name('research_report.md') if path.name == 'research_manifest.json' else None
    report_path = payload.get('report_path')
    if not report_path and report is not None and report.exists():
        report_path = str(report)
    return {
        'run_id': relative,
        'path': str(path),
        'workflow': payload.get('workflow'),
        'status': payload.get('status', 'unknown'),
        'dry_run': bool(payload.get('dry_run', False)),
        'created_at': payload.get('created_at'),
        'completed_steps': payload.get('completed_steps'),
        'failed_steps': payload.get('failed_steps'),
        'report_path': report_path,
        'updated_at': path.stat().st_mtime,
    }


def list_run_manifests(output_root=None, limit=20):
    root = Path(output_root or OUTPUT_ROOT).resolve()
    if not root.is_dir():
        return []
    try:
        size = min(max(int(limit), 1), 100)
    except (TypeError, ValueError):
        size = 20
    entries = []
    for path in root.rglob('*manifest*.json'):
        if not path.is_file():
            continue
        summary = _manifest_summary(path, root)
        if summary:
            entries.append(summary)
    return sorted(entries, key=lambda item: item['updated_at'], reverse=True)[:size]


def get_run_manifest(run_id, output_root=None):
    root = Path(output_root or OUTPUT_ROOT).resolve()
    candidate = (root / unquote(str(run_id))).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if not candidate.is_file() or 'manifest' not in candidate.name:
        return None
    try:
        payload = json.loads(candidate.read_text(encoding='utf-8'))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None

def route_request(method, target, payload=None, output_root=None, job_manager=None, plugin_manager=None):
    parsed = urlparse(target)
    path = parsed.path.rstrip('/') or '/'
    jobs = job_manager or JOB_MANAGER
    plugins = plugin_manager or PLUGIN_MANAGER
    if method == 'GET' and path in {'/', '/health'}:
        return 200, {'status': 'ok', 'service': API_NAME, 'version': API_VERSION}
    if method == 'GET' and path == '/plugins':
        return 200, {'status': 'ok', 'plugins': plugins.list()}
    if method == 'GET' and path.startswith('/plugins/'):
        domain = unquote(path[len('/plugins/'):])
        item = plugins.get(domain)
        if item is None:
            return 404, {'status': 'error', 'error': f'plugin not found: {domain}'}
        return 200, {'status': 'ok', 'plugin': item}
    if method == 'POST' and path.startswith('/plugins/') and path.endswith('/enable'):
        domain = unquote(path[len('/plugins/'): -len('/enable')].rstrip('/'))
        try:
            return 200, {'status': 'ok', 'plugin': plugins.enable(domain)}
        except ValueError as exc:
            return 400, {'status': 'error', 'error': str(exc)}
    if method == 'POST' and path.startswith('/plugins/') and path.endswith('/disable'):
        domain = unquote(path[len('/plugins/'): -len('/disable')].rstrip('/'))
        try:
            return 200, {'status': 'ok', 'plugin': plugins.disable(domain)}
        except ValueError as exc:
            return 400, {'status': 'error', 'error': str(exc)}
    if method == 'GET' and path == '/tools':
        domain = parse_qs(parsed.query).get('domain', ['all'])[0]
        try:
            return 200, {'status': 'ok', 'tools': _public_specs(domain)}
        except ValueError as exc:
            return 400, {'status': 'error', 'error': str(exc)}
    if method == 'GET' and path == '/runs':
        limit = parse_qs(parsed.query).get('limit', ['20'])[0]
        return 200, {'status': 'ok', 'runs': list_run_manifests(output_root, limit)}
    if method == 'GET' and path.startswith('/runs/'):
        run_id = path[len('/runs/'):]
        manifest = get_run_manifest(run_id, output_root)
        if manifest is None:
            return 404, {'status': 'error', 'error': f'run not found: {run_id}'}
        return 200, {'status': 'ok', 'run_id': unquote(run_id), 'manifest': manifest}
    if method == 'POST' and path.startswith('/jobs/') and path.endswith('/retry'):
        job_id = unquote(path[len('/jobs/'): -len('/retry')].rstrip('/'))
        try:
            record = jobs.retry(job_id)
        except ValueError as exc:
            return 400, {'status': 'error', 'error': str(exc)}
        return 202, {'status': 'accepted', 'job': record}
    if method == 'GET' and path == '/jobs':
        limit = parse_qs(parsed.query).get('limit', ['20'])[0]
        return 200, {'status': 'ok', 'jobs': jobs.list(limit)}
    if method == 'GET' and path.startswith('/jobs/'):
        job_id = unquote(path[len('/jobs/'):])
        record = jobs.get(job_id)
        if record is None:
            return 404, {'status': 'error', 'error': f'job not found: {job_id}'}
        return 200, {'status': 'ok', 'job': record}
    if method == 'POST' and path == '/jobs':
        body = payload if isinstance(payload, dict) else {}
        try:
            name = body.get('tool')
            arguments = body.get('arguments', body.get('args', {}))
            record = jobs.submit(name, arguments)
        except (TypeError, ValueError) as exc:
            return 400, {'status': 'error', 'error': str(exc)}
        return 202, {'status': 'accepted', 'job': record}
    if method == 'POST' and (path == '/run' or path.startswith('/run/')):
        body = payload if isinstance(payload, dict) else {}
        name = body.get('tool')
        if path != '/run':
            name = unquote(path[len('/run/'):])
        arguments = body.get('arguments', body.get('args', {}))
        if not isinstance(name, str) or not name:
            return 400, {'status': 'error', 'error': 'tool is required'}
        if not isinstance(arguments, dict):
            return 400, {'status': 'error', 'error': 'arguments must be an object'}
        result = run_tool(name, arguments)
        status = 400 if isinstance(result, dict) and result.get('status') == 'error' else 200
        return status, result
    return 404, {'status': 'error', 'error': f'unknown route: {method} {path}'}


class BioAPIHandler(BaseHTTPRequestHandler):
    server_version = 'cadd-bio-agent-api/0.1.0'

    def _write(self, status, payload, headers=None):
        data = json.dumps(payload, ensure_ascii=False, default=str).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def _check_auth(self):
        if is_authorized(self.path, self.headers):
            return True
        self._write(401, {'status': 'error', 'error': 'authentication required'}, {'WWW-Authenticate': 'Bearer'})
        return False

    def do_GET(self):
        if not self._check_auth():
            return
        try:
            status, payload = route_request('GET', self.path)
        except Exception as exc:
            status, payload = 500, {'status': 'error', 'error': str(exc)}
        self._write(status, payload)

    def do_POST(self):
        if not self._check_auth():
            return
        try:
            length = int(self.headers.get('Content-Length', '0'))
            raw = self.rfile.read(length) if length else b'{}'
            payload = json.loads(raw.decode('utf-8'))
            status, result = route_request('POST', self.path, payload)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            status, result = 400, {'status': 'error', 'error': f'invalid JSON: {exc}'}
        except Exception as exc:
            status, result = 500, {'status': 'error', 'error': str(exc)}
        self._write(status, result)

    def log_message(self, *_args):
        return


def main(argv=None):
    parser = argparse.ArgumentParser(description='Run the local bioinformatics HTTP API')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8765)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), BioAPIHandler)
    print(f'{API_NAME} listening on http://{args.host}:{args.port}')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
