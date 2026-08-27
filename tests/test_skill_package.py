from __future__ import annotations

import re
import shlex
import unittest
from pathlib import Path

from herdr_orchestrator.cli import build_parser

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS = REPO_ROOT / ".agents/skills"


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
                    ["manager", "grok"],
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


if __name__ == "__main__":
    unittest.main()
