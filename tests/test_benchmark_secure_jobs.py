from datetime import datetime, timedelta, timezone
import unittest

from scripts.benchmark_secure_jobs import benchmark, job_timings


class SecureJobBenchmarkTests(unittest.TestCase):
    def test_benchmark_excludes_warmup_and_reports_server_timings(self):
        submitted = []
        base = datetime(2026, 9, 23, tzinfo=timezone.utc)
        queue_delays = [10.0, 0.2, 0.4]

        def requester(_base_url, path, method='GET', data=None, token=None,
                      form=False):
            if path == '/api/v1/auth/token':
                self.assertTrue(form)
                return {'access_token': 'benchmark-token'}
            self.assertEqual(token, 'benchmark-token')
            if path == '/api/v1/projects':
                self.assertEqual(method, 'POST')
                return {'project': {'project_id': 'project-1'}}
            if path == '/api/v1/jobs':
                self.assertEqual(data['project_id'], 'project-1')
                self.assertEqual(data['tool'], 'knowledge_search')
                self.assertEqual(data['arguments']['index_path'], '/index.json')
                job_id = f'job-{len(submitted)}'
                submitted.append(job_id)
                return {'job': {'job_id': job_id}}
            index = int(path.rsplit('-', 1)[-1])
            created = base + timedelta(minutes=index)
            started = created + timedelta(seconds=queue_delays[index])
            return {'job': {
                'job_id': f'job-{index}',
                'status': 'completed',
                'created_at': created.isoformat(),
                'started_at': started.isoformat(),
                'finished_at': (started + timedelta(seconds=1)).isoformat(),
            }}

        report = benchmark(
            'http://api.test', '/index.json', 'user', 'password',
            samples=2, requester=requester,
        )
        self.assertEqual(len(submitted), 3)
        self.assertEqual(report['sample_count'], 2)
        self.assertEqual([sample['job_id'] for sample in report['samples']],
                         ['job-1', 'job-2'])
        self.assertEqual(report['summary_seconds']['queue_seconds']['p50'], 0.3)
        self.assertEqual(report['summary_seconds']['execution_seconds']['p95'], 1.0)
        self.assertEqual(report['summary_seconds']['server_total_seconds']['max'], 1.4)

    def test_job_timings_rejects_invalid_timestamp_order(self):
        job = {
            'job_id': 'job-1',
            'created_at': '2026-09-23T00:00:01+00:00',
            'started_at': '2026-09-23T00:00:00+00:00',
            'finished_at': '2026-09-23T00:00:02+00:00',
        }
        with self.assertRaisesRegex(RuntimeError, 'out of order'):
            job_timings(job, 2.0)


if __name__ == '__main__':
    unittest.main()
