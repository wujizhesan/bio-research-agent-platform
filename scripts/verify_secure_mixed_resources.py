"""Exercise heavy and lightweight tools through the secure container stack."""

import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import time

from scripts.verify_secure_fullstack_e2e import request_json


def verify(base_url, host_root, container_root, username, password, timeout_seconds=120):
    host_root = Path(host_root).resolve()
    source = Path(__file__).resolve().parent.parent / 'examples' / 'rnaseq'
    input_dir = host_root / 'input'
    input_dir.mkdir(parents=True, exist_ok=True)
    for name in ('expression.csv', 'metadata.csv', 'gene_sets.csv'):
        shutil.copy2(source / name, input_dir / name)
    token = request_json(
        base_url,
        '/api/v1/auth/token',
        method='POST',
        data={'username': username, 'password': password},
        form=True,
    ).get('access_token')
    if not token:
        raise RuntimeError('secure mixed-resource authentication failed')
    project = request_json(
        base_url,
        '/api/v1/projects',
        method='POST',
        data={'name': f'Sandbox resource smoke {int(time.time())}'},
        token=token,
    )
    project_id = str((project.get('project') or {}).get('project_id') or '')
    if not project_id:
        raise RuntimeError('secure mixed-resource project creation failed')
    container_root = PurePosixPath(container_root)
    profiles = {
        'heavy': {
            'tool': 'omics_run_analysis',
            'arguments': {
                'expression_csv': str(container_root / 'input' / 'expression.csv'),
                'metadata_csv': str(container_root / 'input' / 'metadata.csv'),
                'gene_sets_csv': str(container_root / 'input' / 'gene_sets.csv'),
                'output_dir': str(container_root / 'artifacts' / 'omics'),
                'statistics_backend': 'scipy',
            },
        },
        'light': {
            'tool': 'omics_inspect_toolchain',
            'arguments': {},
        },
    }

    def submit(profile):
        submitted = request_json(
            base_url,
            '/api/v1/jobs',
            method='POST',
            data={**profile, 'project_id': project_id},
            token=token,
        )
        job_id = str((submitted.get('job') or {}).get('job_id') or '')
        if not job_id:
            raise RuntimeError('secure mixed-resource submission lacked a job ID')
        return job_id

    def wait_for_terminal(job_id):
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            job = (request_json(
                base_url,
                f'/api/v1/jobs/{job_id}',
                token=token,
            ).get('job') or {})
            if job.get('status') in {
                'completed', 'failed', 'cancelled', 'indeterminate'
            }:
                return job
            time.sleep(0.25)
        raise TimeoutError(f'secure mixed-resource job {job_id} timed out')

    with ThreadPoolExecutor(max_workers=2) as pool:
        submissions = {
            name: pool.submit(submit, profile)
            for name, profile in profiles.items()
        }
        job_ids = {name: future.result() for name, future in submissions.items()}
        observations = {
            name: pool.submit(wait_for_terminal, job_id)
            for name, job_id in job_ids.items()
        }
        jobs = {name: future.result() for name, future in observations.items()}
    for name, job in jobs.items():
        if job.get('status') != 'completed':
            raise RuntimeError(
                f"secure mixed-resource {name} job failed with "
                f"{job.get('error_code') or job.get('status')}"
            )
    report = host_root / 'artifacts' / 'omics' / 'omics_report.md'
    if not report.is_file():
        raise RuntimeError('secure mixed-resource omics report is missing')
    return {
        'status': 'ok',
        'heavy_job_id': job_ids['heavy'],
        'light_job_id': job_ids['light'],
        'artifact': str(report),
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18080')
    parser.add_argument('--host-root', default='output/secure-fullstack-e2e')
    parser.add_argument(
        '--container-root', default='/app/output/secure-fullstack-e2e'
    )
    parser.add_argument('--username', default=os.environ.get('SECURE_E2E_USERNAME', ''))
    parser.add_argument('--password', default=os.environ.get('SECURE_E2E_PASSWORD', ''))
    parser.add_argument('--timeout-seconds', type=float, default=120)
    args = parser.parse_args(argv)
    if not args.username or not args.password:
        raise SystemExit('secure E2E credentials are required')
    print(json.dumps(verify(
        args.base_url,
        args.host_root,
        args.container_root,
        args.username,
        args.password,
        args.timeout_seconds,
    ), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
