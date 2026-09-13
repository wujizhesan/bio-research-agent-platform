import importlib.util
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "create_pitr_checkpoint.py"
SPEC = importlib.util.spec_from_file_location("create_pitr_checkpoint", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeConnection:
    def __init__(self):
        self.archive_mode = "on"
        self.failed_count = 0
        self.target_wal = "00000001000000000000000A"

    async def fetchval(self, query, *_args):
        if "archive_mode" in query:
            return self.archive_mode
        if "archive_command" in query:
            return "archive-to-remote %p %f"
        if "archive_library" in query:
            return ""
        if "pg_is_in_recovery" in query:
            return False
        if "pg_create_restore_point" in query:
            return "0/A000000"
        if "pg_walfile_name" in query:
            return self.target_wal
        if "pg_switch_wal" in query:
            return "0/B000000"
        raise AssertionError(query)

    async def fetchrow(self, query):
        if "pg_control_system" in query:
            return {"system_identifier": "7612345678901234567"}
        if "last_archived_wal" not in query:
            return {"archived_count": 10, "failed_count": 0}
        return {
            "archived_count": 11,
            "last_archived_wal": self.target_wal,
            "last_archived_time": datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
            "failed_count": self.failed_count,
            "last_failed_wal": self.target_wal if self.failed_count else None,
        }


class PostgresPitrTests(unittest.IsolatedAsyncioTestCase):
    async def test_creates_restore_point_and_waits_for_archived_wal(self):
        connection = FakeConnection()
        result = await MODULE.create_pitr_checkpoint(
            connection,
            release_tag="v0.2.0-rc.1",
            now=datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc),
            monotonic=lambda: 0,
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["wal_segment"], connection.target_wal)
        self.assertIn("v0.2.0-rc.1", result["restore_point"])

    async def test_rejects_disabled_wal_archiving(self):
        connection = FakeConnection()
        connection.archive_mode = "off"
        with self.assertRaisesRegex(MODULE.PitrError, "archive_mode"):
            await MODULE.create_pitr_checkpoint(
                connection,
                release_tag="release",
                monotonic=lambda: 0,
            )

    async def test_rejects_archiver_failure(self):
        connection = FakeConnection()
        connection.failed_count = 1
        with self.assertRaisesRegex(MODULE.PitrError, "archiving failed"):
            await MODULE.create_pitr_checkpoint(
                connection,
                release_tag="release",
                monotonic=lambda: 0,
            )


if __name__ == "__main__":
    unittest.main()
