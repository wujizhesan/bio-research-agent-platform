import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace

from src.audit_log import AuditLogger
from src.database import Database


class AuditLoggerTests(unittest.TestCase):
    def test_database_is_durable_source_and_jsonl_is_compatibility_copy(self):
        with tempfile.TemporaryDirectory(prefix='bio_audit_') as raw:
            audit_path = Path(raw) / 'audit.jsonl'
            database = Database(
                f"sqlite+aiosqlite:///{(Path(raw) / 'audit.sqlite3').as_posix()}"
            )

            async def scenario():
                await database.init_schema()
                logger = AuditLogger(audit_path, database=database)
                principal = SimpleNamespace(
                    subject='alice',
                    roles=('researcher',),
                )
                event = await logger.record(
                    principal,
                    'file.downloaded',
                    'file',
                    'file-1',
                    {'authorization': 'Bearer secret-value', 'project_id': 'p1'},
                )
                stored = await database.list_audit_events(actor='alice')
                self.assertEqual(len(stored), 1)
                self.assertEqual(stored[0]['event_id'], event['event_id'])
                self.assertEqual(stored[0]['action'], 'file.downloaded')
                self.assertEqual(stored[0]['metadata']['authorization'], '[REDACTED]')
                self.assertEqual(len(stored[0]['event_hash']), 64)

            try:
                asyncio.run(scenario())
                compatibility = [
                    json.loads(line)
                    for line in audit_path.read_text(encoding='utf-8').splitlines()
                ]
                self.assertEqual(len(compatibility), 1)
                self.assertEqual(compatibility[0]['actor'], 'alice')
            finally:
                asyncio.run(database.close())


if __name__ == '__main__':
    unittest.main()
