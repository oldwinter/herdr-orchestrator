from __future__ import annotations

import unittest
from pathlib import Path

from herdr_orchestrator.herdr import replica_slot_names, stable_agent_name
from herdr_orchestrator.model import Harness


class HerdrNamingTests(unittest.TestCase):
    def test_stable_name_is_deterministic(self) -> None:
        workspace = Path("/tmp/project")
        self.assertEqual(
            stable_agent_name("example", workspace, Harness.HERMES),
            stable_agent_name("example", workspace, Harness.HERMES),
        )
        self.assertRegex(
            stable_agent_name("example", workspace, Harness.HERMES),
            r"^ho-hermes-[a-f0-9]{8}$",
        )

    def test_replica_slot_names_keep_single_stable_name(self) -> None:
        workspace = Path("/tmp/project")
        self.assertEqual(
            replica_slot_names("example", workspace, Harness.GROK, 1),
            (stable_agent_name("example", workspace, Harness.GROK),),
        )
        names = replica_slot_names("example", workspace, Harness.GROK, 10)
        self.assertEqual(len(names), 10)
        self.assertEqual(len(set(names)), 10)
        self.assertTrue(all(len(name) <= 32 for name in names))
        self.assertRegex(names[0], r"^ho-grok-01-[a-f0-9]{6}$")
        self.assertRegex(names[9], r"^ho-grok-10-[a-f0-9]{6}$")


if __name__ == "__main__":
    unittest.main()
