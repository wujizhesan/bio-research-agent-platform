import argparse
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import time
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


MARKER = 'BIOAGENT_SECURE_FULLSTACK_E2E_MARKER'


def _inside(path, root):
    return path != root and root in path.parents


def prepare_fixture(host_root, artifact_root):
    host_root = Path(host_root).resolve()
    artifact_root = Path(artifact_root).resolve()
    if not _inside(host_root, artifact_root):
        raise ValueError('secure full-stack root must be inside the artifact root')
    relative = host_root.relative_to(artifact_root)
    if not relative.parts or relative.parts[0] != 'secure-fullstack-e2e':
        raise ValueError('secure full-stack root must use secure-fullstack-e2e')
    shutil.rmtree(host_root, ignore_errors=True)
    input_dir = host_root / 'input'
    input_dir.mkdir(parents=True)
    (input_dir / 'evidence.md').write_text(
        f'# Secure full-stack evidence\n{MARKER}\n',
        encoding='utf-8',
    )
    (host_root / 'invalid-index.json').write_text(
        '{invalid secure e2e fixture',
        encoding='utf-8',
    )
    artifacts_dir = host_root / 'artifacts'
    artifacts_dir.mkdir()
    artifacts_dir.chmod(0o777)
    return artifacts_dir / 'knowledge-index.json'


def request_json(base_url, path, method='GET', data=None, token=None, form=False):
    body = None
    headers = {'Accept': 'application/json'}
    if data is not None:
        if form:
            body = urlencode(data).encode('utf-8')
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        else:
            body = json.dumps(data, ensure_ascii=True).encode('utf-8')
            headers['Content-Type'] = 'application/json'
    if token:
        headers['Authorization'] = f'Bearer {token}'
    request = Request(
        f'{base_url.rstrip("/")}{path}',
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode('utf-8'))
    except HTTPError as exc:
        raise RuntimeError(
            f'{method} {path} failed with HTTP {exc.code}'
        ) from exc


def verify(
    base_url,
    host_root,
    artifact_root,
    container_root,
    username,
    password,
    timeout_seconds=120,
    requester=None,
    sleep_fn=time.sleep,
):
    requester = requester or request_json
    output_path = prepare_fixture(host_root, artifact_root)
    token_response = requester(
        base_url,
        '/api/v1/auth/token',
        method='POST',
        data={'username': username, 'password': password},
        form=True,
    )
    token = str(token_response.get('access_token') or '')
    if not token:
        raise RuntimeError('secure full-stack authentication failed')

    def wait_for_terminal(job_id):
        deadline = time.monotonic() + max(float(timeout_seconds), 1)
        job = {}
        while time.monotonic() < deadline:
            current = requester(
                base_url,
                f'/api/v1/jobs/{job_id}',
                token=token,
            )
            job = current.get('job') or {}
            if job.get('status') in {
                'completed', 'failed', 'cancelled', 'indeterminate'
            }:
                return job
            sleep_fn(0.5)
        return job

    health = requester(base_url, '/health')
    if health.get('status') != 'ok' or health.get('job_backend') != 'redis':
        raise RuntimeError('secure full-stack health contract failed')
    project = requester(
        base_url,
        '/api/v1/projects',
        method='POST',
        data={'name': f'Secure sandbox smoke {int(time.time())}'},
        token=token,
    )
    project_id = str((project.get('project') or {}).get('project_id') or '')
    if not project_id:
        raise RuntimeError('secure full-stack project creation failed')
    container_root = PurePosixPath(container_root)
    submitted = requester(
        base_url,
        '/api/v1/jobs',
        method='POST',
        data={
            'tool': 'knowledge_ingest_directory',
            'arguments': {
                'input_dir': str(container_root / 'input'),
                'output_path': str(
                    container_root / 'artifacts' / 'knowledge-index.json'
                ),
                'extensions': ['.md'],
            },
            'project_id': project_id,
        },
        token=token,
    )
    job_id = str((submitted.get('job') or {}).get('job_id') or '')
    if not job_id:
        raise RuntimeError('secure full-stack job submission failed')
    job = wait_for_terminal(job_id)
    if job.get('status') != 'completed':
        code = str(job.get('error_code') or 'job_did_not_complete')
        raise RuntimeError(f'secure full-stack job failed with {code}')
    payload = json.loads(output_path.read_text(encoding='utf-8'))
    documents = payload.get('documents') or []
    if len(documents) != 1 or MARKER not in str(documents[0].get('text') or ''):
        raise RuntimeError('secure full-stack artifact verification failed')
    failed_submission = requester(
        base_url,
        '/api/v1/jobs',
        method='POST',
        data={
            'tool': 'knowledge_search',
            'arguments': {
                'query': 'TP53',
                'index_path': str(container_root / 'invalid-index.json'),
                'top_k': 1,
            },
            'project_id': project_id,
        },
        token=token,
    )
    failed_job_id = str(
        (failed_submission.get('job') or {}).get('job_id') or ''
    )
    if not failed_job_id:
        raise RuntimeError('secure full-stack failure job submission failed')
    failed_job = wait_for_terminal(failed_job_id)
    expected_failure = {
        'status': 'failed',
        'error_code': 'tool_execution_failed',
        'error': 'tool execution failed',
    }
    for key, expected in expected_failure.items():
        if failed_job.get(key) != expected:
            raise RuntimeError('secure full-stack error contract failed')
    serialized_failure = json.dumps(failed_job, ensure_ascii=True)
    if (
        'Expecting property name' in serialized_failure
        or 'invalid-index.json' in serialized_failure
        or '/run/bioagent/plugin-exchange' in serialized_failure
    ):
        raise RuntimeError('secure full-stack error leaked internal details')
    return {
        'status': 'ok',
        'job_id': job_id,
        'failure_job_id': failed_job_id,
        'tool': 'knowledge_ingest_directory',
        'artifact': str(output_path),
        'execution_mode': 'container',
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18080')
    parser.add_argument('--host-root', default='output/secure-fullstack-e2e')
    parser.add_argument('--artifact-root', default='output')
    parser.add_argument(
        '--container-root',
        default='/app/output/secure-fullstack-e2e',
    )
    parser.add_argument('--username', default=os.environ.get('SECURE_E2E_USERNAME', ''))
    parser.add_argument('--password', default=os.environ.get('SECURE_E2E_PASSWORD', ''))
    parser.add_argument('--timeout-seconds', type=float, default=120)
    args = parser.parse_args(argv)
    if not args.username or not args.password:
        raise SystemExit('secure E2E credentials are required')
    print(json.dumps(
        verify(
            args.base_url,
            args.host_root,
            args.artifact_root,
            args.container_root,
            args.username,
            args.password,
            timeout_seconds=args.timeout_seconds,
        ),
        ensure_ascii=True,
        sort_keys=True,
    ))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
