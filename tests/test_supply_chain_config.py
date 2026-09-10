import json
import re
import tomllib
import unittest
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[1]
SHA256_REFERENCE = re.compile(r"@sha256:[0-9a-f]{64}$")


class SupplyChainConfigurationTests(unittest.TestCase):
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
        self.assertRegex(dockerfile, r"(?m)^USER bioagent$")

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
        self.assertIn('      - "v*"', workflow)

    def test_third_party_image_vulnerabilities_use_expiring_baselines(self):
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        baseline_path = ROOT / "security" / "container-vulnerability-baseline.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
        self.assertIn("check_container_vulnerability_baseline.py", workflow)
        self.assertIn("format: json", workflow)
        self.assertIn("retention-days: 90", workflow)
        self.assertEqual(set(baseline["images"]), {"redis", "postgres", "clamav"})
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


if __name__ == "__main__":
    unittest.main()
