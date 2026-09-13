import json
import os
import re
from urllib.parse import urlparse


ALLOWED_ENCRYPTION = {"AES256", "aws:kms", "aws:kms:dsse"}
ROLE_ARN = re.compile(
    r"^arn:(aws|aws-cn|aws-us-gov):iam::([0-9]{12}):role/([A-Za-z0-9+=,.@_/-]+)$"
)
ASSUMED_ROLE_ARN = re.compile(
    r"^arn:(aws|aws-cn|aws-us-gov):sts::([0-9]{12}):assumed-role/([^/]+)/[^/]+$"
)


class StoragePolicyError(ValueError):
    pass


def _required(value, name):
    normalized = str(value or "").strip()
    if not normalized:
        raise StoragePolicyError(f"{name} is required")
    return normalized


def validate_endpoint(endpoint):
    value = _required(endpoint, "S3_ENDPOINT_URL")
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname:
        raise StoragePolicyError("S3 endpoint must be an absolute HTTPS URL")
    if parsed.username or parsed.password:
        raise StoragePolicyError("S3 endpoint must not contain credentials")
    return value.rstrip("/")


def _normalized_region(value):
    if value in {None, ""}:
        return "us-east-1"
    if value == "EU":
        return "eu-west-1"
    return str(value)


def _role_key(arn):
    role = ROLE_ARN.fullmatch(str(arn or ""))
    if role:
        return role.group(2), role.group(3).split("/")[-1]
    assumed = ASSUMED_ROLE_ARN.fullmatch(str(arn or ""))
    if assumed:
        return assumed.group(2), assumed.group(3)
    return None


def verify_s3_durability(
    s3_client,
    identity_client,
    *,
    bucket,
    expected_region,
    endpoint,
    backup_role_arn,
    expected_owner=None,
):
    bucket = _required(bucket, "S3_BUCKET")
    expected_region = _required(expected_region, "S3_REGION")
    endpoint = validate_endpoint(endpoint)
    backup_role_arn = _required(backup_role_arn, "S3_BACKUP_ROLE_ARN")
    backup_role = _role_key(backup_role_arn)
    if backup_role is None or ":iam::" not in backup_role_arn:
        raise StoragePolicyError("S3_BACKUP_ROLE_ARN must be an IAM role ARN")

    request = {"Bucket": bucket}
    if expected_owner:
        request["ExpectedBucketOwner"] = str(expected_owner).strip()
    s3_client.head_bucket(**request)

    location = s3_client.get_bucket_location(**request)
    actual_region = _normalized_region(location.get("LocationConstraint"))
    if actual_region != expected_region:
        raise StoragePolicyError(
            f"S3 bucket region mismatch: expected {expected_region}, got {actual_region}"
        )

    versioning = s3_client.get_bucket_versioning(**request)
    if versioning.get("Status") != "Enabled":
        raise StoragePolicyError("S3 bucket versioning must be enabled")

    encryption = s3_client.get_bucket_encryption(**request)
    rules = encryption.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
    algorithms = {
        rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
        for rule in rules
    }
    algorithms.discard(None)
    if not algorithms or not algorithms.issubset(ALLOWED_ENCRYPTION):
        raise StoragePolicyError("S3 bucket default server-side encryption is invalid")

    lock = s3_client.get_object_lock_configuration(**request).get(
        "ObjectLockConfiguration", {}
    )
    retention = lock.get("Rule", {}).get("DefaultRetention", {})
    retention_period = retention.get("Days") or retention.get("Years")
    if lock.get("ObjectLockEnabled") != "Enabled":
        raise StoragePolicyError("S3 Object Lock must be enabled")
    if retention.get("Mode") not in {"GOVERNANCE", "COMPLIANCE"}:
        raise StoragePolicyError("S3 Object Lock default retention mode is required")
    if not isinstance(retention_period, int) or retention_period < 1:
        raise StoragePolicyError("S3 Object Lock default retention period is required")

    public_access = s3_client.get_public_access_block(**request).get(
        "PublicAccessBlockConfiguration", {}
    )
    if not all(
        public_access.get(name) is True
        for name in (
            "BlockPublicAcls",
            "IgnorePublicAcls",
            "BlockPublicPolicy",
            "RestrictPublicBuckets",
        )
    ):
        raise StoragePolicyError("S3 public access block must be fully enabled")

    runtime_arn = identity_client.get_caller_identity().get("Arn")
    runtime_role = _role_key(runtime_arn)
    if runtime_role is None:
        raise StoragePolicyError("runtime S3 identity must be an IAM role")
    if runtime_role == backup_role:
        raise StoragePolicyError("runtime and backup S3 identities must be different")

    return {
        "status": "passed",
        "bucket": bucket,
        "region": actual_region,
        "endpoint": endpoint,
        "encryption": sorted(algorithms),
        "versioning": "Enabled",
        "object_lock": retention["Mode"],
        "runtime_identity": runtime_arn,
        "backup_identity": backup_role_arn,
    }


def main():
    import boto3

    endpoint = os.environ.get("S3_ENDPOINT_URL")
    region = os.environ.get("S3_REGION")
    session = boto3.session.Session()
    s3_client = session.client("s3", endpoint_url=endpoint, region_name=region)
    identity_client = session.client("sts", region_name=region)
    try:
        result = verify_s3_durability(
            s3_client,
            identity_client,
            bucket=os.environ.get("S3_BUCKET"),
            expected_region=region,
            endpoint=endpoint,
            backup_role_arn=os.environ.get("S3_BACKUP_ROLE_ARN"),
            expected_owner=os.environ.get("S3_EXPECTED_BUCKET_OWNER"),
        )
    except Exception as error:
        raise SystemExit(
            f"S3 durability preflight rejected: {type(error).__name__}: {error}"
        ) from error
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
