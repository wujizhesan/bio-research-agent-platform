import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from src.job_manager import JobManager


class JobManagerPersistenceTests(unittest.TestCase):
    def test_completed_job_survives_manager_restart(self):
        with tempfile.TemporaryDirectory(prefix='bio_job_store_') as raw:
            store = Path(raw) / 'jobs.sqlite3'
            manager = JobManager(max_workers=1, store_path=store)
            try:
                record = manager.submit('literature_search', {
                    'gene_ids': ['GeneA'],
                    'provider': 'local',
                    'evidence_csv': 'examples/rnaseq/evidence.csv',
                })
                for _ in range(100):
                    current = manager.get(record['job_id'])
                    if current['status'] in {'completed', 'failed'}:
                        break
                    time.sleep(0.01)
                self.assertEqual(current['status'], 'completed')
            finally:
                manager.shutdown()

            restored = JobManager(max_workers=1, store_path=store)
            try:
                recovered = restored.get(record['job_id'])
                self.assertEqual(recovered['status'], 'completed')
                self.assertEqual(recovered['result']['result']['n_matches'], 1)
            finally:
                restored.shutdown()

    def test_interrupted_jobs_are_marked_failed_on_restart(self):
        with tempfile.TemporaryDirectory(prefix='bio_job_store_') as raw:
            store = Path(raw) / 'jobs.sqlite3'
            manager = JobManager(max_workers=1, store_path=store)
            manager.shutdown()
            connection = sqlite3.connect(store)
            try:
                connection.execute(
                    'INSERT INTO jobs (job_id, tool, status, created_at, result_json) VALUES (?, ?, ?, ?, ?)',
                    ('interrupted', 'research_run_preset', 'running', '2026-01-01T00:00:00+00:00', json.dumps(None)),
                )
                connection.commit()
            finally:
                connection.close()
            restored = JobManager(max_workers=1, store_path=store)
            try:
                record = restored.get('interrupted')
                self.assertEqual(record['status'], 'failed')
                self.assertEqual(record['error'], 'job interrupted by process restart')
            finally:
                restored.shutdown()


    def test_queued_job_resumes_on_manager_restart(self):
        with tempfile.TemporaryDirectory(prefix='bio_job_resume_') as raw:
            store = Path(raw) / 'jobs.sqlite3'
            manager = JobManager(max_workers=1, store_path=store)
            manager.shutdown()
            connection = sqlite3.connect(store)
            try:
                connection.execute(
                    'INSERT INTO jobs (job_id, tool, status, created_at, arguments_json) VALUES (?, ?, ?, ?, ?)',
                    (
                        'queued-after-restart',
                        'literature_search',
                        'queued',
                        '2026-01-01T00:00:00+00:00',
                        json.dumps({
                            'gene_ids': ['GeneA'],
                            'provider': 'local',
                            'evidence_csv': 'examples/rnaseq/evidence.csv',
                        }),
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            restored = JobManager(max_workers=1, store_path=store)
            try:
                for _ in range(100):
                    record = restored.get('queued-after-restart')
                    if record['status'] in {'completed', 'failed'}:
                        break
                    time.sleep(0.01)
                self.assertEqual(record['status'], 'completed')
                self.assertEqual(record['result']['result']['n_matches'], 1)
            finally:
                restored.shutdown()
    def test_retry_creates_new_job_without_exposing_arguments(self):
        with tempfile.TemporaryDirectory(prefix='bio_job_retry_') as raw:
            store = Path(raw) / 'jobs.sqlite3'
            manager = JobManager(max_workers=1, store_path=store)
            try:
                original = manager.submit('literature_search', {
                    'gene_ids': ['GeneA'],
                    'provider': 'local',
                    'evidence_csv': 'examples/rnaseq/evidence.csv',
                })
                for _ in range(100):
                    current = manager.get(original['job_id'])
                    if current['status'] in {'completed', 'failed'}:
                        break
                    time.sleep(0.01)
                self.assertNotIn('_arguments', current)
                retried = manager.retry(original['job_id'])
                self.assertEqual(retried['retry_of'], original['job_id'])
                for _ in range(100):
                    current = manager.get(retried['job_id'])
                    if current['status'] in {'completed', 'failed'}:
                        break
                    time.sleep(0.01)
                self.assertEqual(current['status'], 'completed')
            finally:
                manager.shutdown()
if __name__ == '__main__':
    unittest.main()
