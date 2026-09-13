import argparse
import hashlib
import hmac
import json
import re
from datetime import datetime, timezone
from pathlib import Path


MAX_EVIDENCE_BYTES = 64 * 1024
ALLOWED_SCENARIOS = {"production", "staging-production-copy"}
SHA256 = re.compile(r"^[0-9a-f]{64}$")


class EvidenceError(ValueError):
    pass


def canonical_payload(evidence):
    payload = {key: value for key, value in evidence.items() if key != "signature"}
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def load_hmac_key(path):
    try:
        key = Path(path).read_bytes().strip()
    except OSError as error:
        raise EvidenceError("recovery evidence verification key is unavailable") from error
    if len(key) < 32:
        raise EvidenceError("recovery evidence verification key is too short")
    return key


def sign_evidence(evidence, key):
    return hmac.new(key, canonical_payload(evidence), hashlib.sha256).hexdigest()


def verify_evidence_signature(evidence, key):
    signature = evidence.get("signature")
    if not isinstance(signature, str) or not SHA256.fullmatch(signature):
        raise EvidenceError("recovery evidence signature is invalid")
    if not hmac.compare_digest(signature, sign_evidence(evidence, key)):
        raise EvidenceError("recovery evidence signature mismatch")


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
    hmac_key=None,
):
    if not isinstance(evidence, dict):
        raise EvidenceError("evidence must be a JSON object")
    if evidence.get("schema_version") not in {1, 2}:
        raise EvidenceError("unsupported schema_version")
    if evidence.get("status") != "passed":
        raise EvidenceError("recovery verification did not pass")
    if evidence.get("scenario") not in ALLOWED_SCENARIOS:
        raise EvidenceError("scenario is not production-grade")

    require_nonempty_string(evidence.get("backup_id"), "backup_id")
    require_nonempty_string(evidence.get("restored_revision"), "restored_revision")
    require_sha256(evidence.get("database_sha256"), "database_sha256")
    require_sha256(evidence.get("object_manifest_sha256"), "object_manifest_sha256")
    if evidence["schema_version"] == 2:
        require_nonempty_string(evidence.get("verified_backup_id"), "verified_backup_id")
        require_sha256(
            evidence.get("verified_database_sha256"),
            "verified_database_sha256",
        )
        require_sha256(
            evidence.get("verified_object_manifest_sha256"),
            "verified_object_manifest_sha256",
        )
        if hmac_key is None:
            raise EvidenceError("recovery evidence verification key is required")
    if hmac_key is not None:
        verify_evidence_signature(evidence, hmac_key)

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
    parser.add_argument("--hmac-key-file")
    args = parser.parse_args()
    try:
        result = verify_evidence(
            load_evidence(args.evidence),
            max_recovery_point_age_seconds=args.max_recovery_point_age_seconds,
            max_verification_age_seconds=args.max_verification_age_seconds,
            hmac_key=load_hmac_key(args.hmac_key_file) if args.hmac_key_file else None,
        )
    except EvidenceError as error:
        parser.exit(1, f"recovery evidence rejected: {error}\n")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
