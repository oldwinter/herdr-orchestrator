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
test_stability = _load_script("test_stability")

RELEASE_PLAN = REPO_ROOT / "scripts/npm-release-plan.mjs"
CI_WORKFLOW = REPO_ROOT / ".github/workflows/ci.yml"


def _workflow_run_block(workflow: str, step_name: str) -> str:
    lines = workflow.splitlines()
    marker = f"      - name: {step_name}"
    try:
        step_start = lines.index(marker)
    except ValueError as error:
        raise AssertionError(f"workflow step missing: {step_name}") from error
    run_start = next(
        (index for index in range(step_start + 1, len(lines)) if lines[index] == "        run: |"),
        None,
    )
    if run_start is None:
        raise AssertionError(f"workflow step has no literal run block: {step_name}")
    body: list[str] = []
    for line in lines[run_start + 1 :]:
        if line.startswith("      - ") or line and not line.startswith("        "):
            break
        body.append(line[10:] if line.startswith("          ") else "")
    return "\n".join(body)


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
    def test_ci_collects_one_quality_bundle_and_enforces_it_independently(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        test_job = workflow[workflow.index("  test:\n") : workflow.index("  pr-review:\n")]

        self.assertEqual(test_job.count("quality_bundle.py run"), 1)
        self.assertIn("--all", test_job)
        self.assertIn('--commit "$GITHUB_SHA"', test_job)
        self.assertIn('--invocation "$GITHUB_RUN_ID-$GITHUB_RUN_ATTEMPT"', test_job)
        self.assertIn('--result "$RUNNER_TEMP/quality-result.json"', test_job)
        self.assertIn("Generate quality review", test_job)
        self.assertIn("quality_summary.py", test_job)
        self.assertIn("--result", test_job)
        self.assertIn("id: summary", test_job)
        self.assertIn("SUMMARY_STATUS: ${{ steps.summary.outcome }}", test_job)
        self.assertIn("Enforce quality manifest", test_job)
        self.assertIn("quality_bundle.py enforce", test_job)
        self.assertIn('test "$SUMMARY_STATUS" = success', test_job)
        self.assertLess(
            test_job.index("Collect quality bundle"), test_job.index("Generate quality review")
        )
        self.assertLess(
            test_job.index("Generate quality review"), test_job.index("Upload quality evidence")
        )
        self.assertLess(
            test_job.index("Upload quality evidence"), test_job.index("Enforce quality manifest")
        )
        self.assertNotIn("just lint", test_job)
        self.assertNotIn("just test-coverage", test_job)
        self.assertNotIn("security-status.json", test_job)

    def test_local_check_uses_one_explicit_bundle_manifest(self) -> None:
        justfile = (REPO_ROOT / "justfile").read_text(encoding="utf-8")
        check = justfile[justfile.index("check:\n") : justfile.index("seed:\n")]

        self.assertEqual(check.count("quality_bundle.py run"), 1)
        self.assertIn("--all", check)
        self.assertIn("--result", check)
        self.assertIn("quality_summary.py", check)
        self.assertIn('quality_summary.py --result "$result"', check)
        self.assertIn("quality_bundle.py enforce", check)
        self.assertNotIn("just lint", check)
        self.assertNotIn("just test-coverage", check)

    def test_untrusted_pr_code_uses_github_hosted_runner(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertEqual(
            workflow.count("runs-on: [self-hosted, Linux, X64, herdr-orchestrator]"),
            1,
        )
        self.assertEqual(workflow.count("runs-on: ubuntu-latest"), 5)
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
        self.assertEqual(
            workflow.count("actions/setup-node@820762786026740c76f36085b0efc47a31fe5020"),
            3,
        )
        self.assertEqual(
            workflow.count("actions/setup-python@5fda3b95a4ea91299a34e894583c3862153e4b97"),
            1,
        )
        self.assertEqual(
            workflow.count("astral-sh/setup-uv@20cfd1bf945f4377ade1205e4dbc17946fc9a30d"),
            1,
        )
        self.assertEqual(
            workflow.count("actions/upload-artifact@043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"),
            1,
        )
        self.assertEqual(
            workflow.count("actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c"),
            1,
        )
        self.assertIn("npm install --global rust-just@1.57.0", workflow)
        self.assertNotIn("actions/checkout@v4", workflow)
        self.assertNotIn("actions/setup-node@v4", workflow)
        self.assertNotIn("actions/setup-python@v5", workflow)

    def test_local_npm_tokens_are_ignored(self) -> None:
        ignore_rules = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

        self.assertIn(".env", ignore_rules)

    def test_release_note_labels_exist_in_the_local_manifest(self) -> None:
        release = (REPO_ROOT / ".github/release.yml").read_text(encoding="utf-8")
        labels = (REPO_ROOT / ".github/labels.yml").read_text(encoding="utf-8")

        self.assertIn("skip-changelog", labels)
        self.assertIn("breaking-change", labels)
        self.assertIn("skip-changelog", release)
        self.assertIn("breaking-change", release)

    def test_release_publish_and_github_release_are_separate_least_privilege_jobs(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")

        self.assertIn(
            "cancel-in-progress: ${{ github.event_name != 'push' || "
            "github.ref != 'refs/heads/main' }}",
            workflow,
        )
        publish_start = workflow.index("  publish:\n")
        release_start = workflow.index("  github-release:\n")
        publish_job = workflow[publish_start:release_start]
        release_job = workflow[release_start:]
        self.assertIn("runs-on: ubuntu-latest", publish_job)
        self.assertIn("permissions:\n      contents: read\n      id-token: write", publish_job)
        self.assertNotIn("contents: write", publish_job)
        self.assertIn("permissions:\n      contents: write", release_job)
        self.assertNotIn("id-token: write", release_job)
        self.assertIn("needs: [release-plan, publish]", release_job)
        self.assertIn("always()", release_job)
        self.assertIn("!cancelled()", release_job)
        self.assertIn("needs.publish.result == 'success'", release_job)
        self.assertIn("needs.publish.result == 'skipped'", release_job)

    def test_two_attempt_release_model_completes_after_partial_publish(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        publish = _workflow_run_block(workflow, "Publish package versions")
        verify = _workflow_run_block(workflow, "Verify package versions")
        self.assertIn("npm publish --access public", publish)
        self.assertIn("npm publish --access public ./packages/herdr-manager", publish)
        self.assertIn('npm view "$name@$version" version', verify)

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            state = root / "registry.txt"
            state.write_text("", encoding="utf-8")
            npm = fake_bin / "npm"
            npm.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import pathlib\n"
                "import sys\n"
                "state = pathlib.Path(os.environ['REGISTRY_STATE'])\n"
                "versions = set(filter(None, state.read_text().splitlines()))\n"
                "args = sys.argv[1:]\n"
                "if args[:1] == ['publish']:\n"
                "    package = (\n"
                "        'example-package@1.2.0' if len(args) == 3 else 'herdr-manager@0.2.0'\n"
                "    )\n"
                "    if (\n"
                "        os.environ.get('FAIL_MANAGER') == '1'\n"
                "        and package.startswith('herdr-manager@')\n"
                "    ):\n"
                "        raise SystemExit(17)\n"
                "    versions.add(package)\n"
                "    state.write_text('\\n'.join(sorted(versions)) + '\\n')\n"
                "    raise SystemExit(0)\n"
                "if args[:1] == ['view'] and len(args) == 3 and args[2] == 'version':\n"
                "    package = args[1]\n"
                "    if package in versions:\n"
                "        print(package.rsplit('@', 1)[1])\n"
                "        raise SystemExit(0)\n"
                "    raise SystemExit(1)\n"
                "raise SystemExit(19)\n",
                encoding="utf-8",
            )
            npm.chmod(0o755)
            script = "\n".join((publish, verify))
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "REGISTRY_STATE": str(state),
                "ORCHESTRATOR_PUBLISH": "true",
                "MANAGER_PUBLISH": "true",
                "ORCHESTRATOR_NAME": "example-package",
                "ORCHESTRATOR_VERSION": "1.2.0",
                "MANAGER_NAME": "herdr-manager",
                "MANAGER_VERSION": "0.2.0",
                "FAIL_MANAGER": "1",
            }
            first = subprocess.run(
                ["bash", "-eu", "-c", script],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertNotEqual(first.returncode, 0, first.stderr)
            self.assertEqual(
                state.read_text(encoding="utf-8").splitlines(), ["example-package@1.2.0"]
            )

            environment.update(
                {
                    "ORCHESTRATOR_PUBLISH": "false",
                    "MANAGER_PUBLISH": "true",
                    "FAIL_MANAGER": "0",
                }
            )
            second = subprocess.run(
                ["bash", "-eu", "-c", script],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(
                state.read_text(encoding="utf-8").splitlines(),
                ["example-package@1.2.0", "herdr-manager@0.2.0"],
            )

            environment.update(
                {
                    "ORCHESTRATOR_PUBLISH": "false",
                    "MANAGER_PUBLISH": "false",
                }
            )
            no_op = subprocess.run(
                ["bash", "-eu", "-c", script],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(no_op.returncode, 0, no_op.stderr)

    def test_github_release_model_is_idempotent_across_retries(self) -> None:
        workflow = CI_WORKFLOW.read_text(encoding="utf-8")
        release = _workflow_run_block(workflow, "Ensure GitHub release exists")

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            state = root / "release.txt"
            gh = fake_bin / "gh"
            gh.write_text(
                "#!/usr/bin/env python3\n"
                "import os\n"
                "import pathlib\n"
                "import sys\n"
                "state = pathlib.Path(os.environ['RELEASE_STATE'])\n"
                "args = sys.argv[1:]\n"
                "if args[:2] == ['release', 'view']:\n"
                "    raise SystemExit(0 if state.is_file() else 1)\n"
                "if args[:2] == ['release', 'create']:\n"
                "    state.write_text('created', encoding='utf-8')\n"
                "    if os.environ.get('FAIL_CREATE_ONCE') == '1':\n"
                "        os.environ.pop('FAIL_CREATE_ONCE', None)\n"
                "        raise SystemExit(23)\n"
                "    raise SystemExit(0)\n"
                "raise SystemExit(19)\n",
                encoding="utf-8",
            )
            gh.chmod(0o755)
            environment = {
                **os.environ,
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "RELEASE_STATE": str(state),
                "GITHUB_REPOSITORY": "oldwinter/herdr-orchestrator",
                "GITHUB_SHA": "ebcea06",
                "VERSION": "1.2.0",
                "FAIL_CREATE_ONCE": "1",
            }
            first = subprocess.run(
                ["bash", "-eu", "-c", release],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertTrue(state.is_file())

            second = subprocess.run(
                ["bash", "-eu", "-c", release],
                cwd=REPO_ROOT,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
            self.assertEqual(second.returncode, 0, second.stderr)


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


if __name__ == "__main__":
    unittest.main()
