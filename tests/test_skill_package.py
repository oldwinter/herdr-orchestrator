from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILLS = REPO_ROOT / ".agents/skills"


class StandardizedDeliverySkillTests(unittest.TestCase):
    def test_portable_skill_uses_the_npm_runtime(self) -> None:
        skill = (REPO_ROOT / "skills/herdr-orchestrator/SKILL.md").read_text(
            encoding="utf-8"
        )
        frontmatter = skill.split("---", 2)[1]

        self.assertIn("name: herdr-orchestrator", frontmatter)
        self.assertIn("npx --yes herdr-orchestrator install --project .", skill)
        self.assertIn("npx --yes herdr-orchestrator doctor --project .", skill)
        self.assertNotIn("PYTHONPATH=src", skill)
        self.assertNotIn("workflows/multi-harness.toml", skill)

    def test_canonical_skill_has_only_exact_opt_in_keyword_triggers(self) -> None:
        skill = (SKILLS / "standardized-delivery/SKILL.md").read_text(
            encoding="utf-8"
        )
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
        canonical = (SKILLS / "standardized-delivery/SKILL.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("references/workflow-contract.md", canonical)
        self.assertIn("references/authority.md", canonical)
        self.assertIn("references/recovery.md", canonical)
        self.assertTrue(
            (SKILLS / "standardized-delivery/references/authority.md").is_file()
        )
        self.assertTrue(
            (SKILLS / "standardized-delivery/references/recovery.md").is_file()
        )

    def test_cross_harness_skill_paths_share_the_canonical_tree(self) -> None:
        for link in (REPO_ROOT / ".agent/skills", REPO_ROOT / ".claude/skills"):
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), SKILLS.resolve())


if __name__ == "__main__":
    unittest.main()
