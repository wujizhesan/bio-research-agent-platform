import unittest

from scripts.benchmark_worker_slots import summarize_rows


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
                        'light_overlap_count': 8,
                    },
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


if __name__ == '__main__':
    unittest.main()
