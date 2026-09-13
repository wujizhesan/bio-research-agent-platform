import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_recovery_point.py"
SPEC = importlib.util.spec_from_file_location("verify_recovery_point", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class RecoveryPointTests(unittest.TestCase):
    def evidence(self, now):
        return {
            "schema_version": 1,
            "status": "passed",
            "scenario": "production",
            "backup_id": "backup-20260913-001",
            "database_sha256": "a" * 64,
            "object_manifest_sha256": "b" * 64,
            "object_count": 3,
            "recovery_point_at": (now - timedelta(minutes=3)).isoformat(),
            "verified_at": (now - timedelta(minutes=1)).isoformat(),
            "restored_revision": "f00ba4",
        }

    def test_accepts_fresh_production_evidence(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        result = MODULE.verify_evidence(
            self.evidence(now),
            now=now,
            max_recovery_point_age_seconds=900,
            max_verification_age_seconds=3600,
        )
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["recovery_point_age_seconds"], 180)

    def test_backup_and_restore_freshness_are_independent(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        evidence = self.evidence(now)
        evidence["verified_at"] = (now - timedelta(days=3)).isoformat()
        result = MODULE.verify_evidence(
            evidence,
            now=now,
            max_recovery_point_age_seconds=900,
            max_verification_age_seconds=7 * 24 * 3600,
        )
        self.assertEqual(result["verification_age_seconds"], 3 * 24 * 3600)

    def test_rejects_synthetic_drill_evidence(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        evidence = self.evidence(now)
        evidence["scenario"] = "synthetic"
        with self.assertRaisesRegex(MODULE.EvidenceError, "production-grade"):
            MODULE.verify_evidence(
                evidence,
                now=now,
                max_recovery_point_age_seconds=900,
                max_verification_age_seconds=3600,
            )

    def test_rejects_stale_recovery_point(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        evidence = self.evidence(now)
        evidence["recovery_point_at"] = (now - timedelta(hours=1)).isoformat()
        with self.assertRaisesRegex(MODULE.EvidenceError, "recovery point is stale"):
            MODULE.verify_evidence(
                evidence,
                now=now,
                max_recovery_point_age_seconds=900,
                max_verification_age_seconds=3600,
            )

    def test_rejects_invalid_object_manifest_digest(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        evidence = self.evidence(now)
        evidence["object_manifest_sha256"] = "not-a-digest"
        with self.assertRaisesRegex(MODULE.EvidenceError, "object_manifest_sha256"):
            MODULE.verify_evidence(
                evidence,
                now=now,
                max_recovery_point_age_seconds=900,
                max_verification_age_seconds=3600,
            )

    def test_rejects_stale_restore_verification(self):
        now = datetime(2026, 9, 13, 12, 0, tzinfo=timezone.utc)
        evidence = self.evidence(now)
        evidence["verified_at"] = (now - timedelta(days=8)).isoformat()
        with self.assertRaisesRegex(MODULE.EvidenceError, "verification is stale"):
            MODULE.verify_evidence(
                evidence,
                now=now,
                max_recovery_point_age_seconds=900,
                max_verification_age_seconds=7 * 24 * 3600,
            )

    def test_cli_rejects_oversized_evidence_without_echoing_content(self):
        marker = "secret-material"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.json"
            path.write_text(json.dumps({"data": marker * 10000}), encoding="utf-8")
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--evidence",
                    str(path),
                    "--max-recovery-point-age-seconds",
                    "900",
                    "--max-verification-age-seconds",
                    "3600",
                ],
                capture_output=True,
                text=True,
                check=False,
            )
        self.assertEqual(result.returncode, 1)
        self.assertNotIn(marker, result.stderr)


if __name__ == "__main__":
    unittest.main()
