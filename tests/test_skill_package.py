from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import re
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

from herdr_orchestrator.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS = REPO_ROOT / ".agents/skills"


def _load_script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, REPO_ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


CHECK_DOCS = _load_script("check_docs")
CHECK_FEATURE_FLAGS = _load_script("check_feature_flags")
CHECK_REPOSITORY = _load_script("check_repository")
GENERATE_REFERENCE = _load_script("generate_reference")


class StandardizedDeliverySkillTests(unittest.TestCase):
    def test_portable_skill_uses_the_npm_runtime(self) -> None:
        skill = (REPO_ROOT / "skills/herdr-orchestrator/SKILL.md").read_text(encoding="utf-8")
        frontmatter = skill.split("---", 2)[1]

        self.assertIn("name: herdr-orchestrator", frontmatter)
        self.assertIn("npx --yes herdr-orchestrator install --project .", skill)
        self.assertIn("npx --yes herdr-orchestrator doctor --project .", skill)
        self.assertIn("herdr-manager", skill)
        self.assertNotIn("PYTHONPATH=src", skill)
        self.assertNotIn("workflows/multi-harness.toml", skill)

    def test_portable_skill_covers_the_full_queue_operating_contract(self) -> None:
        skill = (REPO_ROOT / "skills/herdr-orchestrator/SKILL.md").read_text(encoding="utf-8")

        for required in (
            "--harness auto",
            "--placement pane",
            "--placement tab",
            "--placement worktree",
            "--controller-harness",
            "--worker-harness",
            "--dedupe-key",
            "--receipt-prefix",
            "--receipt-file",
            "--until-idle",
            "retry --project .",
            "gc --project . --succeeded-agents",
            "http://127.0.0.1:8765",
            "seed_jobs",
            "idle",
            "done",
            "cursor",
            "--auto high",
            "--always-approve",
            "--dangerously-bypass-approvals-and-sandbox",
            "--approve",
            "--dangerously-skip-permissions",
            "--yolo",
            "Quick safety check:",
        ):
            self.assertIn(required, skill)

    def test_portable_skill_runtime_examples_parse_against_the_cli_contract(self) -> None:
        skill = (REPO_ROOT / "skills/herdr-orchestrator/SKILL.md").read_text(encoding="utf-8")
        commands: list[list[str]] = []
        for block in re.findall(r"```bash\n(.*?)```", skill, flags=re.DOTALL):
            normalized = block.replace("\\\n", " ")
            for line in normalized.splitlines():
                if line.startswith("npx --yes herdr-orchestrator "):
                    commands.append(shlex.split(line)[3:])

        runtime_commands = {
            "catalog",
            "dashboard",
            "doctor",
            "enqueue",
            "gc",
            "retry",
            "run",
            "status",
        }
        self.assertTrue(runtime_commands.issubset({command[0] for command in commands}))
        parser = build_parser()
        for command in commands:
            if command[0] == "install":
                self.assertEqual(command[1:3], ["--project", "."])
                continue
            if command[0] == "manager":
                self.assertEqual(
                    command,
                    ["manager", "claude"],
                )
                continue
            arguments = list(command)
            project_index = arguments.index("--project")
            arguments[project_index : project_index + 2] = [
                "--workflow",
                "installed-workflow.toml",
            ]
            parsed = parser.parse_args(arguments)
            self.assertEqual(parsed.command, command[0])

    def test_canonical_skill_has_only_exact_opt_in_keyword_triggers(self) -> None:
        skill = (SKILLS / "standardized-delivery/SKILL.md").read_text(encoding="utf-8")
        frontmatter = skill.split("---", 2)[1]

        for trigger in (
            "标准化交付",
            "完整工程流程",
            "Matt workflow",
            "Pocock workflow",
            "Wayfinder 全流程",
            "自主交付",
        ):
            self.assertIn(trigger, frontmatter)
        self.assertIn("Ordinary coding requests never trigger it.", frontmatter)
        self.assertNotIn("disable-model-invocation: true", frontmatter)

    def test_explicit_aliases_point_to_one_canonical_skill(self) -> None:
        for alias in ("matt-workflow", "wayfinder-delivery"):
            skill = (SKILLS / alias / "SKILL.md").read_text(encoding="utf-8")
            self.assertIn("disable-model-invocation: true", skill)
            self.assertIn("../standardized-delivery/SKILL.md", skill)

    def test_progressive_references_cover_authority_and_recovery_branches(self) -> None:
        canonical = (SKILLS / "standardized-delivery/SKILL.md").read_text(encoding="utf-8")

        self.assertIn("references/workflow-contract.md", canonical)
        self.assertIn("references/authority.md", canonical)
        self.assertIn("references/recovery.md", canonical)
        self.assertTrue((SKILLS / "standardized-delivery/references/authority.md").is_file())
        self.assertTrue((SKILLS / "standardized-delivery/references/recovery.md").is_file())

    def test_cross_harness_skill_paths_share_the_canonical_tree(self) -> None:
        for link in (REPO_ROOT / ".agent/skills", REPO_ROOT / ".claude/skills"):
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), SKILLS.resolve())


