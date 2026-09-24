import argparse
import json
import os
from pathlib import Path
import subprocess
import sys


COMPOSE = ('docker', 'compose', '-f', 'docker-compose.yml', '-f', 'docker-compose.secure.yml')
CONFIGURATIONS = (
    (2, 2048, 4096),
    (3, 1536, 4608),
    (4, 1024, 4096),
)
DEFAULT_CONFIGURATION = CONFIGURATIONS[-1]
CGROUP_FILES = ('memory.current', 'memory.peak', 'memory.events', 'cpu.stat', 'pids.peak')


def run(command, *, env=None, timeout=180):
    return subprocess.run(
        command,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=timeout,
        check=False,
    )


def configuration_env(concurrency, light_memory_limit_mb, memory_budget_mb):
    env = os.environ.copy()
    env.update({
        'WORKER_MAX_CONCURRENCY': str(concurrency),
        'PLUGIN_SANDBOX_CLIENT_CONCURRENCY': str(concurrency),
        'PLUGIN_SANDBOX_MAX_CONCURRENCY': str(concurrency),
        'PLUGIN_SANDBOX_LIGHT_MEMORY_LIMIT_MB': str(light_memory_limit_mb),
        'PLUGIN_SANDBOX_MEMORY_BUDGET_MB': str(memory_budget_mb),
    })
    return env


def restart_services(env):
    for service in ('plugin-sandbox', 'worker'):
        result = run(
            (*COMPOSE, 'up', '-d', '--no-deps', '--force-recreate', '--wait', service),
            env=env,
            timeout=240,
        )
        if result.returncode:
            raise RuntimeError(f'{service} restart failed: {result.stdout[-3000:]}')


def cgroup_snapshot(env, service='plugin-sandbox'):
    command = (
        'import json; from pathlib import Path; '
        f'names={CGROUP_FILES!r}; '
        'print(json.dumps({name: (Path("/sys/fs/cgroup") / name).read_text().strip() '
        'if (Path("/sys/fs/cgroup") / name).exists() else None for name in names}))'
    )
    result = run((*COMPOSE, 'exec', '-T', service, 'python', '-c', command), env=env)
    if result.returncode:
        return {'error': result.stdout[-1000:]}
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return {'error': result.stdout[-1000:]}


def parse_cgroup(snapshot):
    values = {}
    for name in ('memory.current', 'memory.peak', 'pids.peak'):
        raw = snapshot.get(name)
        values[name.replace('.', '_')] = int(raw) if raw and raw.isdigit() else None
    for name in ('memory.events', 'cpu.stat'):
        values[name.replace('.', '_')] = {
            line.split()[0]: int(line.split()[1])
            for line in (snapshot.get(name) or '').splitlines()
            if len(line.split()) == 2 and line.split()[1].isdigit()
        }
    if 'error' in snapshot:
        values['error'] = snapshot['error']
    return values


def sandbox_oom_killed(env):
    container = run((*COMPOSE, 'ps', '-q', 'plugin-sandbox'), env=env)
    container_id = container.stdout.strip()
    if container.returncode or not container_id:
        return None
    inspection = run(('docker', 'inspect', '--format', '{{.State.OOMKilled}}', container_id))
    if inspection.returncode:
        return None
    return inspection.stdout.strip().lower() == 'true'


def scenario_summary(matrix):
    return {
        scenario['name']: {
            'completed': scenario['completed_count'],
            'failed': scenario['failed_count'],
            'throughput_jobs_per_second': scenario['throughput_jobs_per_second'],
            'queue_p95_seconds': (scenario['summary_seconds']['queue_seconds'] or {}).get('p95'),
            'server_p95_seconds': (scenario['summary_seconds']['server_total_seconds'] or {}).get('p95'),
        }
        for scenario in matrix['scenarios']
    }


def benchmark(args, concurrency, env):
    matrix_path = args.output.parent / f'sandbox-capacity-{concurrency}-matrix.json'
    matrix_path.unlink(missing_ok=True)
    result = run((
        sys.executable, '-m', 'scripts.benchmark_workload_matrix',
        '--base-url', args.base_url,
        '--index-path', args.index_path,
        '--samples', str(args.samples),
        '--concurrency', str(args.load_concurrency),
        '--output', str(matrix_path),
    ), env=env, timeout=600)
    matrix = json.loads(matrix_path.read_text(encoding='utf-8')) if matrix_path.exists() else None
    return {
        'exit_code': result.returncode,
        'output_tail': result.stdout[-3000:],
        'matrix_path': str(matrix_path) if matrix else None,
        'scenarios': scenario_summary(matrix) if matrix else None,
    }


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18080')
    parser.add_argument('--index-path', required=True)
    parser.add_argument('--samples', type=int, default=24)
    parser.add_argument('--load-concurrency', type=int, default=8)
    parser.add_argument('--output', type=Path, default=Path('output/sandbox-capacity.json'))
    args = parser.parse_args(argv)
    if args.samples < 1 or args.load_concurrency < 1:
        parser.error('samples and load concurrency must be positive')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    restore_error = None
    try:
        for concurrency, light_limit, memory_budget in CONFIGURATIONS:
            env = configuration_env(concurrency, light_limit, memory_budget)
            row = {
                'concurrency': concurrency,
                'light_memory_limit_mb': light_limit,
                'memory_budget_mb': memory_budget,
            }
            rows.append(row)
            try:
                restart_services(env)
                before = parse_cgroup(cgroup_snapshot(env))
                row['benchmark'] = benchmark(args, concurrency, env)
                after = parse_cgroup(cgroup_snapshot(env))
                row['sandbox_cgroup'] = after
                row['sandbox_oom_killed'] = sandbox_oom_killed(env)
                row['cpu_usage_usec'] = (
                    after['cpu_stat'].get('usage_usec', 0)
                    - before['cpu_stat'].get('usage_usec', 0)
                )
                row['cpu_throttled_usec'] = (
                    after['cpu_stat'].get('throttled_usec', 0)
                    - before['cpu_stat'].get('throttled_usec', 0)
                )
            except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError) as exc:
                row['error'] = str(exc)
                row['sandbox_cgroup'] = parse_cgroup(cgroup_snapshot(env))
                row['sandbox_oom_killed'] = sandbox_oom_killed(env)
            print(json.dumps(row, sort_keys=True), flush=True)
    finally:
        try:
            restart_services(configuration_env(*DEFAULT_CONFIGURATION))
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            restore_error = str(exc)
        report = {'version': 1, 'rows': rows, 'restore_error': restore_error}
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
    default = next(row for row in rows if row['concurrency'] == DEFAULT_CONFIGURATION[0])
    benchmark_result = default.get('benchmark') or {}
    oom_count = (default.get('sandbox_cgroup') or {}).get('memory_events', {}).get('oom_kill', 0)
    return int(bool(
        restore_error or default.get('error') or benchmark_result.get('exit_code') != 0
        or oom_count or default.get('sandbox_oom_killed')
    ))


if __name__ == '__main__':
    raise SystemExit(main())
