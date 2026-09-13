import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify_s3_durability.py"
SPEC = importlib.util.spec_from_file_location("verify_s3_durability", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class FakeS3:
    def __init__(self):
        self.versioning = {"Status": "Enabled"}
        self.encryption = {
            "ServerSideEncryptionConfiguration": {
                "Rules": [
                    {
                        "ApplyServerSideEncryptionByDefault": {
                            "SSEAlgorithm": "aws:kms"
                        }
                    }
                ]
            }
        }
        self.lock = {
            "ObjectLockConfiguration": {
                "ObjectLockEnabled": "Enabled",
                "Rule": {
                    "DefaultRetention": {"Mode": "COMPLIANCE", "Days": 30}
                },
            }
        }

    def head_bucket(self, **_request):
        return {}

    def get_bucket_location(self, **_request):
        return {"LocationConstraint": None}

    def get_bucket_versioning(self, **_request):
        return self.versioning

    def get_bucket_encryption(self, **_request):
        return self.encryption

    def get_object_lock_configuration(self, **_request):
        return self.lock

    def get_public_access_block(self, **_request):
        return {
            "PublicAccessBlockConfiguration": {
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            }
        }


class FakeIdentity:
    def __init__(self, arn="arn:aws:sts::123456789012:assumed-role/runtime/session"):
        self.arn = arn

    def get_caller_identity(self):
        return {"Arn": self.arn}


class S3DurabilityTests(unittest.TestCase):
    def verify(self, s3=None, identity=None, **overrides):
        values = {
            "bucket": "bioagent-production",
            "expected_region": "us-east-1",
            "endpoint": "https://s3.us-east-1.amazonaws.com",
            "backup_role_arn": "arn:aws:iam::123456789012:role/backup",
            "expected_owner": "123456789012",
        }
        values.update(overrides)
        return MODULE.verify_s3_durability(
            s3 or FakeS3(),
            identity or FakeIdentity(),
            **values,
        )

    def test_accepts_encrypted_versioned_locked_private_bucket(self):
        result = self.verify()
        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["object_lock"], "COMPLIANCE")

    def test_rejects_suspended_versioning(self):
        s3 = FakeS3()
        s3.versioning = {"Status": "Suspended"}
        with self.assertRaisesRegex(MODULE.StoragePolicyError, "versioning"):
            self.verify(s3=s3)

    def test_rejects_object_lock_without_default_retention(self):
        s3 = FakeS3()
        s3.lock["ObjectLockConfiguration"].pop("Rule")
        with self.assertRaisesRegex(MODULE.StoragePolicyError, "retention mode"):
            self.verify(s3=s3)

    def test_rejects_same_runtime_and_backup_role(self):
        identity = FakeIdentity(
            "arn:aws:sts::123456789012:assumed-role/backup/deployment"
        )
        with self.assertRaisesRegex(MODULE.StoragePolicyError, "must be different"):
            self.verify(identity=identity)

    def test_rejects_insecure_endpoint(self):
        with self.assertRaisesRegex(MODULE.StoragePolicyError, "HTTPS"):
            self.verify(endpoint="http://s3.example")


if __name__ == "__main__":
    unittest.main()
