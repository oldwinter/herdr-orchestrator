from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


class QualityCommandTests(unittest.TestCase):
    def test_public_runner_binds_tracked_and_untracked_source_changes(self) -> None:
        for label, status_line in (
            ("tracked", " M scripts/quality_bundle.py"),
            ("untracked", "?? untracked.py"),
        ):
            with self.subTest(label=label), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                commands = root / "bin"
                commands.mkdir()
                git = commands / "git"
                git.write_text(
                    "#!/usr/bin/env python3\n"
                    "import os\n"
                    "import sys\n"
                    "args = sys.argv[1:]\n"
                    "if args[:2] == ['rev-parse', 'HEAD']:\n"
                    "    print('a' * 40)\n"
                    "elif args[:1] == ['status']:\n"
                    "    print(os.environ['DIRTY_STATUS'])\n"
                    "elif args[:1] == ['diff']:\n"
                    "    sys.stdout.buffer.write(os.environ['SOURCE_DIFF'].encode())\n"
                    "elif args[:2] == ['ls-files', '-z']:\n"
                    "    names = os.environ['UNTRACKED_FILES']\n"
                    "    if names:\n"
                    "        sys.stdout.buffer.write(names.encode() + b'\\0')\n"
                    "else:\n"
                    "    raise SystemExit(19)\n",
                    encoding="utf-8",
                )
                git.chmod(0o755)
                evidence_root = root / "quality"
                environment = os.environ.copy()
                environment.update(
                    {
                        "DIRTY_STATUS": status_line,
                        "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                        "SOURCE_DIFF": "tracked-diff" if label == "tracked" else "",
                        "UNTRACKED_FILES": "AGENTS.md" if label == "untracked" else "",
                    }
                )

                result = subprocess.run(
                    [
                        sys.executable,
                        "scripts/quality_bundle.py",
                        "run",
                        "--root",
                        str(evidence_root),
                        "--producer",
                        "build",
                    ],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                    timeout=30,
                )

                run = next((evidence_root / "runs").iterdir())
                manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(manifest["source_clean"])
                self.assertTrue(manifest["source_consistent"])
                self.assertEqual(len(manifest["source_digest"]), 64)
                result_path = next((evidence_root / "results").glob("*.json"))
                commit_grade = subprocess.run(
                    [
                        sys.executable,
                        "scripts/quality_bundle.py",
                        "enforce",
                        "--result",
                        str(result_path),
                        "--require-clean",
                    ],
                    cwd=REPO_ROOT,
                    capture_output=True,
                    text=True,
                    check=False,
                    env=environment,
                    timeout=30,
                )
                self.assertEqual(commit_grade.returncode, 2)
                self.assertIn("quality_source_not_clean", commit_grade.stderr)

    def test_full_bundle_collects_all_six_producer_outcomes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "quality"
            commands = root / "bin"
            commands.mkdir()
            self._write_full_quality_tools(commands)
            environment = os.environ.copy()
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_bundle.py",
                    "run",
                    "--root",
                    str(evidence_root),
                    "--all",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            self.assertTrue((evidence_root / "runs").is_dir(), result.stderr)
            run = next((evidence_root / "runs").iterdir())
            manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [producer["name"] for producer in manifest["producers"]],
            ["lint", "coverage", "stability", "security", "build", "profiling"],
        )
        for producer in manifest["producers"]:
            self.assertEqual(producer["verification"], "verified")
            self.assertTrue(producer["commands"])
            self.assertTrue(producer["started_at"].endswith("Z"))
            self.assertTrue(producer["ended_at"].endswith("Z"))
            self.assertTrue(producer["tool_versions"])
            self.assertTrue(producer["artifacts"])
            self.assertTrue(all(len(item["sha256"]) == 64 for item in producer["artifacts"]))

    def test_clean_full_result_summarizes_and_enforces_without_shared_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "quality"
            result_path = root / "current-result.json"
            summary_path = root / "current-summary.md"
            commands = root / "bin"
            commands.mkdir()
            self._write_full_quality_tools(commands)
            environment = os.environ.copy()
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"

            collect = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_bundle.py",
                    "run",
                    "--root",
                    str(evidence_root),
                    "--all",
                    "--result",
                    str(result_path),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )
            summarize = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_summary.py",
                    "--result",
                    str(result_path),
                    "--require-clean",
                    "--output",
                    str(summary_path),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )
            enforce = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_bundle.py",
                    "enforce",
                    "--result",
                    str(result_path),
                    "--require-full",
                    "--require-clean",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            summary = summary_path.read_text(encoding="utf-8")

        self.assertEqual(collect.returncode, 0, collect.stderr)
        self.assertEqual(summarize.returncode, 0, summarize.stderr)
        self.assertEqual(enforce.returncode, 0, enforce.stderr)
        self.assertIn("Coverage: **91.0%**", summary)
        self.assertIn("Security: **0** medium/high Bandit findings", summary)

    def test_full_bundle_rejects_each_omitted_missing_and_corrupt_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "quality"
            result_path = root / "base-result.json"
            commands = root / "bin"
            commands.mkdir()
            self._write_full_quality_tools(commands)
            environment = os.environ.copy()
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"
            collect = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_bundle.py",
                    "run",
                    "--root",
                    str(evidence_root),
                    "--all",
                    "--result",
                    str(result_path),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )
            self.assertEqual(collect.returncode, 0, collect.stderr)
            base_result = json.loads(result_path.read_text(encoding="utf-8"))
            base_bundle = Path(base_result["bundle"])
            base_manifest = json.loads(Path(base_result["manifest"]).read_text(encoding="utf-8"))
            artifacts = [
                (producer["name"], artifact)
                for producer in base_manifest["producers"]
                for artifact in producer["artifacts"]
            ]
            self.assertEqual(len(artifacts), 15)

            for index, (producer_name, artifact) in enumerate(artifacts):
                for mode, error_code in (
                    ("omitted", "quality_producer_incomplete"),
                    ("missing", "quality_artifact_missing"),
                    ("corrupt", "quality_artifact_digest_mismatch"),
                ):
                    with self.subTest(producer=producer_name, key=artifact["key"], mode=mode):
                        case_root = root / "cases" / f"{index}-{mode}"
                        case_bundle = case_root / "runs" / base_bundle.name
                        case_bundle.parent.mkdir(parents=True)
                        shutil.copytree(base_bundle, case_bundle)
                        case_manifest_path = case_bundle / "manifest.json"
                        case_manifest = json.loads(case_manifest_path.read_text(encoding="utf-8"))
                        target = case_bundle / artifact["path"]
                        if mode == "omitted":
                            producer = next(
                                item
                                for item in case_manifest["producers"]
                                if item["name"] == producer_name
                            )
                            producer["artifacts"] = [
                                item
                                for item in producer["artifacts"]
                                if item["key"] != artifact["key"]
                            ]
                            case_manifest_path.write_text(
                                json.dumps(case_manifest), encoding="utf-8"
                            )
                        elif mode == "missing":
                            target.unlink()
                        else:
                            target.write_bytes(target.read_bytes() + b"corrupt")
                        case_result = dict(base_result)
                        case_result["bundle"] = str(case_bundle)
                        case_result["manifest"] = str(case_manifest_path)
                        case_result_path = case_root / "result.json"
                        case_result_path.write_text(json.dumps(case_result), encoding="utf-8")
                        summary_path = case_root / "summary.md"
                        summarize = subprocess.run(
                            [
                                sys.executable,
                                "scripts/quality_summary.py",
                                "--result",
                                str(case_result_path),
                                "--output",
                                str(summary_path),
                            ],
                            cwd=REPO_ROOT,
                            capture_output=True,
                            text=True,
                            check=False,
                            timeout=30,
                        )
                        summary = summary_path.read_text(encoding="utf-8")
                        self.assertEqual(summarize.returncode, 1)
                        self.assertIn("NOT VERIFIED", summary)
                        self.assertIn(error_code, summary)

    def test_inventory_failure_is_recorded_without_stopping_later_producers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "quality"
            commands = root / "bin"
            commands.mkdir()
            self._write_full_quality_tools(commands)
            git_probe = root / "git-probe.jsonl"
            git = commands / "git"
            git.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['GIT_PROBE'], 'a', encoding='utf-8') as output:\n"
                "    output.write(json.dumps(args) + '\\n')\n"
                "if args[:2] == ['rev-parse', 'HEAD']:\n"
                "    print('7' * 40)\n"
                "elif args[:1] == ['status']:\n"
                "    raise SystemExit(0)\n"
                "elif args[:1] == ['diff']:\n"
                "    raise SystemExit(0)\n"
                "elif args[:2] == ['ls-files', '-z'] and '--cached' not in args:\n"
                "    raise SystemExit(0)\n"
                "elif args[:2] == ['ls-files', '-z']:\n"
                "    raise SystemExit(29)\n"
                "else:\n"
                "    raise SystemExit(19)\n",
                encoding="utf-8",
            )
            git.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"
            environment["GIT_PROBE"] = str(git_probe)

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_bundle.py",
                    "run",
                    "--root",
                    str(evidence_root),
                    "--all",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            git_calls = git_probe.read_text(encoding="utf-8") if git_probe.is_file() else "none"
            self.assertTrue((evidence_root / "runs").is_dir(), f"{result.stderr}\n{git_calls}")
            run = next((evidence_root / "runs").iterdir())
            manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            producers = {producer["name"]: producer for producer in manifest["producers"]}

        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            set(producers),
            {"lint", "coverage", "stability", "security", "build", "profiling"},
        )
        self.assertEqual(producers["security"]["verification"], "not_verified")
        self.assertEqual(
            producers["security"]["commands"][0]["error_code"],
            "quality_inventory_unavailable",
        )
        self.assertEqual(producers["build"]["verification"], "verified")
        self.assertEqual(producers["profiling"]["verification"], "verified")

    def test_build_metrics_publishes_one_run_scoped_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "quality"
            commands = root / "bin"
            commands.mkdir()
            self._write_clean_git(commands)
            environment = os.environ.copy()
            environment["QUALITY_EVIDENCE_ROOT"] = str(evidence_root)
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"

            result = subprocess.run(
                ["just", "build-metrics"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=120,
            )

            runs = list((evidence_root / "runs").iterdir())
            manifest = json.loads((runs[0] / "manifest.json").read_text(encoding="utf-8"))
            build = manifest["producers"][0]
            artifact = next(item for item in build["artifacts"] if item["key"] == "build")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(runs), 1)
        self.assertEqual(manifest["status"], "completed")
        self.assertEqual(build["name"], "build")
        self.assertEqual(build["verification"], "verified")
        self.assertEqual(len(artifact["sha256"]), 64)
        self.assertFalse((evidence_root / "build.json").exists())

    def test_coverage_publishes_reports_inside_its_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "quality"
            commands = root / "bin"
            commands.mkdir()
            uv = commands / "uv"
            uv.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import pathlib\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if '--version' in args:\n"
                "    print('pytest 9.1.1')\n"
                "    raise SystemExit(0)\n"
                "for argument in args:\n"
                "    if argument.startswith('--cov-report=json:'):\n"
                "        path = pathlib.Path(argument.split('json:', 1)[1])\n"
                "        path.write_text(json.dumps({'totals': {'percent_covered': 91.0}}))\n"
                "    if argument.startswith('--json-report-file='):\n"
                "        path = pathlib.Path(argument.split('=', 1)[1])\n"
                "        path.write_text(json.dumps({'tests': [{'nodeid': 'fixture', "
                "'outcome': 'passed'}]}))\n"
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            uv.chmod(0o755)
            self._write_clean_git(commands)
            environment = os.environ.copy()
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_bundle.py",
                    "run",
                    "--root",
                    str(evidence_root),
                    "--producer",
                    "coverage",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            runs = list((evidence_root / "runs").iterdir())
            manifest = json.loads((runs[0] / "manifest.json").read_text(encoding="utf-8"))
            coverage = manifest["producers"][0]
            keys = {artifact["key"] for artifact in coverage["artifacts"]}

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(keys, {"coverage", "result", "tests"})
        self.assertEqual(coverage["verification"], "verified")
        self.assertFalse((evidence_root / "coverage.json").exists())
        self.assertFalse((evidence_root / "tests.json").exists())

    def test_focused_test_command_publishes_its_report(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "quality"
            commands = root / "bin"
            commands.mkdir()
            self._write_full_quality_tools(commands)
            environment = os.environ.copy()
            environment["PATH"] = f"{commands}{os.pathsep}{environment['PATH']}"

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_bundle.py",
                    "run",
                    "--root",
                    str(evidence_root),
                    "--producer",
                    "test",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            run = next((evidence_root / "runs").iterdir())
            manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            producer = manifest["producers"][0]

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(producer["name"], "test")
        self.assertEqual(
            {artifact["key"] for artifact in producer["artifacts"]},
            {"result", "tests"},
        )

    def test_security_skips_editable_package_and_propagates_pip_audit_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            commands = root / "bin"
            commands.mkdir()
            probe = root / "uv-probe.jsonl"
            uv = commands / "uv"
            uv.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "with open(os.environ['QUALITY_PROBE'], 'a', encoding='utf-8') as output:\n"
                "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "if '--version' in sys.argv:\n"
                "    print('fixture 1.0')\n"
                "    raise SystemExit(0)\n"
                "if sys.argv[1:3] == ['run', 'pip-audit']:\n"
                "    raise SystemExit(int(os.environ['PIP_AUDIT_EXIT']))\n",
                encoding="utf-8",
            )
            uv.chmod(0o755)
            npm = commands / "npm"
            npm.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            npm.chmod(0o755)
            self._write_clean_git(commands)

            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                    "PIP_AUDIT_EXIT": "37",
                    "QUALITY_PROBE": str(probe),
                }
            )
            result = subprocess.run(
                ["just", "security"],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            invocations = [
                json.loads(line) for line in probe.read_text(encoding="utf-8").splitlines()
            ]
            pip_audit_invocations = [
                arguments
                for arguments in invocations
                if arguments[0:2] == ["run", "pip-audit"] and "--version" not in arguments
            ]

        self.assertEqual(len(pip_audit_invocations), 1)
        self.assertEqual(result.returncode, 37, result.stderr)
        self.assertIn("--local", pip_audit_invocations[0])
        self.assertIn("--skip-editable", pip_audit_invocations[0])

    def test_security_collects_every_scanner_after_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            evidence_root = root / "quality"
            commands = root / "bin"
            commands.mkdir()
            uv_probe = root / "uv-probe.jsonl"
            npm_probe = root / "npm-probe.jsonl"
            uv = commands / "uv"
            uv.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import pathlib\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['UV_PROBE'], 'a', encoding='utf-8') as output:\n"
                "    output.write(json.dumps(args) + '\\n')\n"
                "if '--version' in args:\n"
                "    print('fixture 1.0')\n"
                "    raise SystemExit(0)\n"
                "if args[1:2] == ['bandit']:\n"
                "    path = pathlib.Path(args[args.index('-o') + 1])\n"
                "    path.write_text(json.dumps({'errors': [], 'generated_at': 'now', "
                "'metrics': {'_totals': {}}, 'results': []}))\n"
                "    raise SystemExit(23)\n"
                "if args[1:2] == ['pip-audit']:\n"
                "    path = pathlib.Path(args[args.index('--output') + 1])\n"
                "    path.write_text(json.dumps({'dependencies': [{'name': 'fixture', "
                "'version': '1', 'vulns': []}], 'fixes': []}))\n"
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            uv.chmod(0o755)
            npm = commands / "npm"
            npm.write_text(
                "#!/usr/bin/env python3\n"
                "import json\n"
                "import os\n"
                "import sys\n"
                "args = sys.argv[1:]\n"
                "with open(os.environ['NPM_PROBE'], 'a', encoding='utf-8') as output:\n"
                "    output.write(json.dumps(args) + '\\n')\n"
                "if '--version' in args:\n"
                "    print('12.0.2')\n"
                "else:\n"
                "    print(json.dumps({'auditReportVersion': 2, "
                "'metadata': {'vulnerabilities': {'total': 0}}, 'vulnerabilities': {}}))\n"
                "raise SystemExit(0)\n",
                encoding="utf-8",
            )
            npm.chmod(0o755)
            self._write_clean_git(commands)
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                    "NPM_PROBE": str(npm_probe),
                    "UV_PROBE": str(uv_probe),
                }
            )

            result = subprocess.run(
                [
                    sys.executable,
                    "scripts/quality_bundle.py",
                    "run",
                    "--root",
                    str(evidence_root),
                    "--producer",
                    "security",
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

            run = next((evidence_root / "runs").iterdir())
            manifest = json.loads((run / "manifest.json").read_text(encoding="utf-8"))
            security = manifest["producers"][0]
            keys = {artifact["key"] for artifact in security["artifacts"]}
            uv_calls = [json.loads(line) for line in uv_probe.read_text().splitlines()]
            npm_calls = [json.loads(line) for line in npm_probe.read_text().splitlines()]

        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertEqual(len(security["commands"]), 5)
        self.assertEqual(security["verification"], "not_verified")
        self.assertEqual(
            keys,
            {"bandit", "npm-audit-manager", "npm-audit-root", "pip-audit", "result"},
        )
        self.assertTrue(any(call[1:2] == ["pip-audit"] for call in uv_calls))
        self.assertEqual(sum(call[:1] == ["audit"] for call in npm_calls), 2)
        self.assertTrue(all(not artifact["verified"] for artifact in security["artifacts"]))

    def test_profile_tests_propagates_pytest_failure(self) -> None:
        environment = os.environ.copy()
        environment["PYTEST_ADDOPTS"] = "--definitely-invalid"

        with tempfile.TemporaryDirectory() as temporary:
            result = subprocess.run(
                [
                    "uv",
                    "run",
                    "python",
                    "scripts/profile_tests.py",
                    "--output",
                    str(Path(temporary) / "tests.pstats"),
                ],
                cwd=REPO_ROOT,
                capture_output=True,
                text=True,
                check=False,
                env=environment,
                timeout=30,
            )

        self.assertNotEqual(result.returncode, 0)

    @staticmethod
    def _write_full_quality_tools(commands: Path) -> None:
        uv = commands / "uv"
        uv.write_text(
            "#!/usr/bin/env python3\n"
            "import cProfile\n"
            "import json\n"
            "import pathlib\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if '--version' in args:\n"
            "    print('fixture 1.0')\n"
            "    raise SystemExit(0)\n"
            "for argument in args:\n"
            "    if argument.startswith('--cov-report=json:'):\n"
            "        pathlib.Path(argument.split('json:', 1)[1]).write_text("
            "json.dumps({'totals': {'percent_covered': 91.0}}))\n"
            "    if argument.startswith('--json-report-file='):\n"
            "        pathlib.Path(argument.split('=', 1)[1]).write_text("
            "json.dumps({'tests': [{'nodeid': 'fixture', 'outcome': 'passed'}]}))\n"
            "if args[1:2] == ['bandit']:\n"
            "    pathlib.Path(args[args.index('-o') + 1]).write_text(json.dumps("
            "{'errors': [], 'generated_at': 'now', 'metrics': {'_totals': {}}, "
            "'results': []}))\n"
            "if args[1:2] == ['pip-audit']:\n"
            "    pathlib.Path(args[args.index('--output') + 1]).write_text(json.dumps("
            "{'dependencies': [{'name': 'fixture', 'version': '1', 'vulns': []}], "
            "'fixes': []}))\n"
            "if args[1:3] == ['python', 'scripts/test_stability.py']:\n"
            "    pathlib.Path(args[args.index('--output') + 1]).write_text(json.dumps("
            "{'executions': [{'exit_code': 0}, {'exit_code': 0}, {'exit_code': 0}], "
            "'runs': 3, 'status': 'passed', 'unstable': []}))\n"
            "if args[1:3] == ['python', 'scripts/profile_tests.py']:\n"
            "    output = pathlib.Path(args[args.index('--output') + 1])\n"
            "    profiler = cProfile.Profile()\n"
            "    profiler.runcall(sum, range(10))\n"
            "    profiler.dump_stats(str(output))\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        uv.chmod(0o755)
        npm = commands / "npm"
        npm.write_text(
            "#!/usr/bin/env python3\n"
            "import json\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if '--version' in args:\n"
            "    print('12.0.2')\n"
            "elif args[:1] == ['pack']:\n"
            "    print(json.dumps([{'files': [{'path': 'package.json'}], "
            "'size': 10, 'unpackedSize': 20}]))\n"
            "else:\n"
            "    print(json.dumps({'auditReportVersion': 2, "
            "'metadata': {'vulnerabilities': {'total': 0}}, 'vulnerabilities': {}}))\n"
            "raise SystemExit(0)\n",
            encoding="utf-8",
        )
        npm.chmod(0o755)

        QualityCommandTests._write_clean_git(commands)

    @staticmethod
    def _write_clean_git(commands: Path) -> None:
        git = commands / "git"
        git.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "args = sys.argv[1:]\n"
            "if args[:2] == ['rev-parse', 'HEAD']:\n"
            "    print('7' * 40)\n"
            "elif args[:1] == ['status']:\n"
            "    raise SystemExit(0)\n"
            "elif args[:1] == ['diff']:\n"
            "    raise SystemExit(0)\n"
            "elif args[:2] == ['ls-files', '-z']:\n"
            "    sys.stdout.buffer.write(b'pyproject.toml\\0')\n"
            "else:\n"
            "    raise SystemExit(19)\n",
            encoding="utf-8",
        )
        git.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
