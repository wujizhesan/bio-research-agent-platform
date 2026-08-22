import unittest

from tools.load_test import percentile, summarize


class LoadTestHelpersTests(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertEqual(percentile([1, 2, 3, 4], 50), 2.5)

    def test_summarize_returns_latency_baseline(self):
        result = summarize([0.01, 0.02, 0.04])
        self.assertEqual(result['count'], 3)
        self.assertEqual(result['min_ms'], 10.0)
        self.assertEqual(result['max_ms'], 40.0)
        self.assertEqual(result['p95_ms'], 38.0)


if __name__ == '__main__':
    unittest.main()
