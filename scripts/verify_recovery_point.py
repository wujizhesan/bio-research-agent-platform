import argparse
import json
import re
from datetime import datetime, timezone
from pathlib import Path


MAX_EVIDENCE_BYTES = 64 * 1024
ALLOWED_SCENARIOS = {"production", "staging-production-copy"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(ValueError):
    pass


def parse_timestamp(value, field):
    if not isinstance(value, str):
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise EvidenceError(f"{field} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise EvidenceError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc)


def require_sha256(value, field):
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise EvidenceError(f"{field} must be a lowercase SHA-256 digest")


def require_nonempty_string(value, field):
    if not isinstance(value, str) or not value.strip():
        raise EvidenceError(f"{field} must be a non-empty string")


def verify_evidence(
    evidence,
    *,
    now=None,
    max_recovery_point_age_seconds,
    max_verification_age_seconds,
):
    if not isinstance(evidence, dict):
        raise EvidenceError("evidence must be a JSON object")
    if evidence.get("schema_version") != 1:
        raise EvidenceError("unsupported schema_version")
    if evidence.get("status") != "passed":
        raise EvidenceError("recovery verification did not pass")
    if evidence.get("scenario") not in ALLOWED_SCENARIOS:
        raise EvidenceError("scenario is not production-grade")

    require_nonempty_string(evidence.get("backup_id"), "backup_id")
    require_nonempty_string(evidence.get("restored_revision"), "restored_revision")
    require_sha256(evidence.get("database_sha256"), "database_sha256")
    require_sha256(evidence.get("object_manifest_sha256"), "object_manifest_sha256")

    object_count = evidence.get("object_count")
    if not isinstance(object_count, int) or isinstance(object_count, bool) or object_count < 1:
        raise EvidenceError("object_count must be a positive integer")

    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    recovery_point_at = parse_timestamp(evidence.get("recovery_point_at"), "recovery_point_at")
    verified_at = parse_timestamp(evidence.get("verified_at"), "verified_at")
    future_tolerance_seconds = 300
    if recovery_point_at > current or verified_at > current:
        latest = max(recovery_point_at, verified_at)
        if (latest - current).total_seconds() > future_tolerance_seconds:
            raise EvidenceError("evidence timestamp is in the future")
    recovery_age = max(0.0, (current - recovery_point_at).total_seconds())
    verification_age = max(0.0, (current - verified_at).total_seconds())
    if recovery_age > max_recovery_point_age_seconds:
        raise EvidenceError("recovery point is stale")
    if verification_age > max_verification_age_seconds:
        raise EvidenceError("recovery verification is stale")

    return {
        "status": "passed",
        "scenario": evidence["scenario"],
        "backup_id": evidence["backup_id"],
        "recovery_point_age_seconds": round(recovery_age, 3),
        "verification_age_seconds": round(verification_age, 3),
        "restored_revision": evidence["restored_revision"],
    }


def load_evidence(path):
    evidence_path = Path(path)
    try:
        size = evidence_path.stat().st_size
    except OSError as error:
        raise EvidenceError("recovery evidence is unavailable") from error
    if size > MAX_EVIDENCE_BYTES:
        raise EvidenceError("recovery evidence is too large")
    try:
        return json.loads(evidence_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise EvidenceError("recovery evidence is not valid JSON") from error


def positive_seconds(value):
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--max-recovery-point-age-seconds", required=True, type=positive_seconds)
    parser.add_argument("--max-verification-age-seconds", required=True, type=positive_seconds)
    args = parser.parse_args()
    try:
        result = verify_evidence(
            load_evidence(args.evidence),
            max_recovery_point_age_seconds=args.max_recovery_point_age_seconds,
            max_verification_age_seconds=args.max_verification_age_seconds,
        )
    except EvidenceError as error:
        parser.exit(1, f"recovery evidence rejected: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
