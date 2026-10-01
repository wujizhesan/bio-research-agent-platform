from datetime import datetime, timedelta, timezone
import io
from threading import Lock
import unittest

from scripts.benchmark_workload_matrix import benchmark_matrix, wait_for_sse_job


class WorkloadMatrixTests(unittest.TestCase):
    def test_sse_reconnects_with_cursor_and_detects_terminal_job(self):
        urls = []
        streams = [
            b'id: r-1\nevent: job\ndata: {"job":{"status":"running"}}\n\n',
            b'id: r-2\nevent: job\ndata: {"job":{"status":"completed","job_id":"job-1"}}\n\n',
        ]

        def requester(_base, path, method='GET', token=None):
            self.assertEqual(path, '/api/v1/jobs/job-1/events/ticket')
            self.assertEqual(method, 'POST')
            self.assertEqual(token, 'token')
            return {'ticket': 'ticket-1'}

        def opener(url, _timeout):
            urls.append(url)
            return io.BytesIO(streams.pop(0))

        job, reconnects = wait_for_sse_job(
            'http://api.test',
            'job-1',
            'token',
            5,
            requester=requester,
            opener=opener,
            sleep_fn=lambda _seconds: None,
        )
        self.assertEqual(job['status'], 'completed')
        self.assertEqual(reconnects, 1)
        self.assertIn('last_event_id=r-1', urls[1])

    def test_matrix_covers_each_domain_and_mixed_concurrency(self):
        lock = Lock()
        submitted = []
        base = datetime(2026, 9, 24, tzinfo=timezone.utc)

        def requester(_base, path, method='GET', data=None, token=None,
                      form=False):
            if path == '/api/v1/auth/token':
                self.assertTrue(form)
                return {'access_token': 'token'}
            self.assertEqual(token, 'token')
            if path == '/api/v1/projects':
                return {'project': {'project_id': 'project-1'}}
            if path == '/api/v1/jobs':
                self.assertEqual(method, 'POST')
                with lock:
                    job_id = f'job-{len(submitted)}'
                    submitted.append((job_id, data['tool']))
                return {'job': {'job_id': job_id}}
            job_id = path.rsplit('/', 1)[-1]
            created = base
            started = created + timedelta(seconds=0.1)
            return {'job': {
                'job_id': job_id,
                'status': 'completed',
                'created_at': created.isoformat(),
                'started_at': started.isoformat(),
                'finished_at': (started + timedelta(seconds=0.2)).isoformat(),
                'queue_phase_seconds': {
                    'submission': 0.01,
                    'outbox_wait': 0.02,
                    'dispatch': 0.01,
                    'worker_wait': 0.06,
                },
            }}

        def observer(_base, job_id, _token, _timeout, requester=None):
            return {'job_id': job_id, 'status': 'completed'}, 0

        report = benchmark_matrix(
            'http://api.test',
            '/index.json',
            'user',
            'password',
            sample_count=3,
            concurrency_levels=(1, 2),
            requester=requester,
            observer=observer,
        )
        self.assertEqual(len(submitted), 27)
        self.assertEqual(len(report['scenarios']), 8)
        self.assertTrue(all(item['failed_count'] == 0 for item in report['scenarios']))
        mixed = [item for item in report['scenarios'] if item['name'] == 'mixed']
        self.assertEqual(
            {sample['tool'] for sample in mixed[0]['samples']},
            {'knowledge_search', 'literature_summarize', 'omics_inspect_toolchain'},
        )
        self.assertEqual(
            mixed[1]['summary_seconds']['server_total_seconds']['p95'],
            0.3,
        )


if __name__ == '__main__':
    unittest.main()
