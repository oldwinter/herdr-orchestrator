from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which("just"), "just is not installed")
class JustfileTests(unittest.TestCase):
    def test_check_allocates_distinct_json_results_for_repeated_runs(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            commands = root / "commands.jsonl"
            environment = {**os.environ, "PATH": f"{root}{os.pathsep}{os.environ['PATH']}"}
            environment["QUALITY_EVIDENCE_ROOT"] = str(root / "quality root")
            uv = root / "uv"
            uv.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            uv.chmod(0o755)
            python = root / "python3"
            python.write_text(
                f"#!{sys.executable}\n"
                "import json, os, sys\n"
                "if sys.argv[1] == '-c':\n"
                "    os.execv(sys.executable, [sys.executable, *sys.argv[1:]])\n"
                f"with open({str(commands)!r}, 'a') as stream:\n"
                "    stream.write(json.dumps(sys.argv[1:]) + '\\n')\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            for _ in range(2):
                result = subprocess.run(
                    [shutil.which("just"), "check"],
                    cwd=REPO_ROOT,
                    env=environment,
                    capture_output=True,
                    text=True,
                    check=False,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
            invocations = [json.loads(line) for line in commands.read_text().splitlines()]
            results = [Path(argv[argv.index("--result") + 1]) for argv in invocations]
            self.assertEqual(len(results), 6)
            self.assertEqual(results[:3], [results[0]] * 3)
            self.assertEqual(results[3:], [results[3]] * 3)
            self.assertNotEqual(results[0], results[3])
            for path in {results[0], results[3]}:
                self.assertEqual(path.suffix, ".json")
                self.assertTrue(path.is_file())

    def test_bound_arguments_and_extra_flags_preserve_shell_metacharacters(self) -> None:
        prefix = "DONE 'quoted' value; literal"
        workflow = "workflow with spaces.toml"
        cases = (
            (
                "enqueue",
                ["codex", "Task title", "prompt with spaces.md", "task-key"],
                ["--receipt-prefix", prefix],
            ),
            (
                "enqueue-auto",
                ["Task title", "prompt with spaces.md", "task-key"],
                ["--receipt-prefix", prefix],
            ),
            ("retry", ["42"], ["--extra-attempts", "3"]),
            ("deliver", ["goal with spaces.md"], ["--tracker-root", "tracker with spaces"]),
        )
        with tempfile.TemporaryDirectory() as temporary:
            capture = Path(temporary).resolve() / "capture argv.py"
            capture.write_text(
                "import json, sys\nprint(json.dumps(sys.argv[1:]))\n", encoding="utf-8"
            )
            python = f"{shlex.quote(sys.executable)} {shlex.quote(str(capture))}"
            for recipe, bound, extra in cases:
                with self.subTest(recipe=recipe):
                    result = subprocess.run(
                        [
                            "just",
                            "--set",
                            "python",
                            python,
                            "--set",
                            "workflow",
                            workflow,
                            recipe,
                            *bound,
                            *extra,
                        ],
                        cwd=REPO_ROOT,
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    argv = json.loads(result.stdout)
                    self.assertEqual(argv[argv.index("--workflow") + 1], workflow)
                    self.assertEqual(argv[-len(extra) :], extra)
                    for argument in bound:
                        self.assertEqual(argv.count(argument), 1, argv)


if __name__ == "__main__":
    unittest.main()
