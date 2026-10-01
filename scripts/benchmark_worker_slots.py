import argparse
import json
import math
import os
from pathlib import Path
import re
from statistics import median
from urllib.request import urlopen

from scripts.benchmark_sandbox_capacity import COMPOSE, cgroup_snapshot, parse_cgroup, run
from scripts.benchmark_secure_jobs import percentile
from scripts.verify_secure_mixed_resources import verify


CONFIGURATIONS = {
    'baseline': (4, 3, 4, 4, 2.0, 2.0),
    'candidate': (5, 4, 5, 5, 2.0, 2.0),
    'balanced': (5, 4, 5, 5, 2.5, 1.5),
}
POOLS = ('plugin-sandbox', 'plugin-sandbox-heavy')
CAPACITY_REJECTION_SAMPLE = re.compile(
    r'^bio_agent_redis_worker_capacity_rejections_total(?:\{[^}]*\})?\s+([\d.eE+-]+)(?:\s|$)'
)


def configuration_env(name):
    total, reserved, client, cpu_capacity, light_cpus, heavy_cpus = CONFIGURATIONS[name]
    env = os.environ.copy()
    env.update({
        'SECURE_WORKER_MAX_CONCURRENCY': str(total),
        'SECURE_WORKER_LIGHT_RESERVED_SLOTS': str(reserved),
        'SECURE_PLUGIN_SANDBOX_CLIENT_CONCURRENCY': str(client),
        'SECURE_WORKER_TOTAL_CPU_CORES': str(cpu_capacity),
        'PLUGIN_SANDBOX_CPUS': str(light_cpus),
        'PLUGIN_SANDBOX_HEAVY_CPUS': str(heavy_cpus),
    })
    return env


def restart_configuration(name=None):
    env = configuration_env(name) if name else os.environ.copy()
    for service in (*POOLS, 'worker'):
        result = run(
            (*COMPOSE, 'up', '-d', '--no-deps', '--force-recreate', '--wait', service),
            env=env,
            timeout=240,
        )
        if result.returncode:
            raise RuntimeError(
                f'{name or "deployment"} {service} restart failed: {result.stdout[-3000:]}'
            )


def pool_snapshot(env):
    snapshots = {name: parse_cgroup(cgroup_snapshot(env, name)) for name in POOLS}
    for name, snapshot in snapshots.items():
        if snapshot.get('error'):
            raise RuntimeError(f'{name} cgroup snapshot failed: {snapshot["error"]}')
    return snapshots


def pool_delta(before, after):
    return {
        name: {
            'cpu_limit_cores': after[name]['cpu_limit_cores'],
            'oom_kill_delta': (
                after[name]['memory_events'].get('oom_kill', 0)
                - before[name]['memory_events'].get('oom_kill', 0)
            ),
            'cpu_throttled_usec': (
                after[name]['cpu_stat'].get('throttled_usec', 0)
                - before[name]['cpu_stat'].get('throttled_usec', 0)
            ),
        }
        for name in POOLS
    }


def parse_capacity_rejections(metrics_text):
    return sum(
        float(match.group(1))
        for line in metrics_text.splitlines()
        if (match := CAPACITY_REJECTION_SAMPLE.match(line))
    )


def capacity_rejections(url):
    with urlopen(url, timeout=10) as response:
        return parse_capacity_rejections(response.read().decode('utf-8'))


def parse_tool_phase_events(log_text, tool):
    samples = []
    for line in log_text.splitlines():
        start = line.find('{')
        if start < 0:
            continue
        try:
            event = json.loads(line[start:])
        except json.JSONDecodeError:
            continue
        if event.get('event') != 'tool.execution.completed' or event.get('tool') != tool:
            continue
        if event.get('status') != 'success':
            raise RuntimeError(f'{tool} phase trace contains a failed execution')
        values = {'duration': event.get('duration_seconds')}
        values.update(event.get('phase_seconds') or {})
        values.update(event.get('boundary_seconds') or {})
        samples.append({
            key: float(value) for key, value in values.items()
            if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0
        })
    return samples


def collect_tool_phases(env, service, tool, expected_count):
    container = run((*COMPOSE, 'ps', '-q', service), env=env)
    container_id = container.stdout.strip()
    if container.returncode or not container_id:
        raise RuntimeError(f'{service} container ID is unavailable')
    logs = run(('docker', 'logs', container_id), env=env, timeout=60)
    if logs.returncode:
        raise RuntimeError(f'{service} logs are unavailable: {logs.stdout[-1000:]}')
    samples = parse_tool_phase_events(logs.stdout, tool)
    if len(samples) != expected_count:
        raise RuntimeError(
            f'{service} emitted {len(samples)} {tool} phase traces, expected {expected_count}'
        )
    seconds = {
        phase: [sample[phase] for sample in samples if phase in sample]
        for phase in sorted({phase for sample in samples for phase in sample})
    }
    return {
        'count': len(samples),
        'p95_seconds': {
            phase: round(percentile(values, 95), 3)
            for phase, values in seconds.items()
        },
        'seconds': seconds,
    }


