import argparse
import json
import os
from pathlib import Path
from statistics import median

from scripts.benchmark_sandbox_capacity import COMPOSE, cgroup_snapshot, parse_cgroup, run
from scripts.benchmark_secure_jobs import percentile
from scripts.verify_secure_mixed_resources import verify


CONFIGURATIONS = {
    'baseline': (4, 3, 4),
    'candidate': (5, 4, 5),
}
POOLS = ('plugin-sandbox', 'plugin-sandbox-heavy')


def worker_env(name):
    total, reserved, client = CONFIGURATIONS[name]
    env = os.environ.copy()
    env.update({
        'WORKER_MAX_CONCURRENCY': str(total),
        'WORKER_LIGHT_RESERVED_SLOTS': str(reserved),
        'PLUGIN_SANDBOX_CLIENT_CONCURRENCY': str(client),
    })
    return env


def restart_worker(name):
    result = run(
        (*COMPOSE, 'up', '-d', '--no-deps', '--force-recreate', '--wait', 'worker'),
        env=worker_env(name),
        timeout=240,
    )
    if result.returncode:
        raise RuntimeError(f'{name} worker restart failed: {result.stdout[-3000:]}')


def pool_snapshot(env):
    snapshots = {name: parse_cgroup(cgroup_snapshot(env, name)) for name in POOLS}
    for name, snapshot in snapshots.items():
        if snapshot.get('error'):
            raise RuntimeError(f'{name} cgroup snapshot failed: {snapshot["error"]}')
    return snapshots


def pool_delta(before, after):
    return {
        name: {
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


def summarize_rows(rows):
    summary = {}
    for name in CONFIGURATIONS:
        selected = [row for row in rows if row['configuration'] == name]
        if not selected:
            continue
        light = [value for row in selected for value in row['result']['light_queue_seconds']]
        heavy = [value for row in selected for value in row['result']['heavy_server_seconds']]
        summary[name] = {
            'rounds': len(selected),
            'light_jobs': len(light),
            'heavy_jobs': len(heavy),
            'light_queue_p95_seconds': round(percentile(light, 95), 3),
            'heavy_server_p95_seconds': round(percentile(heavy, 95), 3),
            'light_round_p95_median_seconds': round(median(
                row['result']['light_queue_p95_seconds'] for row in selected
            ), 3),
            'heavy_round_p95_median_seconds': round(median(
                row['result']['heavy_server_p95_seconds'] for row in selected
            ), 3),
            'light_overlap_count': sum(row['result']['light_overlap_count'] for row in selected),
            'oom_kill_delta': sum(
                values['oom_kill_delta']
                for row in selected for values in row['pools'].values()
            ),
            'light_cpu_throttled_usec': sum(
                row['pools']['plugin-sandbox']['cpu_throttled_usec'] for row in selected
            ),
        }
    if len(summary) == len(CONFIGURATIONS):
        baseline = summary['baseline']
        candidate = summary['candidate']
        baseline_rounds = {
            row['round']: row for row in rows if row['configuration'] == 'baseline'
        }
        candidate_rounds = {
            row['round']: row for row in rows if row['configuration'] == 'candidate'
        }
        paired_rounds = sorted(baseline_rounds.keys() & candidate_rounds.keys())
        paired_wins = sum(
            candidate_rounds[index]['result']['light_queue_p95_seconds']
            < baseline_rounds[index]['result']['light_queue_p95_seconds']
            for index in paired_rounds
        )
        required_wins = (max(baseline['rounds'], candidate['rounds']) * 2 + 2) // 3
        summary['comparison'] = {
            'light_queue_improvement_percent': round(
                100 * (1 - candidate['light_queue_p95_seconds'] / baseline['light_queue_p95_seconds']), 1
            ),
            'heavy_server_change_percent': round(
                100 * (candidate['heavy_server_p95_seconds'] / baseline['heavy_server_p95_seconds'] - 1), 1
            ),
            'light_round_wins': paired_wins,
            'paired_rounds': len(paired_rounds),
            'required_light_round_wins': required_wins,
            'promote_candidate': bool(
                len(paired_rounds) == baseline['rounds'] == candidate['rounds']
                and baseline['rounds'] >= 2
                and paired_wins >= required_wins
                and candidate['light_queue_p95_seconds'] <= baseline['light_queue_p95_seconds'] * 0.9
                and candidate['heavy_server_p95_seconds'] <= baseline['heavy_server_p95_seconds'] * 1.1
                and candidate['oom_kill_delta'] == 0
            ),
        }
    return summary


def benchmark(args):
    rows = []
    error = None
    restore_error = None
    current = None
    try:
        for index in range(args.rounds):
            order = ('baseline', 'candidate') if index % 2 == 0 else ('candidate', 'baseline')
            for name in order:
                if current != name:
                    restart_worker(name)
                    current = name
                env = worker_env(name)
                before = pool_snapshot(env)
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
                after = pool_snapshot(env)
                row = {
                    'round': index,
                    'configuration': name,
                    'result': result,
                    'pools': pool_delta(before, after),
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
            restart_worker('baseline')
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
    parser.add_argument('--output', type=Path, default=Path('output/worker-slot-benchmark.json'))
    args = parser.parse_args(argv)
    if not args.username or not args.password:
        parser.error('benchmark credentials are required')
    if min(args.rounds, args.light_count, args.heavy_count, args.timeout_seconds) <= 0:
        parser.error('rounds, counts, and timeout must be positive')
    return benchmark(args)


if __name__ == '__main__':
    raise SystemExit(main())
