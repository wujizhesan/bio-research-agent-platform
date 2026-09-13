import argparse
import json
import os
import tempfile
from pathlib import Path

from verify_recovery_point import (
    ALLOWED_SCENARIOS,
    EvidenceError,
    load_evidence,
    load_hmac_key,
    parse_timestamp,
    require_nonempty_string,
    require_sha256,
    sign_evidence,
)


def validate_backup(manifest):
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise EvidenceError("unsupported backup manifest")
    if manifest.get("status") != "available":
        raise EvidenceError("backup is not available")
    if manifest.get("scenario") not in ALLOWED_SCENARIOS:
        raise EvidenceError("backup scenario is not production-grade")
    require_nonempty_string(manifest.get("backup_id"), "backup_id")
    parse_timestamp(manifest.get("recovery_point_at"), "recovery_point_at")
    require_sha256(manifest.get("database_sha256"), "database_sha256")
    require_sha256(manifest.get("object_manifest_sha256"), "object_manifest_sha256")
    count = manifest.get("object_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise EvidenceError("object_count must be a positive integer")


def validate_restore(report, scenario):
    if not isinstance(report, dict) or report.get("schema_version") != 1:
        raise EvidenceError("unsupported restore report")
    if report.get("status") != "passed":
        raise EvidenceError("restore verification did not pass")
    if report.get("scenario") != scenario:
        raise EvidenceError("backup and restore scenarios do not match")
    parse_timestamp(report.get("verified_at"), "verified_at")
    require_nonempty_string(report.get("verified_backup_id"), "verified_backup_id")
    require_nonempty_string(report.get("restored_revision"), "restored_revision")
    require_sha256(
        report.get("verified_database_sha256"),
        "verified_database_sha256",
    )
    require_sha256(
        report.get("verified_object_manifest_sha256"),
        "verified_object_manifest_sha256",
    )


def build_evidence(backup, restore, key):
    validate_backup(backup)
    validate_restore(restore, backup["scenario"])
    evidence = {
        "schema_version": 2,
        "status": "passed",
        "scenario": backup["scenario"],
        "backup_id": backup["backup_id"],
        "database_sha256": backup["database_sha256"],
        "object_manifest_sha256": backup["object_manifest_sha256"],
        "object_count": backup["object_count"],
        "recovery_point_at": backup["recovery_point_at"],
        "verified_at": restore["verified_at"],
        "verified_backup_id": restore["verified_backup_id"],
        "verified_database_sha256": restore["verified_database_sha256"],
        "verified_object_manifest_sha256": restore[
            "verified_object_manifest_sha256"
        ],
        "restored_revision": restore["restored_revision"],
    }
    evidence["signature"] = sign_evidence(evidence, key)
    return evidence


def atomic_write(path, payload):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        dir=target.parent,
        prefix=f".{target.name}.",
    )
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        os.chmod(target, 0o600)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        Path(temporary).unlink(missing_ok=True)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup-manifest", required=True)
    parser.add_argument("--restore-report", required=True)
    parser.add_argument("--hmac-key-file", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    try:
        evidence = build_evidence(
            load_evidence(args.backup_manifest),
            load_evidence(args.restore_report),
            load_hmac_key(args.hmac_key_file),
        )
        atomic_write(args.output, evidence)
    except EvidenceError as error:
        parser.exit(1, f"recovery evidence publication rejected: {error}\n")
    print(json.dumps({"status": "published", "backup_id": evidence["backup_id"]}))


if __name__ == "__main__":
    main()
