"""Exercise heavy and lightweight tools through the secure container stack."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import time

from scripts.verify_secure_fullstack_e2e import request_json
from scripts.benchmark_secure_jobs import job_timings, summarize


def verify(base_url, host_root, container_root, username, password,
           timeout_seconds=120, light_count=8, heavy_count=4,
           artifact_prefix='omics-heavy', heavy_running_target=1):
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
    heavy_profiles = [
        {
            'tool': 'omics_run_analysis',
            'arguments': {
                'expression_csv': str(container_root / 'input' / 'expression.csv'),
                'metadata_csv': str(container_root / 'input' / 'metadata.csv'),
                'gene_sets_csv': str(container_root / 'input' / 'gene_sets.csv'),
                'output_dir': str(container_root / 'artifacts' / f'{artifact_prefix}-{index}'),
                'statistics_backend': 'scipy',
            },
        }
        for index in range(heavy_count)
    ]
    light_profile = {'tool': 'omics_inspect_toolchain', 'arguments': {}}

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

    with ThreadPoolExecutor(max_workers=light_count + heavy_count) as pool:
        heavy_submissions = [pool.submit(submit, profile) for profile in heavy_profiles]
        heavy_ids = [future.result() for future in heavy_submissions]
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            running_at_submission = sum(
                (request_json(base_url, f'/api/v1/jobs/{job_id}', token=token).get('job') or {}).get('status') == 'running'
                for job_id in heavy_ids
            )
            if running_at_submission >= heavy_running_target:
                break
            time.sleep(0.1)
        else:
            raise TimeoutError('heavy jobs did not reach the expected running capacity')
        light_submissions = [
            pool.submit(submit, light_profile) for _ in range(light_count)
        ]
        light_ids = [future.result() for future in light_submissions]
        observations = {
            job_id: pool.submit(wait_for_terminal, job_id)
            for job_id in (*heavy_ids, *light_ids)
        }
        jobs = {job_id: future.result() for job_id, future in observations.items()}
    for job_id, job in jobs.items():
        if job.get('status') != 'completed':
            raise RuntimeError(
                f"secure mixed-resource {job_id} job failed with "
                f"{job.get('error_code') or job.get('status')}"
            )
    heavy_jobs = [jobs[job_id] for job_id in heavy_ids]
    heavy_timings = [job_timings(job, 0) for job in heavy_jobs]
    heavy_intervals = [
        (datetime.fromisoformat(job['started_at']), datetime.fromisoformat(job['finished_at']))
        for job in heavy_jobs
    ]
    light_jobs = [jobs[job_id] for job_id in light_ids]
    light_timings = [job_timings(job, 0) for job in light_jobs]
    overlap_count = sum(
        any(
            datetime.fromisoformat(job['started_at']) < finished
            and datetime.fromisoformat(job['finished_at']) > started
            for started, finished in heavy_intervals
        )
        for job in light_jobs
    )
    reports = [
        host_root / 'artifacts' / f'{artifact_prefix}-{index}' / 'omics_report.md'
        for index in range(heavy_count)
    ]
    if any(not report.is_file() for report in reports):
        raise RuntimeError('secure mixed-resource omics report is missing')
    return {
        'status': 'ok',
        'heavy_job_ids': heavy_ids,
        'heavy_running_at_light_submission': running_at_submission,
        'light_job_ids': light_ids,
        'heavy_execution_p95_seconds': summarize(heavy_timings, 'execution_seconds')['p95'],
        'heavy_server_p95_seconds': summarize(heavy_timings, 'server_total_seconds')['p95'],
        'heavy_server_seconds': [timing['server_total_seconds'] for timing in heavy_timings],
        'light_queue_p95_seconds': summarize(light_timings, 'queue_seconds')['p95'],
        'light_queue_seconds': [timing['queue_seconds'] for timing in light_timings],
        'light_queue_phase_p95_seconds': {
            phase: summarize(light_timings, f'{phase}_seconds')['p95']
            for phase in ('submission', 'outbox_wait', 'dispatch', 'worker_wait')
        },
        'light_overlap_count': overlap_count,
        'artifacts': [str(report) for report in reports],
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
    parser.add_argument('--light-count', type=int, default=8)
    parser.add_argument('--heavy-count', type=int, default=4)
    parser.add_argument('--artifact-prefix', default='omics-heavy')
    parser.add_argument('--heavy-running-target', type=int, default=1)
    args = parser.parse_args(argv)
    if not args.username or not args.password:
        raise SystemExit('secure E2E credentials are required')
    if args.light_count < 1 or args.heavy_count < 1:
        parser.error('light and heavy counts must be positive')
    if not 1 <= args.heavy_running_target <= args.heavy_count:
        parser.error('heavy running target must be between one and heavy count')
    if not args.artifact_prefix.replace('-', '').replace('_', '').isalnum():
        parser.error('artifact prefix must contain only letters, digits, hyphens, and underscores')
    print(json.dumps(verify(
        args.base_url,
        args.host_root,
        args.container_root,
        args.username,
        args.password,
        args.timeout_seconds,
        args.light_count,
        args.heavy_count,
        args.artifact_prefix,
        args.heavy_running_target,
    ), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
