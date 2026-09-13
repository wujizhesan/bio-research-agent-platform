import importlib.util
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERIFY_SCRIPT = ROOT / "scripts" / "verify_recovery_point.py"
PUBLISH_SCRIPT = ROOT / "scripts" / "publish_recovery_evidence.py"
sys.path.insert(0, str(ROOT / "scripts"))

VERIFY_SPEC = importlib.util.spec_from_file_location("verify_recovery_point", VERIFY_SCRIPT)
VERIFY = importlib.util.module_from_spec(VERIFY_SPEC)
VERIFY_SPEC.loader.exec_module(VERIFY)

PUBLISH_SPEC = importlib.util.spec_from_file_location(
    "publish_recovery_evidence",
    PUBLISH_SCRIPT,
)
PUBLISH = importlib.util.module_from_spec(PUBLISH_SPEC)
PUBLISH_SPEC.loader.exec_module(PUBLISH)


class RecoveryEvidencePublisherTests(unittest.TestCase):
    def inputs(self, now):
        backup = {
            "schema_version": 1,
            "status": "available",
            "scenario": "production",
            "backup_id": "backup-current",
            "recovery_point_at": (now - timedelta(minutes=2)).isoformat(),
            "database_sha256": "a" * 64,
            "object_manifest_sha256": "b" * 64,
            "object_count": 4,
        }
        restore = {
            "schema_version": 1,
            "status": "passed",
            "scenario": "production",
            "verified_at": (now - timedelta(days=2)).isoformat(),
            "verified_backup_id": "backup-drill",
            "verified_database_sha256": "c" * 64,
            "verified_object_manifest_sha256": "d" * 64,
            "restored_revision": "0006_job_run_context",
        }
        return backup, restore

    def test_builds_signed_independent_backup_and_restore_evidence(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        backup, restore = self.inputs(now)
        key = b"recovery-evidence-test-key-material-0001"
        evidence = PUBLISH.build_evidence(backup, restore, key)
        result = VERIFY.verify_evidence(
            evidence,
            now=now,
            max_recovery_point_age_seconds=900,
            max_verification_age_seconds=7 * 24 * 3600,
            hmac_key=key,
        )
        self.assertEqual(evidence["schema_version"], 2)
        self.assertEqual(evidence["backup_id"], "backup-current")
        self.assertEqual(evidence["verified_backup_id"], "backup-drill")
        self.assertEqual(result["status"], "passed")

    def test_tampering_is_rejected(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        backup, restore = self.inputs(now)
        key = b"recovery-evidence-test-key-material-0001"
        evidence = PUBLISH.build_evidence(backup, restore, key)
        evidence["object_count"] = 99
        with self.assertRaisesRegex(VERIFY.EvidenceError, "signature mismatch"):
            VERIFY.verify_evidence(
                evidence,
                now=now,
                max_recovery_point_age_seconds=900,
                max_verification_age_seconds=7 * 24 * 3600,
                hmac_key=key,
            )

    def test_atomic_write_replaces_complete_document(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "latest-production.json"
            target.write_text("old", encoding="utf-8")
            payload = {"schema_version": 2, "status": "passed"}
            PUBLISH.atomic_write(target, payload)
            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
            self.assertEqual(list(target.parent.glob(f".{target.name}.*")), [])

    def test_rejects_mismatched_scenarios(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        backup, restore = self.inputs(now)
        restore["scenario"] = "staging-production-copy"
        with self.assertRaisesRegex(PUBLISH.EvidenceError, "do not match"):
            PUBLISH.build_evidence(
                backup,
                restore,
                b"recovery-evidence-test-key-material-0001",
            )


if __name__ == "__main__":
    unittest.main()
