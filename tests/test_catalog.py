from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.catalog import (
    CatalogError,
    execution_prompt,
    full_profile_payload,
    load_harness_profiles,
    profile_for_harness,
    render_compact_catalog,
)
from herdr_orchestrator.model import Harness

REPO_ROOT = Path(__file__).resolve().parents[1]


class HarnessCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profiles = load_harness_profiles(REPO_ROOT / "profiles/harnesses")

    def test_loads_all_supported_harnesses(self) -> None:
        self.assertEqual({profile.harness for profile in self.profiles}, set(Harness))

    def test_compact_catalog_excludes_full_context(self) -> None:
        catalog = render_compact_catalog(self.profiles)

        self.assertIn('"profile_ref": "harness:codex"', catalog)
        self.assertIn("代码实现型 agent", catalog)
        self.assertNotIn("# OpenAI Codex CLI execution profile", catalog)
        self.assertNotIn("## Operating contract", catalog)

    def test_full_profile_loads_context_on_demand(self) -> None:
        codex = profile_for_harness(self.profiles, Harness.CODEX)

        payload = full_profile_payload(codex)

        profile = payload["profile"]
        self.assertIsInstance(profile, dict)
        assert isinstance(profile, dict)
        self.assertIn("# OpenAI Codex CLI execution profile", profile["context"])

    def test_grok_build_profile_is_available(self) -> None:
        grok = profile_for_harness(self.profiles, Harness.GROK)

        payload = full_profile_payload(grok)

        profile = payload["profile"]
        self.assertIsInstance(profile, dict)
        assert isinstance(profile, dict)
        self.assertEqual(profile["display_name"], "Grok Build")
        self.assertIn("# Grok Build execution profile", profile["context"])

    def test_execution_prompt_loads_only_selected_profile(self) -> None:
        droid = profile_for_harness(self.profiles, Harness.DROID)

        prompt = execution_prompt(droid, "Inspect the queue.")

        self.assertIn("# Factory Droid execution profile", prompt)
        self.assertIn("# Task packet\n\nInspect the queue.", prompt)
        self.assertNotIn("# Hermes Agent execution profile", prompt)

    def test_context_is_read_at_dispatch_not_catalog_load(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            context = root / "droid.md"
            context.write_text("first version", encoding="utf-8")
            (root / "droid.toml").write_text(
                """
schema_version = 1
harness = "droid"
display_name = "Droid"
summary = "Summary"
strengths = ["one"]
best_for = ["one"]
avoid_for = ["one"]
traits = ["one"]
context_file = "droid.md"
""",
                encoding="utf-8",
            )
            profiles = load_harness_profiles(root)
            context.write_text("updated after catalog load", encoding="utf-8")

            prompt = execution_prompt(profiles[0], "Task")

        self.assertIn("updated after catalog load", prompt)
        self.assertNotIn("first version", prompt)

    def test_rejects_context_path_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "profile.toml").write_text(
                """
schema_version = 1
harness = "droid"
display_name = "Droid"
summary = "Summary"
strengths = ["one"]
best_for = ["one"]
avoid_for = ["one"]
traits = ["one"]
context_file = "../outside.md"
""",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(CatalogError, "context_path_invalid"):
                load_harness_profiles(root)


if __name__ == "__main__":
    unittest.main()
