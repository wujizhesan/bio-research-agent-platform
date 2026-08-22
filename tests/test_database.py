import asyncio
import os
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from src.database import Database
from src.job_state_store import DatabaseStateWriter


class DatabaseStateTests(unittest.TestCase):
    def test_existing_local_schema_gets_execution_columns(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_upgrade_') as raw:
            path = Path(raw) / 'legacy.sqlite3'
            connection = sqlite3.connect(path)
            connection.execute(
                'CREATE TABLE job_records ('
                'job_id VARCHAR(64) PRIMARY KEY, tool VARCHAR(200) NOT NULL, '
                'status VARCHAR(32) NOT NULL, created_at VARCHAR(64) NOT NULL, '
                'started_at VARCHAR(64), finished_at VARCHAR(64), arguments JSON NOT NULL, '
                'result JSON, error TEXT, retry_of VARCHAR(64))'
            )
            connection.commit()
            connection.close()
            url = f"sqlite+aiosqlite:///{path.as_posix()}"
            record = {
                'job_id': 'legacy-job',
                'tool': 'research_catalog',
                'status': 'queued',
                'created_at': '2026-08-23T00:00:00+00:00',
                '_arguments': {},
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                database = Database(url)
                try:
                    asyncio.run(database.init_schema())
                    asyncio.run(database.upsert_job(record))
                    stored = asyncio.run(database.get_job('legacy-job'))
                finally:
                    asyncio.run(database.close())
            self.assertEqual(stored['status'], 'queued')
            self.assertEqual(stored['attempts'], 0)

    def test_worker_state_writer_persists_execution_state(self):
        with tempfile.TemporaryDirectory(prefix='bio_database_state_') as raw:
            url = f"sqlite+aiosqlite:///{(Path(raw) / 'state.sqlite3').as_posix()}"
            record = {
                'job_id': 'job-1',
                'tool': 'research_catalog',
                'status': 'running',
                'created_at': '2026-08-23T00:00:00+00:00',
                '_arguments': {},
                '_attempts': 2,
                '_cancel_requested': False,
                '_worker_id': 'worker-1',
                '_lease_until': 123.5,
            }
            with patch.dict(os.environ, {'AUTO_CREATE_SCHEMA': 'true'}, clear=False):
                writer = DatabaseStateWriter(url)
                writer.save(record)
                writer.close()

            database = Database(url)
            try:
                stored = asyncio.run(database.get_job('job-1'))
            finally:
                asyncio.run(database.close())
            self.assertEqual(stored['status'], 'running')
            self.assertEqual(stored['attempts'], 2)


if __name__ == '__main__':
    unittest.main()