def compare_configurations(rows, summary, baseline_name, candidate_name):
    baseline = summary[baseline_name]
    candidate = summary[candidate_name]
    baseline_rounds = {
        row['round']: row for row in rows if row['configuration'] == baseline_name
    }
    candidate_rounds = {
        row['round']: row for row in rows if row['configuration'] == candidate_name
    }
    paired_rounds = sorted(baseline_rounds.keys() & candidate_rounds.keys())
    queue_wins = sum(
        candidate_rounds[index]['result']['light_queue_p95_seconds']
        < baseline_rounds[index]['result']['light_queue_p95_seconds']
        for index in paired_rounds
    )
    server_wins = sum(
        candidate_rounds[index]['result']['light_server_p95_seconds']
        < baseline_rounds[index]['result']['light_server_p95_seconds']
        for index in paired_rounds
    )
    required_wins = (max(baseline['rounds'], candidate['rounds']) * 2 + 2) // 3
    return {
        'light_queue_improvement_percent': round(
            100 * (1 - candidate['light_queue_p95_seconds'] / baseline['light_queue_p95_seconds']), 1
        ),
        'heavy_server_change_percent': round(
            100 * (candidate['heavy_server_p95_seconds'] / baseline['heavy_server_p95_seconds'] - 1), 1
        ),
        'light_server_improvement_percent': round(
            100 * (1 - candidate['light_server_p95_seconds'] / baseline['light_server_p95_seconds']), 1
        ),
        'light_round_wins': queue_wins,
        'light_server_round_wins': server_wins,
        'paired_rounds': len(paired_rounds),
        'required_light_round_wins': required_wins,
        'promote_candidate': bool(
            len(paired_rounds) == baseline['rounds'] == candidate['rounds']
            and baseline['rounds'] >= 2
            and queue_wins >= required_wins
            and server_wins >= required_wins
            and candidate['light_queue_p95_seconds'] <= baseline['light_queue_p95_seconds'] * 0.9
            and candidate['light_server_p95_seconds'] <= baseline['light_server_p95_seconds'] * 0.95
            and candidate['heavy_server_p95_seconds'] <= baseline['heavy_server_p95_seconds'] * 1.05
            and candidate['max_light_running_while_heavy'] >= 4
            and candidate['capacity_rejections'] <= baseline['capacity_rejections']
            and candidate['oom_kill_delta'] == 0
        ),
    }


def summarize_rows(rows):
    summary = {}
    for name in CONFIGURATIONS:
        selected = [row for row in rows if row['configuration'] == name]
        if not selected:
            continue
        light = [value for row in selected for value in row['result']['light_queue_seconds']]
        light_server = [value for row in selected for value in row['result']['light_server_seconds']]
        heavy = [value for row in selected for value in row['result']['heavy_server_seconds']]
        summary[name] = {
            'rounds': len(selected),
            'light_jobs': len(light),
            'heavy_jobs': len(heavy),
            'light_queue_p95_seconds': round(percentile(light, 95), 3),
            'light_server_p95_seconds': round(percentile(light_server, 95), 3),
            'heavy_server_p95_seconds': round(percentile(heavy, 95), 3),
            'light_round_p95_median_seconds': round(median(
                row['result']['light_queue_p95_seconds'] for row in selected
            ), 3),
            'heavy_round_p95_median_seconds': round(median(
                row['result']['heavy_server_p95_seconds'] for row in selected
            ), 3),
            'light_execution_round_p95_median_seconds': round(median(
                row['result']['light_execution_p95_seconds'] for row in selected
            ), 3),
            'light_server_round_p95_median_seconds': round(median(
                row['result']['light_server_p95_seconds'] for row in selected
            ), 3),
            'light_overlap_count': sum(row['result']['light_overlap_count'] for row in selected),
            'max_light_running_while_heavy': max(
                row['result']['max_light_running_while_heavy'] for row in selected
            ),
            'capacity_rejections': sum(row['capacity_rejections'] for row in selected),
            'oom_kill_delta': sum(
                values['oom_kill_delta']
                for row in selected for values in row['pools'].values()
            ),
            'light_cpu_throttled_usec': sum(
                row['pools']['plugin-sandbox']['cpu_throttled_usec'] for row in selected
            ),
        }
        for pool in ('light', 'heavy'):
            phase_samples = {}
            for row in selected:
                for phase, values in (row.get('tool_phases') or {}).get(pool, {}).get('seconds', {}).items():
                    phase_samples.setdefault(phase, []).extend(values)
            if phase_samples:
                summary[name][f'{pool}_phase_p95_seconds'] = {
                    phase: round(percentile(values, 95), 3)
                    for phase, values in sorted(phase_samples.items())
                }
    if 'baseline' in summary and 'candidate' in summary:
        summary['comparison'] = compare_configurations(rows, summary, 'baseline', 'candidate')
    if 'baseline' in summary and 'balanced' in summary:
        summary['balanced_comparison'] = compare_configurations(
            rows, summary, 'baseline', 'balanced'
        )
    if 'candidate' in summary and 'balanced' in summary:
        allocation = compare_configurations(rows, summary, 'candidate', 'balanced')
        allocation['prefer_balanced'] = bool(
            allocation['light_server_round_wins'] >= allocation['required_light_round_wins']
            and allocation['light_server_improvement_percent'] >= 5
            and allocation['heavy_server_change_percent'] <= 5
            and summary['balanced']['capacity_rejections'] <= summary['candidate']['capacity_rejections']
            and summary['balanced']['oom_kill_delta'] == 0
        )
        summary['allocation_comparison'] = allocation
    return summary