class RepositoryCheckerTests(unittest.TestCase):
    def test_repository_checker_rejects_each_untracked_debt_marker(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.py"
            source.write_text(
                "# " + "TO" + "DO missing owner; TODO(#1 owner=alice): tracked\n",
                encoding="utf-8",
            )

            failures = CHECK_REPOSITORY.repository_failures(root, (source,))

        self.assertEqual(
            failures,
            [
                "sample.py:1: debt marker needs issue and owner, "
                "for example TODO(#123 owner=name):"
            ],
        )

    def test_repository_checker_counts_a_trailing_newline_once(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "sample.py"
            source.write_text("value = 1\n", encoding="utf-8")
            with patch.object(CHECK_REPOSITORY, "MAX_SOURCE_LINES", 1):
                failures = CHECK_REPOSITORY.repository_failures(root, (source,))

        self.assertEqual(failures, [])

    def test_docs_checker_requires_every_key_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "justfile").write_text("check:\n\t@true\n", encoding="utf-8")
            (root / "README.md").write_text("", encoding="utf-8")
            (root / "AGENTS.md").write_text("", encoding="utf-8")

            failures = CHECK_DOCS.documentation_failures(root)

        self.assertEqual(failures, ["CONTRIBUTING.md: required document is missing"])

    def test_docs_checker_rejects_links_outside_the_repository(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            root.mkdir()
            (base / "outside.md").write_text("outside\n", encoding="utf-8")
            (root / "justfile").write_text("check:\n\t@true\n", encoding="utf-8")
            (root / "README.md").write_text("[outside](../outside.md)\n", encoding="utf-8")
            (root / "AGENTS.md").write_text("", encoding="utf-8")
            (root / "CONTRIBUTING.md").write_text("", encoding="utf-8")

            failures = CHECK_DOCS.documentation_failures(root)

        self.assertEqual(failures, ["README.md: local link escapes repository ../outside.md"])

    def test_docs_checker_rejects_a_symlinked_key_document(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            root = base / "repo"
            root.mkdir()
            (base / "outside.md").write_text("outside\n", encoding="utf-8")
            (root / "justfile").write_text("check:\n\t@true\n", encoding="utf-8")
            (root / "README.md").symlink_to(base / "outside.md")
            (root / "AGENTS.md").write_text("", encoding="utf-8")
            (root / "CONTRIBUTING.md").write_text("", encoding="utf-8")

            failures = CHECK_DOCS.documentation_failures(root)

        self.assertEqual(failures, ["README.md: required document must be a regular file"])

    def test_docs_checker_rejects_missing_paths_in_command_examples(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "justfile").write_text(
                "check:\n\t@true\nenqueue:\n\t@true\nrun:\n\t@true\n",
                encoding="utf-8",
            )
            for name in ("README.md", "AGENTS.md", "CONTRIBUTING.md"):
                (root / name).write_text("", encoding="utf-8")
            docs = root / "docs"
            docs.mkdir()
            (docs / "architecture.md").write_text("", encoding="utf-8")
            (docs / "runtime-troubleshooting.md").write_text(
                "```bash\njust run --workflow workflows/missing-runtime.toml\n```\n",
                encoding="utf-8",
            )
            (docs / "workflow-schema.md").write_text("", encoding="utf-8")
            (root / "README.md").write_text(
                "```bash\n"
                "just enqueue codex review workflows/prompts/missing-review.md review-v1\n"
                "```\n",
                encoding="utf-8",
            )

            failures = CHECK_DOCS.documentation_failures(root)

        self.assertIn(
            "README.md: missing command path workflows/prompts/missing-review.md",
            failures,
        )
        self.assertIn(
            "runtime-troubleshooting.md: missing command path workflows/missing-runtime.toml",
            failures,
        )

    def test_documented_workflows_are_all_tracked_examples(self) -> None:
        workflows = sorted(
            path.relative_to(REPO_ROOT).as_posix()
            for path in (REPO_ROOT / "workflows").glob("*.toml")
        )
        documentation = "\n".join(
            (REPO_ROOT / name).read_text(encoding="utf-8") for name in ("README.md", "AGENTS.md")
        )

        self.assertGreater(len(workflows), 1)
        for workflow in workflows:
            self.assertIn(workflow, documentation)

    def test_reference_generator_runs_without_installed_package_or_pythonpath(self) -> None:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        environment["PYTHONNOUSERSITE"] = "1"
        result = subprocess.run(
            [
                sys.executable,
                "-S",
                str(REPO_ROOT / "scripts/generate_reference.py"),
                "--check",
            ],
            cwd=REPO_ROOT,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), "generated CLI reference: current")

    def test_generated_reference_describes_required_gc_scope(self) -> None:
        rendered = GENERATE_REFERENCE.render()

        for option in ("--succeeded-agents", "--failed-agents"):
            self.assertIn(
                f"| `{option}` | one of `--succeeded-agents`, `--failed-agents` |",
                rendered,
            )

    def test_feature_flag_checker_requires_executable_references_and_assignments(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "src" / "herdr_orchestrator"
            tests = root / "tests"
            docs = root / "docs"
            source.mkdir(parents=True)
            tests.mkdir()
            docs.mkdir()
            names = ("SENTRY_EXPORT", "POSTHOG_ANALYTICS", "WEBHOOK_ALERTS")
            values = ("sentry_export", "posthog_analytics", "webhook_alerts")
            variables = (
                "HERDR_FEATURE_SENTRY_EXPORT",
                "HERDR_FEATURE_POSTHOG_ANALYTICS",
                "HERDR_FEATURE_WEBHOOK_ALERTS",
            )
            (source / "consumer.py").write_text(
                "from fake_feature_flags import FeatureFlag\n"
                "FAKE = FeatureFlag.SENTRY_EXPORT\n"
                + "\n".join(f"# FeatureFlag.{name}" for name in names),
                encoding="utf-8",
            )
            (tests / "test_flags.py").write_text(
                "\n".join(f"MENTION_{name} = '{name}'" for name in names),
                encoding="utf-8",
            )
            (docs / "feature-flags.md").write_text(
                "\n".join(f"The `{value}` flag still exists." for value in values),
                encoding="utf-8",
            )
            (root / ".env.example").write_text(
                f"# {' '.join(variables)}\n",
                encoding="utf-8",
            )

            failures = CHECK_FEATURE_FLAGS.policy_failures(root)

        for value, variable in zip(values, variables, strict=True):
            self.assertIn(f"{value}: no production consumer", failures)
            self.assertIn(f"{value}: missing lifecycle row", failures)
            self.assertIn(f"{value}: {variable} missing from .env.example", failures)
            self.assertIn(f"{value}: no test reference", failures)

    def test_generated_reference_check_rejects_missing_and_stale_output(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            output = Path(temporary) / "cli.md"
            stdout = io.StringIO()
            with (
                patch.object(GENERATE_REFERENCE, "OUTPUT", output),
                patch.object(sys, "argv", ["generate_reference.py", "--check"]),
                contextlib.redirect_stdout(stdout),
            ):
                self.assertEqual(GENERATE_REFERENCE.main(), 1)
                output.write_text("stale\n", encoding="utf-8")
                self.assertEqual(GENERATE_REFERENCE.main(), 1)

        self.assertEqual(
            stdout.getvalue().splitlines(),
            [
                "generated CLI reference is stale; run just docs-generate",
                "generated CLI reference is stale; run just docs-generate",
            ],
        )


if __name__ == "__main__":
    unittest.main()
