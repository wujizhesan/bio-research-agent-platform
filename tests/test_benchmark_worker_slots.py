import unittest
from datetime import datetime, timedelta, timezone

from scripts.benchmark_worker_slots import (
    parse_capacity_rejections,
    summarize_rows,
    worker_env,
)
from scripts.verify_secure_mixed_resources import peak_light_running_while_heavy


class WorkerSlotBenchmarkTests(unittest.TestCase):
    def test_promotion_requires_light_gain_and_heavy_guardrail(self):
        rows = []
        for index in range(3):
            for name, light, heavy in (
                ('baseline', 3.0, 10.0),
                ('candidate', 2.0, 10.5),
            ):
                rows.append({
                    'round': index,
                    'configuration': name,
                    'result': {
                        'light_queue_seconds': [light] * 8,
                        'heavy_server_seconds': [heavy] * 4,
                        'light_queue_p95_seconds': light,
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

        for row in rows:
            if row['configuration'] == 'candidate':
                row['result']['heavy_server_seconds'] = [12.0] * 4
                row['result']['heavy_server_p95_seconds'] = 12.0
        self.assertFalse(summarize_rows(rows)['comparison']['promote_candidate'])

    def test_candidate_includes_the_extra_logical_cpu(self):
        self.assertEqual(worker_env('baseline')['JOB_TOTAL_CPU_CORES'], '4')
        self.assertEqual(worker_env('candidate')['JOB_TOTAL_CPU_CORES'], '5')

    def test_capacity_rejections_are_read_from_prometheus_samples(self):
        metrics = (
            '# TYPE bio_agent_redis_worker_capacity_rejections_total counter\n'
            'bio_agent_redis_worker_capacity_rejections_total{tool="a"} 2\n'
            'bio_agent_redis_worker_capacity_rejections_total{tool="b"} 3\n'
        )
        self.assertEqual(parse_capacity_rejections(metrics), 5)

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
