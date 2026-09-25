import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from scripts.benchmark_worker_slots import (
    configuration_env,
    parse_capacity_rejections,
    parse_tool_phase_events,
    restart_configuration,
    summarize_rows,
)
from scripts.benchmark_sandbox_capacity import parse_cgroup
from scripts.verify_secure_mixed_resources import peak_light_running_while_heavy


class WorkerSlotBenchmarkTests(unittest.TestCase):
    def test_promotion_requires_light_gain_and_heavy_guardrail(self):
        rows = []
        for index in range(3):
            for name, light, heavy in (
                ('baseline', 3.0, 10.0),
                ('candidate', 2.0, 10.5),
                ('balanced', 1.8, 10.3),
            ):
                rows.append({
                    'round': index,
                    'configuration': name,
                    'result': {
                        'light_queue_seconds': [light] * 8,
                        'light_server_seconds': [light + 1] * 8,
                        'heavy_server_seconds': [heavy] * 4,
                        'light_queue_p95_seconds': light,
                        'light_server_p95_seconds': light + 1,
                        'heavy_server_p95_seconds': heavy,
                        'light_execution_p95_seconds': 1.0,
                        'light_overlap_count': 8,
                        'max_light_running_while_heavy': 3 if name == 'baseline' else 4,
                    },
                    'capacity_rejections': 0,
                    'pools': {
                        'plugin-sandbox': {'oom_kill_delta': 0, 'cpu_throttled_usec': 5},
                        'plugin-sandbox-heavy': {'oom_kill_delta': 0, 'cpu_throttled_usec': 0},
                    },
                })
        summary = summarize_rows(rows)
        self.assertEqual(summary['baseline']['light_jobs'], 24)
        self.assertEqual(summary['candidate']['heavy_jobs'], 12)
        self.assertEqual(summary['comparison']['light_round_wins'], 3)
        self.assertTrue(summary['comparison']['promote_candidate'])
        self.assertTrue(summary['balanced_comparison']['promote_candidate'])
        self.assertTrue(summary['allocation_comparison']['prefer_balanced'])

        for row in rows:
            if row['configuration'] == 'candidate':
                row['result']['heavy_server_seconds'] = [12.0] * 4
                row['result']['heavy_server_p95_seconds'] = 12.0
        self.assertFalse(summarize_rows(rows)['comparison']['promote_candidate'])

        for row in rows:
            if row['configuration'] == 'candidate':
                row['result']['heavy_server_seconds'] = [10.5] * 4
                row['result']['heavy_server_p95_seconds'] = 10.5
                row['result']['light_server_seconds'] = [5.0] * 8
                row['result']['light_server_p95_seconds'] = 5.0
        self.assertFalse(summarize_rows(rows)['comparison']['promote_candidate'])

        for row in rows:
            if row['configuration'] == 'balanced':
                row['result']['heavy_server_seconds'] = [11.0] * 4
                row['result']['heavy_server_p95_seconds'] = 11.0
        self.assertFalse(summarize_rows(rows)['balanced_comparison']['promote_candidate'])

    def test_candidate_includes_the_extra_logical_cpu(self):
        self.assertEqual(configuration_env('baseline')['SECURE_WORKER_TOTAL_CPU_CORES'], '4')
        self.assertEqual(configuration_env('candidate')['SECURE_WORKER_TOTAL_CPU_CORES'], '5')
        self.assertEqual(configuration_env('balanced')['PLUGIN_SANDBOX_CPUS'], '2.5')
        self.assertEqual(configuration_env('balanced')['PLUGIN_SANDBOX_HEAVY_CPUS'], '1.5')

    def test_configuration_restarts_both_sandboxes_before_worker(self):
        with patch('scripts.benchmark_worker_slots.run', return_value=SimpleNamespace(returncode=0)) as run:
            restart_configuration('balanced')
        self.assertEqual(
            [call.args[0][-1] for call in run.call_args_list],
            ['plugin-sandbox', 'plugin-sandbox-heavy', 'worker'],
        )
        self.assertTrue(all(
            call.kwargs['env']['PLUGIN_SANDBOX_CPUS'] == '2.5'
            for call in run.call_args_list
        ))

    def test_cgroup_cpu_quota_is_reported_in_cores(self):
        self.assertEqual(parse_cgroup({'cpu.max': '250000 100000'})['cpu_limit_cores'], 2.5)

    def test_capacity_rejections_are_read_from_prometheus_samples(self):
        metrics = (
            '# TYPE bio_agent_redis_worker_capacity_rejections_total counter\n'
            'bio_agent_redis_worker_capacity_rejections_total{tool="a"} 2\n'
            'bio_agent_redis_worker_capacity_rejections_total{tool="b"} 3\n'
        )
        self.assertEqual(parse_capacity_rejections(metrics), 5)

    def test_tool_phase_events_are_selected_from_container_logs(self):
        logs = (
            '2026-09-25T01:00:00Z {"event":"plugin.sandbox.started"}\n'
            '2026-09-25T01:00:01Z {"event":"tool.execution.completed",'
            '"tool":"omics_inspect_toolchain","status":"success",'
            '"duration_seconds":1.5,"phase_seconds":{"tool_run":0.2},'
            '"boundary_seconds":{"module_import":0.4}}\n'
        )
        self.assertEqual(parse_tool_phase_events(logs, 'omics_inspect_toolchain'), [{
            'duration': 1.5,
            'tool_run': 0.2,
            'module_import': 0.4,
        }])

    def test_peak_light_parallelism_requires_overlapping_heavy_job(self):
        start = datetime(2026, 9, 24, tzinfo=timezone.utc)
        heavy = [(start, start + timedelta(seconds=10))]
        light = [
            (start + timedelta(seconds=index), start + timedelta(seconds=8))
            for index in range(4)
        ]
        self.assertEqual(peak_light_running_while_heavy(heavy, light), 4)
        self.assertEqual(peak_light_running_while_heavy(
            heavy, [(start + timedelta(seconds=10), start + timedelta(seconds=11))]
        ), 0)


if __name__ == '__main__':
    unittest.main()
