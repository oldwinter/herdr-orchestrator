from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
RELEASE_PLAN = REPO_ROOT / "scripts/npm-release-plan.mjs"
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"


class NpmReleasePlanTests(unittest.TestCase):
    def test_missing_registry_version_is_publishable(self) -> None:
        result, _ = self._run_plan("1.2.0", '["1.0.0", "1.1.0"]')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "name": "example-package",
                "publish": True,
                "reason": "version_missing",
                "version": "1.2.0",
            },
        )

    def test_existing_registry_version_is_not_publishable(self) -> None:
        result, _ = self._run_plan("1.2.0", '["1.1.0", "1.2.0"]')

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "name": "example-package",
                "publish": False,
                "reason": "version_exists",
                "version": "1.2.0",
            },
        )

    def test_plan_writes_github_action_outputs(self) -> None:
        result, github_output = self._run_plan(
            "1.2.0",
            '["1.1.0"]',
            write_github_output=True,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            github_output.splitlines(),
            [
                "name=example-package",
                "version=1.2.0",
                "publish=true",
                "reason=version_missing",
            ],
        )

    def test_registry_failure_stops_release_planning(self) -> None:
        result, github_output = self._run_plan(
            "1.2.0",
            '["1.1.0"]',
            npm_exit=1,
            write_github_output=True,
        )

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stderr.strip(), "npm_registry_query_failed")
        self.assertEqual(github_output, "")

    def _run_plan(
        self,
        version: str,
        registry_versions: str,
        *,
        npm_exit: int = 0,
        write_github_output: bool = False,
    ) -> tuple[subprocess.CompletedProcess[str], str]:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / "package.json"
            package.write_text(
                json.dumps(
                    {
                        "name": "example-package",
                        "version": version,
                    }
                ),
                encoding="utf-8",
            )
            fake_bin = root / "bin"
            fake_bin.mkdir()
            npm = fake_bin / "npm"
            npm.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import sys\n"
                "expected = ['view', 'example-package', 'versions', '--json']\n"
                "if sys.argv[1:] != expected:\n"
                "    raise SystemExit(9)\n"
                "if os.environ['NPM_EXIT'] != '0':\n"
                "    raise SystemExit(int(os.environ['NPM_EXIT']))\n"
                "print(os.environ['REGISTRY_VERSIONS'])\n",
                encoding="utf-8",
            )
            npm.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "NPM_EXIT": str(npm_exit),
                "REGISTRY_VERSIONS": registry_versions,
            }
            github_output = root / "github-output.txt"
            if write_github_output:
                environment["GITHUB_OUTPUT"] = str(github_output)
            result = subprocess.run(
                [
                    "node",
                    str(RELEASE_PLAN),
                    "--package-json",
                    str(package),
                ],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            output = github_output.read_text(encoding="utf-8") if github_output.is_file() else ""
            return result, output


class NpmReleaseWorkflowTests(unittest.TestCase):
    def test_untrusted_pr_code_uses_github_hosted_runner(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertEqual(
            workflow.count("runs-on: [self-hosted, Linux, X64, herdr-orchestrator]"),
            1,
        )
        self.assertEqual(workflow.count("runs-on: ubuntu-latest"), 4)
        self.assertIn("release-plan:", workflow)
        self.assertIn(
            "publish: ${{ steps.plan.outputs.publish }}",
            workflow,
        )
        self.assertIn(
            "needs.release-plan.outputs.publish == 'true'",
            workflow,
        )
        self.assertEqual(workflow.count("persist-credentials: false"), 3)

    def test_main_publish_uses_test_gate_oidc_and_version_plan(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("publish:", workflow)
        self.assertIn("needs: test", workflow)
        self.assertIn("github.event_name == 'push'", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", workflow)
        self.assertIn("environment: npm", workflow)
        self.assertIn("id-token: write", workflow)
        self.assertIn(
            "node scripts/npm-release-plan.mjs --package-json package.json",
            workflow,
        )
        self.assertIn("needs.release-plan.outputs.publish == 'true'", workflow)
        self.assertIn("npm publish --access public", workflow)
        self.assertNotIn("--provenance", workflow)
        self.assertNotIn("NODE_AUTH_TOKEN", workflow)
        self.assertIn("actions/checkout@11d5960a326750d5838078e36cf38b85af677262", workflow)
        self.assertIn("actions/setup-node@49933ea5288caeca8642d1e84afbd3f7d6820020", workflow)
        self.assertIn(
            "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065",
            workflow,
        )
        self.assertIn("npm install --global rust-just@1.57.0", workflow)
        self.assertNotIn("actions/checkout@v4", workflow)
        self.assertNotIn("actions/setup-node@v4", workflow)
        self.assertNotIn("actions/setup-python@v5", workflow)

    def test_local_npm_tokens_are_ignored(self) -> None:
        ignore_rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

        self.assertIn(".env", ignore_rules)


if __name__ == "__main__":
    unittest.main()
