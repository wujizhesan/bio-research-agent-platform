import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "deploy_transaction.sh"
BUNDLE_FILES = (
    "deploy_transaction.sh",
    "docker-compose.yml",
    "docker-compose.secure.yml",
    "docker-compose.deploy.yml",
    "verify_public_deployment.py",
    "monitoring/prometheus.yml",
    "monitoring/alertmanager.yml",
    "monitoring/storage-deletion-alerts.yml",
)


def bundle_digest(bundle, *, version=3):
    files = BUNDLE_FILES if version == 3 else BUNDLE_FILES[1:]
    manifest = "".join(
        f"{hashlib.sha256((bundle / name).read_bytes()).hexdigest()}  {name}\n"
        for name in files
    )
    return hashlib.sha256(manifest.encode()).hexdigest()


@unittest.skipIf(os.name == "nt", "requires a POSIX shell")
class DeployTransactionTests(unittest.TestCase):
    def prepare(self, directory, *, current=False, bootstrap_current=True):
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
        (deploy / "deploy_transaction.sh").write_text("legacy: true\n", encoding="utf-8")
        monitoring = deploy / "monitoring"
        monitoring.mkdir()
        for name in (
            "prometheus.yml", "alertmanager.yml", "storage-deletion-alerts.yml",
        ):
            (monitoring / name).write_text("legacy: true\n", encoding="utf-8")
        candidate = deploy / "release-bundles" / "candidate"
        (candidate / "monitoring").mkdir(parents=True)
        for name in (
            "deploy_transaction.sh",
            "docker-compose.yml", "docker-compose.secure.yml",
            "docker-compose.deploy.yml", "verify_public_deployment.py",
        ):
            (candidate / name).write_text("candidate: true\n", encoding="utf-8")
        for name in (
            "prometheus.yml", "alertmanager.yml", "storage-deletion-alerts.yml",
        ):
            (candidate / "monitoring" / name).write_text(
                "candidate: true\n", encoding="utf-8",
            )
        (deploy / "release-images.next.env").write_text(
            "BACKEND_IMAGE=next-backend\nFRONTEND_IMAGE=next-frontend\n"
            "RELEASE_TAG=v0.2.0-rc.1\nGIT_SHA=" + "a" * 40 + "\n"
            "RELEASE_BUNDLE_DIR=release-bundles/candidate\n"
            f"RELEASE_BUNDLE_SHA256={bundle_digest(candidate)}\n",
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
if [[ "$*" == *" config --hash "* ]]; then
  printf '%s %064d\\n' "${@: -1}" 0
  exit 0
fi
if [[ "$*" == *" ps -q "* ]]; then
  if [[ "${BOOTSTRAP_MISSING_CONTAINER:-0}" != "1" ]]; then
    printf 'container-%s\\n' "${@: -1}"
  fi
  exit 0
fi
if [[ "$1" == inspect ]]; then
  if [[ "$3" == *Config.Image* ]]; then
    if [[ "${BOOTSTRAP_IMAGE_MISMATCH:-0}" == "1" ]]; then
      printf 'stale-image\\n'
    elif [[ "$4" == container-web ]]; then
      printf 'current-frontend\\n'
    else
      printf 'current-backend\\n'
    fi
  elif [[ "$3" == *config-hash* ]]; then
    if [[ "${BOOTSTRAP_HASH_MISMATCH:-0}" == "1" ]]; then
      printf '%064d\\n' 1
    else
      printf '%064d\\n' 0
    fi
  elif [[ "$3" == *State.StartedAt* ]]; then
    if [[ "${BOOTSTRAP_STALE_SECRET:-0}" == "1" ]]; then
      printf '2000-01-01T00:00:00Z\\n'
    else
      printf '2100-01-01T00:00:00Z\\n'
    fi
  fi
  exit 0
fi
if [[ -n "${BACKEND_IMAGE:-}" ]]; then
  printf 'backend-image-override %s\\n' "$BACKEND_IMAGE" >> "$COMMAND_LOG"
fi
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
        if current and bootstrap_current:
            result = self.run_deploy(deploy, environment, bootstrap=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            log.unlink(missing_ok=True)
        return deploy, log, environment

    def run_deploy(self, deploy, environment, *, bootstrap=False):
        command = ["bash", str(SCRIPT), str(deploy), "https://platform.example/health"]
        if bootstrap:
            command.append("--bootstrap-config")
        return subprocess.run(
            command,
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
            self.assertIn(
                "RELEASE_CONFIG_VERSION=3",
                (deploy / "release-images.env").read_text(),
            )
            storage = next(
                i for i, line in enumerate(commands)
                if "run --rm --no-deps storage-check" in line
            )
            auth_config = next(
                i for i, line in enumerate(commands)
                if "run --rm --no-deps api python -c" in line
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
            staging = next(
                i for i, line in enumerate(commands)
                if "configure_tenant_context.py --stage" in line
            )
            self.assertLess(auth_config, storage)
            self.assertLess(storage, recovery)
            self.assertLess(recovery, pitr)
            self.assertLess(pitr, migration)
            self.assertLess(recovery, migration)
            self.assertLess(migration, rollout)
            self.assertLess(migration, staging)
            self.assertLess(staging, rollout)
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
            self.assertIn("configure_tenant_context.py --stage", commands)
            self.assertNotIn("configure_tenant_context.py --disable", commands)
            self.assertIn("release-images.env up -d", commands)
            rollback_start = commands.index("release-images.env up -d")
            rollback_enforcement = commands.index(
                "release-images.env run --rm migration python scripts/configure_tenant_context.py --enable"
            )
            self.assertLess(rollback_enforcement, rollback_start)
            self.assertIn("backend-image-override next-backend", commands)
            self.assertIn("rollback completed", result.stderr)

    def test_existing_release_requires_explicit_config_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(
                directory, current=True, bootstrap_current=False,
            )
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("lacks a bundle snapshot", result.stderr)
            self.assertFalse(log.exists())
            bootstrap = self.run_deploy(deploy, environment, bootstrap=True)
            self.assertEqual(bootstrap.returncode, 0, bootstrap.stderr)
            self.assertIn(
                "--env-file .env.production --env-file release-images.env config --hash api",
                log.read_text(encoding="utf-8"),
            )
            self.assertIn(
                "RELEASE_CONFIG_VERSION=3",
                (deploy / "release-images.env").read_text(),
            )
            self.assertNotIn(
                "RELEASE_CONFIG_VERSION=3",
                (deploy / "release-images.prebootstrap.env").read_text(),
            )

    def test_upgrades_version_one_snapshot_before_bundle_staging(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, _, environment = self.prepare(
                directory, current=True, bootstrap_current=False,
            )
            config = (deploy / ".env.production").read_text(encoding="utf-8")
            images = (deploy / "release-images.env").read_text(encoding="utf-8")
            root = deploy.parent
            old_state = (
                config + "\n" + images + "RELEASE_CONFIG_VERSION=1\n"
                + "CADD_JWT_SECRET_SHA256="
                + hashlib.sha256((root / "cadd_jwt_secret").read_bytes()).hexdigest()
                + "\nRLS_CONTEXT_SIGNING_KEY_SHA256="
                + hashlib.sha256((root / "rls_context_signing_key").read_bytes()).hexdigest()
                + "\n"
            )
            (deploy / "release-images.env").write_text(old_state, encoding="utf-8")
            result = self.run_deploy(deploy, environment, bootstrap=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(
                "RELEASE_CONFIG_VERSION=3",
                (deploy / "release-images.env").read_text(encoding="utf-8"),
            )

    def test_bootstrap_rejects_live_config_or_image_mismatch(self):
        for mismatch in ("BOOTSTRAP_HASH_MISMATCH", "BOOTSTRAP_IMAGE_MISMATCH"):
            with self.subTest(mismatch=mismatch):
                with tempfile.TemporaryDirectory() as directory:
                    deploy, _, environment = self.prepare(
                        directory, current=True, bootstrap_current=False,
                    )
                    environment[mismatch] = "1"
                    result = self.run_deploy(deploy, environment, bootstrap=True)
                    self.assertEqual(result.returncode, 1)
                    self.assertIn("differs from current release", result.stderr)
                    self.assertNotIn(
                        "RELEASE_CONFIG_VERSION=3",
                        (deploy / "release-images.env").read_text(),
                    )

    def test_bootstrap_rejects_missing_live_container(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, _, environment = self.prepare(
                directory, current=True, bootstrap_current=False,
            )
            environment["BOOTSTRAP_MISSING_CONTAINER"] = "1"
            result = self.run_deploy(deploy, environment, bootstrap=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("has no running container", result.stderr)

    def test_bootstrap_rejects_secret_changed_since_api_start(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, _, environment = self.prepare(
                directory, current=True, bootstrap_current=False,
            )
            environment["BOOTSTRAP_STALE_SECRET"] = "1"
            result = self.run_deploy(deploy, environment, bootstrap=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("file changed after live API started", result.stderr)

    def test_bootstrap_rejects_unhealthy_current_release(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, _, environment = self.prepare(
                directory, current=True, bootstrap_current=False,
            )
            environment["CURL_EXIT"] = "1"
            result = self.run_deploy(deploy, environment, bootstrap=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("current release health check failed", result.stderr)

    def test_rejects_in_place_secret_replacement_before_rollout(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory, current=True)
            old_secret = deploy.parent / "rls_context_signing_key"
            old_secret.write_text("changed-rls-secret-" * 3, encoding="utf-8")
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("file changed after release snapshot", result.stderr)
            self.assertFalse(log.exists())

    def test_rejects_in_place_database_credential_replacement(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory, current=True)
            old_secret = deploy.parent / "api_database_url"
            old_secret.write_text(
                "postgresql+asyncpg://api:new@postgres.example:5432/bioagent",
                encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("API_DATABASE_URL file changed after release snapshot", result.stderr)
            self.assertFalse(log.exists())

    def test_rejects_modified_current_release_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory, current=True)
            state = (deploy / "release-images.env").read_text(encoding="utf-8")
            bundle = next(
                line.split("=", 1)[1] for line in state.splitlines()
                if line.startswith("RELEASE_BUNDLE_DIR=")
            )
            (deploy / bundle / "docker-compose.yml").write_text(
                "tampered: true\n", encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("release bundle changed after snapshot", result.stderr)
            self.assertFalse(log.exists())

    def test_rejects_modified_candidate_script_before_running_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory)
            (deploy / "release-bundles" / "candidate" / "deploy_transaction.sh").write_text(
                "tampered: true\n", encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("candidate release bundle differs from verified source", result.stderr)
            self.assertFalse(log.exists())

    def test_rejects_modified_previous_script_before_running_compose(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory, current=True)
            state = (deploy / "release-images.env").read_text(encoding="utf-8")
            bundle = next(
                line.split("=", 1)[1] for line in state.splitlines()
                if line.startswith("RELEASE_BUNDLE_DIR=")
            )
            (deploy / bundle / "deploy_transaction.sh").write_text(
                "tampered: true\n", encoding="utf-8",
            )
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("release bundle changed after snapshot", result.stderr)
            self.assertFalse(log.exists())

    def test_version_two_snapshot_remains_verifiable_during_rollback(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory, current=True)
            state_path = deploy / "release-images.env"
            state = state_path.read_text(encoding="utf-8")
            bundle = next(
                line.split("=", 1)[1] for line in state.splitlines()
                if line.startswith("RELEASE_BUNDLE_DIR=")
            )
            version_three_digest = bundle_digest(deploy / bundle)
            version_two_digest = bundle_digest(deploy / bundle, version=2)
            (deploy / bundle / "deploy_transaction.sh").unlink()
            state_path.write_text(
                state.replace("RELEASE_CONFIG_VERSION=3", "RELEASE_CONFIG_VERSION=2")
                .replace(version_three_digest, version_two_digest),
                encoding="utf-8",
            )
            environment["NEXT_UP_FAIL"] = "1"
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("rollback completed", result.stderr)
            self.assertIn("release-images.env up -d", log.read_text(encoding="utf-8"))

    def test_rollback_uses_immutable_previous_bundle(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory, current=True)
            state = (deploy / "release-images.env").read_text(encoding="utf-8")
            bundle = next(
                line.split("=", 1)[1] for line in state.splitlines()
                if line.startswith("RELEASE_BUNDLE_DIR=")
            )
            (deploy / "docker-compose.yml").write_text(
                "overwritten: true\n", encoding="utf-8",
            )
            (deploy / "monitoring" / "prometheus.yml").write_text(
                "overwritten: true\n", encoding="utf-8",
            )
            environment["NEXT_UP_FAIL"] = "1"
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn("rollback completed", result.stderr)
            self.assertEqual(
                (deploy / "monitoring" / "prometheus.yml").read_text(encoding="utf-8"),
                "legacy: true\n",
            )
            rollback_command = next(
                line for line in log.read_text(encoding="utf-8").splitlines()
                if "release-images.env up -d" in line
            )
            self.assertIn(f"-f {bundle}/docker-compose.yml", rollback_command)
            self.assertNotIn("release-bundles/candidate", rollback_command)

    def test_rollback_uses_previous_secret_path(self):
        with tempfile.TemporaryDirectory() as directory:
            deploy, log, environment = self.prepare(directory, current=True)
            old_path = deploy.parent / "rls_context_signing_key"
            new_path = deploy.parent / "rls_context_signing_key_new"
            new_path.write_text("new-rls-secret-" * 3, encoding="utf-8")
            new_path.chmod(0o600)
            candidate_config = deploy / ".env.production"
            candidate_config.write_text(
                candidate_config.read_text(encoding="utf-8").replace(
                    f"RLS_CONTEXT_SIGNING_KEY_FILE={old_path}",
                    f"RLS_CONTEXT_SIGNING_KEY_FILE={new_path}",
                ).replace(
                    "APP_ENV=production",
                    "APP_ENV=production\nRLS_CONTEXT_KEY_ID=rotated",
                ),
                encoding="utf-8",
            )
            environment["NEXT_UP_FAIL"] = "1"
            result = self.run_deploy(deploy, environment)
            self.assertEqual(result.returncode, 1)
            self.assertIn(f"RLS_CONTEXT_SIGNING_KEY_FILE={old_path}",
                          (deploy / "release-images.env").read_text())
            self.assertIn(f"RLS_CONTEXT_SIGNING_KEY_FILE={new_path}",
                          (deploy / "release-images.next.env").read_text())
            commands = log.read_text(encoding="utf-8")
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
