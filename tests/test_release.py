from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"quality_{name}", path)
    if spec is None or spec.loader is None:
        raise AssertionError(f"unable to load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


build_metrics = _load_script("build_metrics")
quality_summary = _load_script("quality_summary")
test_stability = _load_script("test_stability")

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

    def test_missing_registry_package_is_publishable(self) -> None:
        result, _ = self._run_plan(
            "0.1.0",
            "[]",
            npm_exit=1,
            npm_stderr="npm error code E404\nnpm error 404 Not Found\n",
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout),
            {
                "name": "example-package",
                "publish": True,
                "reason": "version_missing",
                "version": "0.1.0",
            },
        )

    def _run_plan(
        self,
        version: str,
        registry_versions: str,
        *,
        npm_exit: int = 0,
        npm_stderr: str = "",
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
                "sys.stderr.write(os.environ['NPM_STDERR'])\n"
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
                "NPM_STDERR": npm_stderr,
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
            "orchestrator_publish: ${{ steps.orchestrator.outputs.publish }}",
            workflow,
        )
        self.assertIn(
            "manager_publish: ${{ steps.manager.outputs.publish }}",
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
        self.assertIn(
            "node scripts/npm-release-plan.mjs "
            "--package-json packages/herdr-manager/package.json",
            workflow,
        )
        self.assertIn("needs.release-plan.outputs.orchestrator_publish == 'true'", workflow)
        self.assertIn("needs.release-plan.outputs.manager_publish == 'true'", workflow)
        self.assertIn("npm publish --access public", workflow)
        self.assertIn(
            "npm publish --access public ./packages/herdr-manager",
            workflow,
        )
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


class QualityScriptTests(unittest.TestCase):
    def test_build_metrics_preserves_a_nonzero_pack_exit(self) -> None:
        result = subprocess.CompletedProcess(
            ["npm", "pack", "--dry-run", "--json"],
            9,
            "",
            "pack failed",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "build.json"
            with (
                patch.object(build_metrics.subprocess, "run", return_value=result),
                patch.object(sys, "argv", ["build_metrics.py", "--output", str(output)]),
            ):
                status = build_metrics.main()

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(status, 9)
        self.assertEqual(payload["exit_code"], 9)
        self.assertEqual(payload["error_code"], "npm_pack_failed")

    def test_build_metrics_rejects_an_empty_success_response(self) -> None:
        result = subprocess.CompletedProcess(
            ["npm", "pack", "--dry-run", "--json"],
            0,
            "[]\n",
            "",
        )
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "build.json"
            with (
                patch.object(build_metrics.subprocess, "run", return_value=result),
                patch.object(sys, "argv", ["build_metrics.py", "--output", str(output)]),
            ):
                status = build_metrics.main()

            payload = json.loads(output.read_text(encoding="utf-8"))

        self.assertEqual(status, 1)
        self.assertEqual(payload["exit_code"], 0)
        self.assertEqual(payload["error_code"], "npm_pack_response_invalid")

    def test_test_stability_rejects_a_missing_report_and_uses_current_python(self) -> None:
        result = subprocess.CompletedProcess([sys.executable], 0, "", "")
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "stability.json"
            with (
                patch.object(test_stability.subprocess, "run", return_value=result) as run,
                patch.object(
                    sys,
                    "argv",
                    ["test_stability.py", "--runs", "2", "--output", str(output)],
                ),
            ):
                status = test_stability.main()

            payload = json.loads(output.read_text(encoding="utf-8"))
            command = run.call_args.args[0]

        self.assertEqual(status, 1)
        self.assertEqual(command[0], sys.executable)
        self.assertIn("-p", command)
        self.assertIn("no:cacheprovider", command)
        self.assertEqual(payload["executions"][0]["error_code"], "report_missing")

    def test_quality_summary_propagates_failed_quality_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            quality_root = Path(temporary) / "quality"
            quality_root.mkdir()
            (quality_root / "coverage.json").write_text(
                json.dumps({"totals": {"percent_covered": 82.0}}),
                encoding="utf-8",
            )
            (quality_root / "stability.json").write_text(
                json.dumps(
                    {
                        "runs": 2,
                        "unstable": [],
                        "executions": [
                            {"exit_code": 1},
                            {"exit_code": 1},
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (quality_root / "build.json").write_text(
                json.dumps({"exit_code": 7}),
                encoding="utf-8",
            )
            (quality_root / "bandit.json").write_text(
                json.dumps({"results": []}),
                encoding="utf-8",
            )
            (quality_root / "pip-audit.json").write_text(
                json.dumps({"dependencies": []}),
                encoding="utf-8",
            )
            output = Path(temporary) / "summary.md"
            with (
                patch.object(quality_summary, "QUALITY_ROOT", quality_root),
                patch.object(sys, "argv", ["quality_summary.py", "--output", str(output)]),
            ):
                status = quality_summary.main()

            summary = output.read_text(encoding="utf-8")

        self.assertEqual(status, 1)
        self.assertIn("Stability: **FAILED**", summary)
        self.assertIn("exit codes: 1", summary)
        self.assertIn("Build: **FAILED**", summary)
        self.assertIn("exit code 7", summary)

    def test_quality_summary_rejects_missing_quality_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            quality_root = Path(temporary) / "quality"
            quality_root.mkdir()
            output = Path(temporary) / "summary.md"
            with (
                patch.object(quality_summary, "QUALITY_ROOT", quality_root),
                patch.object(sys, "argv", ["quality_summary.py", "--output", str(output)]),
            ):
                status = quality_summary.main()

            summary = output.read_text(encoding="utf-8")

        self.assertEqual(status, 1)
        self.assertIn("Coverage: **unavailable**", summary)
        self.assertIn("Security: **unavailable**", summary)

    def test_quality_summary_rejects_an_unbounded_coverage_number(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            quality_root = Path(temporary) / "quality"
            quality_root.mkdir()
            (quality_root / "coverage.json").write_text(
                json.dumps({"totals": {"percent_covered": 10**1000}}),
                encoding="utf-8",
            )
            output = Path(temporary) / "summary.md"
            with (
                patch.object(quality_summary, "QUALITY_ROOT", quality_root),
                patch.object(sys, "argv", ["quality_summary.py", "--output", str(output)]),
            ):
                status = quality_summary.main()

            summary = output.read_text(encoding="utf-8")

        self.assertEqual(status, 1)
        self.assertIn("Coverage: **unavailable**", summary)


if __name__ == "__main__":
    unittest.main()