def benchmark(args):
    rows = []
    error = None
    restore_error = None
    current = None
    try:
        names = tuple(CONFIGURATIONS)
        for index in range(args.rounds):
            order = names[index % len(names):] + names[:index % len(names)]
            for name in order:
                if current != name:
                    restart_configuration(name)
                    current = name
                env = configuration_env(name)
                before = pool_snapshot(env)
                for service, expected in zip(POOLS, CONFIGURATIONS[name][4:]):
                    actual = before[service]['cpu_limit_cores']
                    if actual is not None and abs(actual - expected) > 0.01:
                        raise RuntimeError(f'{name} {service} CPU limit is {actual}, expected {expected}')
                rejected_before = capacity_rejections(args.worker_metrics_url)
                result = verify(
                    args.base_url,
                    args.host_root,
                    args.container_root,
                    args.username,
                    args.password,
                    timeout_seconds=args.timeout_seconds,
                    light_count=args.light_count,
                    heavy_count=args.heavy_count,
                    artifact_prefix=f'worker-slots-{name}-{index}',
                )
                tool_phases = {
                    'light': collect_tool_phases(
                        env, 'plugin-sandbox', 'omics_inspect_toolchain', args.light_count
                    ),
                    'heavy': collect_tool_phases(
                        env, 'plugin-sandbox-heavy', 'omics_run_analysis', args.heavy_count
                    ),
                }
                after = pool_snapshot(env)
                rejected_after = capacity_rejections(args.worker_metrics_url)
                if rejected_after < rejected_before:
                    raise RuntimeError(f'{name} worker metrics counter reset during round {index}')
                row = {
                    'round': index,
                    'configuration': name,
                    'result': result,
                    'tool_phases': tool_phases,
                    'pools': pool_delta(before, after),
                    'capacity_rejections': int(rejected_after - rejected_before),
                }
                rows.append(row)
                if result['light_overlap_count'] != args.light_count:
                    raise RuntimeError(f'{name} round {index} lost heavy/light overlap')
                if any(pool['oom_kill_delta'] for pool in row['pools'].values()):
                    raise RuntimeError(f'{name} round {index} killed a sandbox process')
    except Exception as exc:
        error = f'{type(exc).__name__}: {exc}'
    finally:
        try:
            restart_configuration()
        except Exception as exc:
            restore_error = f'{type(exc).__name__}: {exc}'
        report = {
            'version': 1,
            'configurations': CONFIGURATIONS,
            'rows': rows,
            'summary': summarize_rows(rows),
            'error': error,
            'restore_error': restore_error,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, sort_keys=True), encoding='utf-8')
    print(json.dumps(report['summary'], sort_keys=True))
    return int(bool(error or restore_error))


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:18080')
    parser.add_argument('--host-root', default='output/secure-fullstack-e2e')
    parser.add_argument('--container-root', default='/app/output/secure-fullstack-e2e')
    parser.add_argument('--username', default=os.environ.get('SECURE_E2E_USERNAME', ''))
    parser.add_argument('--password', default=os.environ.get('SECURE_E2E_PASSWORD', ''))
    parser.add_argument('--rounds', type=int, default=3)
    parser.add_argument('--light-count', type=int, default=8)
    parser.add_argument('--heavy-count', type=int, default=4)
    parser.add_argument('--timeout-seconds', type=float, default=120)
    parser.add_argument('--worker-metrics-url', default='http://127.0.0.1:9000/metrics')
    parser.add_argument('--output', type=Path, default=Path('output/worker-slot-benchmark.json'))
    args = parser.parse_args(argv)
    if not args.username or not args.password:
        parser.error('benchmark credentials are required')
    if min(args.rounds, args.light_count, args.heavy_count, args.timeout_seconds) <= 0:
        parser.error('rounds, counts, and timeout must be positive')
    return benchmark(args)


if __name__ == '__main__':
    raise SystemExit(main())
