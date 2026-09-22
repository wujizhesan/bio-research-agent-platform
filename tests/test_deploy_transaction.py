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
        password_file = root / "smoke-password"
        password_file.write_text("deployment-secret\n", encoding="utf-8")
        password_file.chmod(0o600)
        secret_values = {
            "POSTGRES_PASSWORD": "production-password",
            "CADD_JWT_SECRET": "a" * 32,
            "RLS_CONTEXT_SIGNING_KEY": "c" * 32,
            "PLUGIN_SANDBOX_TOKEN": "b" * 32,
            "CADD_AUTH_USERS": '{"deployment-smoke":{"password_hash":"test","roles":["admin"]}}',
            "METRICS_SCRAPE_TOKEN": "m" * 40,
            "ALERTMANAGER_WEBHOOK_URL": "https://alerts.example/bioagent",
            "API_DATABASE_URL": "postgresql+asyncpg://api:secret@postgres.example:5432/bioagent",
            "DISPATCHER_DATABASE_URL": "postgresql+asyncpg://dispatcher:secret@postgres.example:5432/bioagent",
            "WORKER_DATABASE_URL": "postgresql+asyncpg://worker:secret@postgres.example:5432/bioagent",
            "MAINTENANCE_DATABASE_URL": "postgresql+asyncpg://maintenance:secret@postgres.example:5432/bioagent",
            "MIGRATION_DATABASE_URL": "postgresql+asyncpg://migration:secret@postgres.example:5432/bioagent",
            "PITR_DATABASE_URL": "postgresql://backup:secret@postgres.example:5432/bioagent",
            "API_REDIS_URL": "rediss://api:secret@redis.example:6379/0",
            "DISPATCHER_REDIS_URL": "rediss://dispatcher:secret@redis.example:6379/0",
            "WORKER_REDIS_URL": "rediss://worker:secret@redis.example:6379/0",
        }
        monitoring_secret_gid = os.getgid() or 1
        secret_lines = []
        for name, value in secret_values.items():
            path = root / name.lower()
            path.write_text(value + "\n", encoding="utf-8")
            path.chmod(0o600)
            if name in {"METRICS_SCRAPE_TOKEN", "ALERTMANAGER_WEBHOOK_URL"}:
                os.chown(path, -1, monitoring_secret_gid)
                path.chmod(0o640)
            secret_lines.append(f"{name}_FILE={path}")
        (deploy / ".env.production").write_text(
            "\n".join(
                [
                    "APP_ENV=production",
                    "PUBLIC_BASE_URL=https://platform.example",
                    "CORS_ORIGINS=https://platform.example",
                    "WEB_PUBLISHED_PORT=5173",
                    "DEPLOY_SMOKE_USERNAME=deployment-smoke",
                    f"DEPLOY_SMOKE_PASSWORD_FILE={password_file}",
                    "DEPLOY_SMOKE_JOB_TOOL=research_catalog",
                    "DEPLOY_SMOKE_JOB_TIMEOUT_SECONDS=60",
                    "TRUSTED_PROXY_CIDRS=172.16.0.0/12,127.0.0.1/32",
                    f"MONITORING_SECRET_GID={monitoring_secret_gid}",
                    *secret_lines,
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
        (deploy / "verify_public_deployment.py").write_text("", encoding="utf-8")
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
        python = binaries / "python3"
        python.write_text(
            """#!/usr/bin/env bash
printf 'python3 %s\\n' "$*" >> "$COMMAND_LOG"
exit "${VERIFY_EXIT:-0}"
""",
            encoding="utf-8",
        )
        docker.chmod(0o755)
        curl.chmod(0o755)
        python.chmod(0o755)
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
            verification = next(
                i for i, line in enumerate(commands) if line.startswith("python3 ")
            )
            enforcement = next(
                i for i, line in enumerate(commands)
                if "configure_tenant_context.py --enable" in line
            )
            self.assertLess(storage, recovery)
            self.assertLess(recovery, pitr)
            self.assertLess(pitr, migration)
            self.assertLess(recovery, migration)
            self.assertLess(migration, rollout)
            self.assertLess(rollout, health)
            self.assertLess(rollout, enforcement)
            self.assertLess(enforcement, health)
            self.assertLess(health, verification)
            self.assertIn('--base-url https://platform.example', commands[verification])
            self.assertIn('--username deployment-smoke', commands[verification])
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
            self.assertNotIn("configure_tenant_context.py --disable", commands)
            self.assertIn("release-images.env up -d", commands)
            rollback_start = commands.index("release-images.env up -d")
            rollback_enforcement = commands.index(
                "release-images.env run --rm migration python scripts/configure_tenant_context.py --enable"
            )
            self.assertLess(rollback_start, rollback_enforcement)
            self.assertIn("rollback completed", result.stderr)

    def test_first_failed_deployment_reports_missing_rollback_target(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, _, environment = self.prepare(directory)
            environment["NEXT_UP_FAIL"] = "1"
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no previous image set exists", result.stderr)
            self.assertFalse((deploy / "release-images.env").exists())

    def test_public_verification_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory, current=True)
            environment["VERIFY_EXIT"] = "1"
            result = self.run_deploy(deploy, environment)
            commands = log.read_text(encoding="utf-8")
            self.assertEqual(result.returncode, 1)
            self.assertIn("release-images.next.env up -d", commands)
            self.assertIn("release-images.env up -d", commands)

    def test_rejects_non_https_public_origin_before_running_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory)
            environment_path = deploy / ".env.production"
            environment_path.write_text(
                environment_path.read_text(encoding="utf-8").replace(
                    "PUBLIC_BASE_URL=https://platform.example",
                    "PUBLIC_BASE_URL=http://platform.example",
                ),
                encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())

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

    def test_rejects_monitoring_secret_group_mismatch(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory)
            environment_path = deploy / ".env.production"
            lines = environment_path.read_text(encoding="utf-8").splitlines()
            selected = next(
                int(line.split("=", 1)[1])
                for line in lines
                if line.startswith("MONITORING_SECRET_GID=")
            )
            environment_path.write_text(
                "\n".join(
                    f"MONITORING_SECRET_GID={selected + 1}"
                    if line.startswith("MONITORING_SECRET_GID=")
                    else line
                    for line in lines
                ) + "\n",
                encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())

    def test_rejects_local_database_before_running_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory)
            environment_path = deploy / ".env.production"
            api_secret_line = next(
                line for line in environment_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("API_DATABASE_URL_FILE=")
            )
            api_secret_path = Path(api_secret_line.split("=", 1)[1])
            api_secret_path.write_text(
                "postgresql+asyncpg://api:secret@db:5432/bioagent\n",
                encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())

    def test_rejects_default_database_password_before_running_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory)
            environment_path = deploy / ".env.production"
            password_line = next(
                line for line in environment_path.read_text(encoding="utf-8").splitlines()
                if line.startswith("POSTGRES_PASSWORD_FILE=")
            )
            Path(password_line.split("=", 1)[1]).write_text(
                "bioagent-dev-password\n",
                encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(log.exists())


if __name__ == "__main__":
    unittest.main()
