import json
import re
import tomllib
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SHA256_REFERENCE = re.compile(r"@sha256:[0-9a-f]{64}$")


class ComposeLoader(yaml.SafeLoader):
    pass


def _compose_sequence(loader, node):
    return loader.construct_sequence(node)


ComposeLoader.add_constructor('!reset', _compose_sequence)
ComposeLoader.add_constructor('!override', _compose_sequence)


class SupplyChainConfigurationTests(unittest.TestCase):
    def test_artifact_recovery_is_automatically_scheduled(self):
        compose = yaml.safe_load(
            (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
        )
        service = compose["services"]["artifact-maintenance"]
        self.assertEqual(service["restart"], "unless-stopped")
        self.assertIn("src.artifact_recovery", service["command"])
        self.assertIn("--loop-seconds", service["command"])
        self.assertIn("healthcheck", service)

    def test_github_actions_use_immutable_commits(self):
        references = []
        for workflow_path in (ROOT / ".github" / "workflows").glob("*.yml"):
            workflow = workflow_path.read_text(encoding="utf-8")
            references.extend(re.findall(r"^\s*uses:\s*([^\s#]+)", workflow, re.MULTILINE))
        remote_references = [reference for reference in references if not reference.startswith("./")]
        self.assertTrue(remote_references)
        for reference in remote_references:
            self.assertRegex(reference, r"@[0-9a-f]{40}$")

    def test_external_container_images_use_digests(self):
        files = [
            ROOT / "docker-compose.yml",
            ROOT / "docker-compose.secure.yml",
            ROOT / "docker-compose.recovery.yml",
            ROOT / ".github" / "workflows" / "ci.yml",
        ]
        references = []
        for path in files:
            content = path.read_text(encoding="utf-8")
            references.extend(
                re.findall(r"^\s*(?:image|pull_image):\s*([^\s#]+)", content, re.MULTILINE)
            )
        self.assertTrue(references)
        for reference in references:
            self.assertRegex(reference, SHA256_REFERENCE)

    def test_dockerfile_bases_use_digests(self):
        for relative_path in ("Dockerfile", "frontend/Dockerfile"):
            content = (ROOT / relative_path).read_text(encoding="utf-8")
            references = re.findall(r"^FROM\s+([^\s]+)", content, re.MULTILINE)
            self.assertTrue(references)
            for reference in references:
                self.assertRegex(reference, SHA256_REFERENCE)

    def test_backend_image_uses_unprivileged_runtime_user(self):
        dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        dockerignore = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        self.assertRegex(dockerfile, r"(?m)^USER bioagent$")
        self.assertIn("apt-get upgrade -y", dockerfile)
        self.assertIn("frontend", dockerignore)

    def test_dependabot_covers_all_dependency_sources(self):
        configuration = yaml.safe_load(
            (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        )
        ecosystems = {update["package-ecosystem"] for update in configuration["updates"]}
        self.assertEqual(set(ecosystems), {"uv", "npm", "docker", "github-actions"})

    def test_dependabot_version_updates_exclude_major_releases(self):
        configuration = yaml.safe_load(
            (ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8")
        )
        expected = [
            {
                "dependency-name": "*",
                "update-types": [
                    "version-update:semver-minor",
                    "version-update:semver-patch",
                ],
            }
        ]
        for update in configuration["updates"]:
            self.assertEqual(update["allow"], expected)

    def test_release_images_are_attested_and_verified(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        self.assertIn("packages: write", workflow)
        self.assertIn("attestations: write", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertEqual(workflow.count("push-to-registry: true"), 2)
        self.assertIn("sbom-path:", workflow)
        self.assertIn("gh attestation verify", workflow)
        self.assertIn("--predicate-type https://cyclonedx.org/bom", workflow)
        self.assertIn("Require successful CI for source commit", workflow)
        self.assertIn("release:\n    types: [published]", workflow)
        self.assertNotIn("push:\n    tags:", workflow)

    def test_production_deployment_requires_approval_and_verified_digests(self):
        workflow = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "deploy-production.yml").read_text(
                encoding="utf-8"
            )
        )
        verify = workflow["jobs"]["verify"]
        deploy = workflow["jobs"]["deploy"]
        self.assertNotIn("environment", verify)
        self.assertNotIn("secrets.", json.dumps(verify))
        self.assertEqual(deploy["environment"], {"name": "production"})
        deployment = json.dumps(deploy)
        deployment_script = (ROOT / "scripts" / "deploy_transaction.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("secrets.PRODUCTION_SSH_PRIVATE_KEY", deployment)
        self.assertIn("gh attestation verify", json.dumps(verify))
        self.assertIn("--signer-workflow", json.dumps(verify))
        self.assertIn("--source-ref", json.dumps(verify))
        self.assertIn("scripts/deploy_transaction.sh", deployment)
        self.assertIn("scripts/verify_public_deployment.py", deployment)
        self.assertIn("^https://", deployment)
        self.assertIn("--no-build", deployment_script)
        self.assertIn("verify_public_deployment", deployment_script)

        compose = yaml.load(
            (ROOT / "docker-compose.deploy.yml").read_text(encoding="utf-8"),
            Loader=ComposeLoader,
        )
        self.assertEqual(compose["services"]["api"]["image"], "${BACKEND_IMAGE:?required}")
        self.assertEqual(compose["services"]["worker"]["image"], "${BACKEND_IMAGE:?required}")
        self.assertEqual(
            compose["services"]["artifact-maintenance"]["image"],
            "${BACKEND_IMAGE:?required}",
        )
        self.assertEqual(
            compose["services"]["artifact-maintenance"]["environment"]["DATABASE_ROLE"],
            "maintenance",
        )
        self.assertEqual(
            compose["services"]["artifact-maintenance"]["environment"]["DATABASE_URL_FILE"],
            "/run/secrets/maintenance_database_url",
        )
        self.assertEqual(
            compose["services"]["migration"]["image"],
            "${BACKEND_IMAGE:?required}",
        )
        self.assertEqual(
            compose["services"]["recovery-check"]["image"],
            "${BACKEND_IMAGE:?required}",
        )
        self.assertEqual(
            compose["services"]["recovery-evidence-publisher"]["image"],
            "${BACKEND_IMAGE:?required}",
        )
        self.assertEqual(
            compose["services"]["storage-check"]["image"],
            "${BACKEND_IMAGE:?required}",
        )
        self.assertEqual(
            compose["services"]["pitr-checkpoint"]["image"],
            "${BACKEND_IMAGE:?required}",
        )
        self.assertEqual(
            compose["services"]["api"]["environment"]["DATABASE_URL"],
            "",
        )
        self.assertEqual(
            compose["services"]["api"]["environment"]["DATABASE_URL_FILE"],
            "/run/secrets/api_database_url",
        )
        self.assertEqual(
            compose["secrets"]["api_database_url"]["file"],
            "${API_DATABASE_URL_FILE:?required}",
        )
        self.assertEqual(
            compose["secrets"]["maintenance_database_url"]["file"],
            "${MAINTENANCE_DATABASE_URL_FILE:?required}",
        )
        self.assertEqual(
            compose["services"]["plugin-sandbox"]["image"],
            "${BACKEND_IMAGE:?required}",
        )
        self.assertEqual(compose["services"]["web"]["image"], "${FRONTEND_IMAGE:?required}")
        self.assertEqual(compose["services"]["api"]["ports"], [])
        self.assertEqual(compose["services"]["worker"]["ports"], [])
        self.assertEqual(
            compose["services"]["web"]["ports"],
            ["127.0.0.1:${WEB_PUBLISHED_PORT:?required}:80"],
        )

    def test_production_deployment_is_gated_and_rolls_back_images(self):
        workflow = (ROOT / ".github" / "workflows" / "deploy-production.yml").read_text(
            encoding="utf-8"
        )
        script = (ROOT / "scripts" / "deploy_transaction.sh").read_text(encoding="utf-8")
        ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        secure_compose = yaml.safe_load(
            (ROOT / "docker-compose.secure.yml").read_text(encoding="utf-8")
        )
        compose = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
        self.assertEqual(
            secure_compose["services"]["api"]["environment"]["RUN_MIGRATIONS"],
            "false",
        )
        self.assertEqual(
            compose["services"]["dispatcher"]["environment"]["RUN_MIGRATIONS"],
            "false",
        )
        self.assertEqual(compose["services"]["migration"]["profiles"], ["operations"])
        self.assertEqual(
            compose["services"]["migration"]["command"],
            ["alembic", "upgrade", "head"],
        )
        recovery = compose["services"]["recovery-check"]
        self.assertEqual(recovery["profiles"], ["operations"])
        self.assertEqual(recovery["network_mode"], "none")
        self.assertTrue(recovery["read_only"])
        publisher = compose["services"]["recovery-evidence-publisher"]
        self.assertEqual(publisher["network_mode"], "none")
        self.assertTrue(publisher["read_only"])
        storage_check = compose["services"]["storage-check"]
        self.assertEqual(storage_check["profiles"], ["operations"])
        self.assertTrue(storage_check["read_only"])
        pitr_checkpoint = compose["services"]["pitr-checkpoint"]
        self.assertEqual(pitr_checkpoint["profiles"], ["operations"])
        self.assertTrue(pitr_checkpoint["read_only"])
        self.assertEqual(
            compose["services"]["worker"]["stop_grace_period"],
            "${WORKER_STOP_GRACE_PERIOD:-150s}",
        )
        self.assertIn("release-images.next.env", workflow)
        self.assertIn("deploy_transaction.sh", workflow)
        self.assertIn("release-images.previous.env", script)
        self.assertIn('run --rm --no-deps recovery-evidence-publisher', script)
        self.assertIn('run --rm --no-deps recovery-check', script)
        self.assertIn('run --rm --no-deps storage-check', script)
        self.assertIn('run --rm --no-deps pitr-checkpoint', script)
        self.assertIn('run --rm migration', script)
        self.assertIn(
            'python -m src.artifact_backfill --apply --artifact-root /app/output',
            script,
        )
        self.assertIn('verify_database_roles.py', script)
        self.assertIn('rolling back to previous image digests', script)
        self.assertIn('RECOVERY_EVIDENCE_PATH=', script)
        self.assertIn("check_expand_contract_migrations.py", ci)
        self.assertIn('DATABASE_URL="$API_DATABASE_URL" DATABASE_ROLE=api', ci)
        self.assertIn(
            'DATABASE_URL="$WORKER_DATABASE_URL" DATABASE_ROLE=worker',
            ci,
        )
        self.assertIn('scripts/verify_database_roles.py', ci)
        self.assertIn("source_commit", workflow)
        self.assertIn("X-Expected-Release", script)
        self.assertLess(
            script.index('run --rm --no-deps storage-check'),
            script.index('run --rm --no-deps recovery-evidence-publisher'),
        )
        self.assertLess(
            script.index('run --rm --no-deps recovery-evidence-publisher'),
            script.index('run --rm --no-deps recovery-check'),
        )
        self.assertLess(
            script.index('run --rm --no-deps recovery-check'),
            script.index('run --rm --no-deps pitr-checkpoint'),
        )
        self.assertLess(
            script.index('run --rm --no-deps pitr-checkpoint'),
            script.index('run --rm migration'),
        )
        self.assertLess(
            script.index('run --rm migration'),
            script.index('up -d --no-build --remove-orphans'),
        )

    def test_monitoring_stack_loads_alerts_with_scoped_credentials(self):
        compose = yaml.safe_load(
            (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        )
        deploy = yaml.load(
            (ROOT / 'docker-compose.deploy.yml').read_text(encoding='utf-8'),
            Loader=ComposeLoader,
        )
        workflow = (
            ROOT / '.github' / 'workflows' / 'deploy-production.yml'
        ).read_text(encoding='utf-8')
        ci = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(
            encoding='utf-8'
        )
        script = (ROOT / 'scripts' / 'deploy_transaction.sh').read_text(
            encoding='utf-8'
        )
        prometheus = compose['services']['prometheus']
        alertmanager = compose['services']['alertmanager']
        monitoring_test = yaml.safe_load(
            (ROOT / 'docker-compose.monitoring-test.yml').read_text(
                encoding='utf-8'
            )
        )
        self.assertEqual(prometheus['profiles'], ['monitoring'])
        self.assertEqual(alertmanager['profiles'], ['monitoring'])
        self.assertRegex(prometheus['image'], SHA256_REFERENCE)
        self.assertRegex(alertmanager['image'], SHA256_REFERENCE)
        self.assertIn('metrics_scrape_token', prometheus['secrets'])
        self.assertIn('alertmanager_webhook_url', alertmanager['secrets'])
        self.assertEqual(
            prometheus['group_add'],
            ['${MONITORING_SECRET_GID:-1000}'],
        )
        self.assertEqual(
            alertmanager['group_add'],
            ['${MONITORING_SECRET_GID:-1000}'],
        )
        self.assertIn(
            '${MONITORING_SECRET_GID:?required}',
            monitoring_test['services']['api']['group_add'],
        )
        self.assertIn('alert-webhook-sink', monitoring_test['services'])
        self.assertEqual(deploy['services']['prometheus']['ports'], [])
        self.assertEqual(deploy['services']['alertmanager']['ports'], [])
        self.assertIn('monitoring/prometheus.yml', workflow)
        self.assertIn('monitoring/alertmanager.yml', workflow)
        self.assertIn('monitoring/storage-deletion-alerts.yml', workflow)
        self.assertIn('test rules /etc/prometheus/storage-deletion-alerts.test.yml', ci)
        self.assertIn('check-config /etc/alertmanager/alertmanager.yml', ci)
        self.assertIn('verify_monitoring_stack.py', ci)
        self.assertIn('docker-compose.monitoring-test.yml', ci)
        self.assertIn('alert-webhook-sink prometheus alertmanager', ci)
        self.assertIn('chmod 0777 output/monitoring-smoke', ci)
        self.assertIn('MONITORING_SECRET_GID', script)
        self.assertIn("stat -c '%g'", script)
        self.assertIn('--profile monitoring', script)
        self.assertIn('prometheus alertmanager', script)

        prometheus_config = yaml.safe_load(
            (ROOT / 'monitoring' / 'prometheus.yml').read_text(encoding='utf-8')
        )
        api_scrape = next(
            item
            for item in prometheus_config['scrape_configs']
            if item['job_name'] == 'bioagent-api'
        )
        self.assertEqual(
            api_scrape['authorization']['credentials_file'],
            '/run/secrets/metrics_scrape_token',
        )
        self.assertEqual(
            prometheus_config['rule_files'],
            ['/etc/prometheus/rules/*.yml'],
        )
        alertmanager_config = yaml.safe_load(
            (ROOT / 'monitoring' / 'alertmanager.yml').read_text(encoding='utf-8')
        )
        webhook = alertmanager_config['receivers'][0]['webhook_configs'][0]
        self.assertEqual(
            webhook['url_file'],
            '/run/secrets/alertmanager_webhook_url',
        )

    def test_recovery_drill_restores_durable_state_and_discards_redis(self):
        workflow = (
            ROOT / ".github" / "workflows" / "recovery-drill.yml"
        ).read_text(encoding="utf-8")
        script = (ROOT / "scripts" / "recovery_drill.py").read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn('cron: "37 2 * * 0"', workflow)
        self.assertIn("--max-rpo-seconds 900", workflow)
        self.assertIn("--max-rto-seconds 60", workflow)
        self.assertIn("Capture recovery failure diagnostics", workflow)
        self.assertIn("logs --no-color --timestamps", workflow)
        self.assertIn("Clean up isolated recovery services", workflow)
        self.assertIn("Publish recovery summary", workflow)
        self.assertIn("--volumes", script)
        self.assertIn("pg_dump", script)
        self.assertIn("pg_restore", script)
        self.assertIn('"alembic", "upgrade", "head"', script)
        self.assertIn('asyncio.run(ensure_database_roles(args))', script)
        self.assertIn('CREATE ROLE bioagent_dispatcher NOLOGIN', script)
        self.assertIn('CREATE ROLE bioagent_maintenance NOLOGIN', script)
        self.assertIn("backup_objects", script)
        self.assertIn("restore_objects", script)
        self.assertIn('"backed_up": False', script)
        self.assertIn("verify_redis_is_disposable", script)
        self.assertIn('metrics["simulated_rpo_seconds"]', script)
        self.assertIn('metrics["rto_seconds"]', script)
        self.assertIn('backup_dir / "failure.json"', script)

    def test_third_party_image_vulnerabilities_use_expiring_baselines(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        baseline_path = ROOT / "security" / "container-vulnerability-baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertIn("check_container_vulnerability_baseline.py", workflow)
        self.assertIn("format: json", workflow)
        self.assertIn("retention-days: 90", workflow)
        self.assertEqual(
            set(baseline["images"]),
            {"redis", "postgres", "clamav", "prometheus", "alertmanager"},
        )
        for image in baseline["images"].values():
            self.assertRegex(image["image_ref"], SHA256_REFERENCE)
        self.assertEqual(baseline["policy"]["max_exception_days"], {"HIGH": 30, "CRITICAL": 7})

    def test_dependency_license_and_signature_policy_is_enforced(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        package = json.loads((ROOT / "frontend" / "package.json").read_text(encoding="utf-8"))
        self.assertIn("pip-licenses==5.5.5", project["dependency-groups"]["dev"])
        self.assertEqual(project["project"]["license"], "MIT")
        self.assertTrue(project["tool"]["pip-licenses"]["allow-only"].strip())
        self.assertIn("licenses:check", package["scripts"])
        self.assertIn("npm audit signatures --registry=https://registry.npmjs.org", workflow)
        self.assertIn("pip-licenses --output-file=output/python-licenses.json", workflow)
        self.assertIn("dependency-license-evidence", workflow)

    def test_scorecard_and_workflow_codeowners_are_configured(self):
        scorecard = (ROOT / ".github" / "workflows" / "scorecard.yml").read_text(
            encoding="utf-8"
        )
        codeowners = (ROOT / ".github" / "CODEOWNERS").read_text(encoding="utf-8")
        self.assertIn("permissions: read-all", scorecard)
        self.assertIn("security-events: write", scorecard)
        self.assertIn("id-token: write", scorecard)
        self.assertIn("persist-credentials: false", scorecard)
        self.assertIn("publish_results: true", scorecard)
        self.assertIn("branches: [main]", scorecard)
        self.assertIn("/.github/workflows/ @wujizhesan", codeowners)

    def test_dispatcher_has_distinct_database_and_redis_identity(self):
        compose = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
        deploy = (ROOT / 'docker-compose.deploy.yml').read_text(encoding='utf-8')
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(
            encoding='utf-8'
        )
        project = tomllib.loads((ROOT / 'pyproject.toml').read_text(encoding='utf-8'))
        self.assertIn('DISPATCHER_DATABASE_URL', compose)
        self.assertIn('DISPATCHER_REDIS_URL', compose)
        self.assertIn('DATABASE_ROLE: dispatcher', deploy)
        self.assertIn('bioagent_dispatcher_ci', workflow)
        self.assertIn('python -m src.dispatcher', workflow)
        self.assertIn('dispatcher', project['tool']['setuptools']['py-modules'])

    def test_frontend_security_headers_block_active_report_content(self):
        nginx = (ROOT / 'frontend' / 'nginx.conf').read_text(encoding='utf-8')
        self.assertIn("default-src 'self'", nginx)
        self.assertIn("script-src 'self'", nginx)
        self.assertNotIn("script-src 'self' 'unsafe-inline'", nginx)
        self.assertIn("object-src 'none'", nginx)
        self.assertIn("frame-ancestors 'none'", nginx)
        self.assertIn('X-Content-Type-Options "nosniff"', nginx)
        self.assertIn('X-Frame-Options "DENY"', nginx)
        self.assertIn('Strict-Transport-Security "max-age=86400"', nginx)
        self.assertNotIn('includeSubDomains', nginx)
        self.assertNotIn('preload', nginx)

    def test_ci_runs_real_container_plugin_sandbox(self):
        workflow = (ROOT / '.github' / 'workflows' / 'ci.yml').read_text(
            encoding='utf-8'
        )
        secure_compose = yaml.safe_load(
            (ROOT / 'docker-compose.secure.yml').read_text(encoding='utf-8')
        )
        job = yaml.safe_load(workflow)['jobs']['secure-plugin-e2e']
        rendered = json.dumps(job)
        self.assertIn('docker-compose.secure.yml', rendered)
        self.assertIn('up -d --wait db redis', rendered)
        self.assertIn('verify_secure_fullstack_e2e.py', rendered)
        self.assertIn('plugin-sandbox', rendered)
        self.assertIn('migration', rendered)
        self.assertIn('dispatcher', rendered)
        self.assertIn('configure_tenant_context.py --enable', rendered)
        self.assertIn('verify_database_roles.py', rendered)
        self.assertIn('bioagent_api_secure', rendered)
        self.assertIn(
            'sudo chown 1000:1000 \\"$PLUGIN_SANDBOX_TOKEN_FILE\\"', rendered
        )
        self.assertIn(
            'sudo chmod 0400 \\"$PLUGIN_SANDBOX_TOKEN_FILE\\"', rendered
        )
        self.assertIn('sudo rm -f', rendered)
        self.assertIn('\\"$PLUGIN_SANDBOX_TOKEN_FILE\\"', rendered)
        self.assertIn('\\"$METRICS_SCRAPE_TOKEN_SECRET_FILE\\"', rendered)
        self.assertIn('\\"$ALERTMANAGER_WEBHOOK_URL_SECRET_FILE\\"', rendered)
        self.assertIn('for attempt in 1 2 3', rendered)
        self.assertIn('sleep $((attempt * 10))', rendered)
        self.assertIn('JOB_EXECUTION_MODE', (
            ROOT / 'docker-compose.secure.yml'
        ).read_text(encoding='utf-8'))
        self.assertIn('/app/output', rendered)
        self.assertIn('/run/bioagent/plugin-exchange', rendered)
        self.assertIn('.boundary-probe', rendered)
        self.assertIn('down -v --remove-orphans', rendered)
        self.assertEqual(job['timeout-minutes'], 35)
        self.assertEqual(
            secure_compose['secrets']['plugin_sandbox_token'],
            {'file': '${PLUGIN_SANDBOX_TOKEN_FILE:?required}'},
        )


if __name__ == "__main__":
    unittest.main()
