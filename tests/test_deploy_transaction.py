import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_transaction.sh"


@unittest.skipIf(os.name == "nt", "requires a POSIX shell")
class DeployTransactionTests(unittest.TestCase):
    def prepare(self, directory, *, current=False):
        root = Path(directory)
        deploy = root / "deploy"
        binaries = root / "bin"
        deploy.mkdir()
        binaries.mkdir()
        (deploy / ".env.production").write_text(
            "\n".join(
                [
                    "POSTGRES_PASSWORD=production-password",
                    "APP_ENV=production",
                    f"CADD_JWT_SECRET={'a' * 32}",
                    f"PLUGIN_SANDBOX_TOKEN={'b' * 32}",
                    "DATABASE_URL=postgresql+asyncpg://app:secret@postgres.example:5432/bioagent",
                    "PITR_DATABASE_URL=postgresql://backup:secret@postgres.example:5432/bioagent",
                    "STORAGE_BACKEND=s3",
                    "S3_BUCKET=bioagent-production",
                    "S3_REGION=us-east-1",
                    "S3_ENDPOINT_URL=https://s3.us-east-1.amazonaws.com",
                    "S3_EXPECTED_BUCKET_OWNER=123456789012",
                    "S3_BACKUP_ROLE_ARN=arn:aws:iam::123456789012:role/bioagent-backup",
                    "PITR_CHECKPOINT_TIMEOUT_SECONDS=60",
                    "RECOVERY_EVIDENCE_PATH=/var/lib/bioagent/latest-production.json",
                    "RECOVERY_EVIDENCE_DIRECTORY=/var/lib/bioagent",
                    "RECOVERY_BACKUP_MANIFEST_PATH=/var/lib/bioagent/latest-backup.json",
                    "RECOVERY_RESTORE_REPORT_PATH=/var/lib/bioagent/latest-restore.json",
                    "RECOVERY_EVIDENCE_HMAC_KEY_PATH=/var/lib/bioagent/evidence.key",
                    "RECOVERY_POINT_MAX_AGE_SECONDS=900",
                    "RECOVERY_VERIFICATION_MAX_AGE_SECONDS=604800",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        for name in (
            "docker-compose.yml",
            "docker-compose.secure.yml",
            "docker-compose.deploy.yml",
        ):
            (deploy / name).write_text("services: {}\n", encoding="utf-8")
        (deploy / "release-images.next.env").write_text(
            "BACKEND_IMAGE=next-backend\nFRONTEND_IMAGE=next-frontend\n"
            "RELEASE_TAG=v0.2.0-rc.1\nGIT_SHA=" + "a" * 40 + "\n",
            encoding="utf-8",
        )
        if current:
            (deploy / "release-images.env").write_text(
                "BACKEND_IMAGE=current-backend\nFRONTEND_IMAGE=current-frontend\n"
                "RELEASE_TAG=v0.1.0-rc.1\nGIT_SHA=" + "b" * 40 + "\n",
                encoding="utf-8",
            )
        docker = binaries / "docker"
        docker.write_text(
            """#!/usr/bin/env bash
printf 'docker %s\\n' "$*" >> "$COMMAND_LOG"
if [[ "${NEXT_UP_FAIL:-0}" == "1" && "$*" == *"release-images.next.env"* && "$*" == *" up -d "* ]]; then
  exit 1
fi
""",
            encoding="utf-8",
        )
        curl = binaries / "curl"
        curl.write_text(
            """#!/usr/bin/env bash
printf 'curl %s\\n' "$*" >> "$COMMAND_LOG"
exit "${CURL_EXIT:-0}"
""",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        curl.chmod(0o755)
        log = root / "commands.log"
        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{binaries}{os.pathsep}{environment['PATH']}",
                "COMMAND_LOG": str(log),
                "DEPLOY_HEALTH_ATTEMPTS": "1",
                "DEPLOY_HEALTH_INTERVAL_SECONDS": "0",
            }
        )
        return deploy, log, environment

    def run_deploy(self, deploy, environment):
        return subprocess.run(
            ["bash", str(SCRIPT), str(deploy), "https://platform.example/health"],
            capture_output=True,
            text=True,
            env=environment,
            check=False,
        )

    def test_promotes_candidate_after_gates_and_health_check(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory)
            result = self.run_deploy(deploy, environment)
            commands = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse((deploy / "release-images.next.env").exists())
            self.assertIn("next-backend", (deploy / "release-images.env").read_text())
            storage = next(
                i for i, line in enumerate(commands)
                if "run --rm --no-deps storage-check" in line
            )
            recovery = next(
                i for i, line in enumerate(commands)
                if "run --rm --no-deps recovery-check" in line
            )
            pitr = next(
                i for i, line in enumerate(commands)
                if "run --rm --no-deps pitr-checkpoint" in line
            )
            migration = next(i for i, line in enumerate(commands) if "run --rm migration" in line)
            rollout = next(i for i, line in enumerate(commands) if " up -d " in line)
            health = next(i for i, line in enumerate(commands) if line.startswith("curl "))
            self.assertLess(storage, recovery)
            self.assertLess(recovery, pitr)
            self.assertLess(pitr, migration)
            self.assertLess(recovery, migration)
            self.assertLess(migration, rollout)
            self.assertLess(rollout, health)
            self.assertIn("X-Expected-Release: v0.2.0-rc.1", commands[health])
            self.assertIn("X-Expected-Commit: " + "a" * 40, commands[health])

    def test_restores_previous_digests_when_candidate_start_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory, current=True)
            environment["NEXT_UP_FAIL"] = "1"
            result = self.run_deploy(deploy, environment)
            commands = log.read_text(encoding="utf-8")
            current = (deploy / "release-images.env").read_text(encoding="utf-8")
            previous = (deploy / "release-images.previous.env").read_text(encoding="utf-8")
            self.assertEqual(result.returncode, 1)
            self.assertEqual(current, previous)
            self.assertTrue((deploy / "release-images.next.env").exists())
            self.assertIn("release-images.next.env up -d", commands)
            self.assertIn("release-images.env up -d", commands)
            self.assertIn("rollback completed", result.stderr)

    def test_first_failed_deployment_reports_missing_rollback_target(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, _, environment = self.prepare(directory)
            environment["NEXT_UP_FAIL"] = "1"
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no previous image set exists", result.stderr)
            self.assertFalse((deploy / "release-images.env").exists())

    def test_rejects_local_storage_before_running_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory)
            environment_path = deploy / ".env.production"
            environment_path.write_text(
                environment_path.read_text(encoding="utf-8").replace(
                    "STORAGE_BACKEND=s3",
                    "STORAGE_BACKEND=local",
                ),
                encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())

    def test_rejects_local_database_before_running_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory)
            environment_path = deploy / ".env.production"
            environment_path.write_text(
                environment_path.read_text(encoding="utf-8").replace(
                    "postgres.example:5432",
                    "db:5432",
                ),
                encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())

    def test_rejects_default_database_password_before_running_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory)
            environment_path = deploy / ".env.production"
            environment_path.write_text(
                environment_path.read_text(encoding="utf-8").replace(
                    "POSTGRES_PASSWORD=production-password",
                    "POSTGRES_PASSWORD=bioagent-dev-password",
                ),
                encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
