from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from herdr_orchestrator.store import Store, StoreError


class StoreMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temporary.name) / "state.db")
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def _write_metadata(self, key: str, value: str) -> None:
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)",
                (key, value, 0.0),
            )

    def test_metadata_float_returns_none_for_missing_key(self) -> None:
        self.assertIsNone(self.store.metadata_float("absent"))

    def test_metadata_float_round_trips_and_overwrites(self) -> None:
        self.store.set_metadata_float("drift_seconds", 1.5)
        self.assertEqual(self.store.metadata_float("drift_seconds"), 1.5)

        self.store.set_metadata_float("drift_seconds", 2.25)

        self.assertEqual(self.store.metadata_float("drift_seconds"), 2.25)

    def test_metadata_float_rejects_non_numeric_value(self) -> None:
        self._write_metadata("corrupt", "not-a-float")

        with self.assertRaisesRegex(StoreError, "metadata_invalid_float: corrupt"):
            self.store.metadata_float("corrupt")

    def test_reserve_planner_run_allows_first_call_then_blocks_within_interval(self) -> None:
        self.assertTrue(self.store.reserve_planner_run("wf", 60, now=1000.0))
        self.assertFalse(self.store.reserve_planner_run("wf", 60, now=1030.0))

    def test_reserve_planner_run_allows_again_at_interval_boundary(self) -> None:
        self.assertTrue(self.store.reserve_planner_run("wf", 60, now=1000.0))
        self.assertTrue(self.store.reserve_planner_run("wf", 60, now=1060.0))

    def test_reserve_planner_run_scopes_per_workflow_and_workspace(self) -> None:
        self.assertTrue(self.store.reserve_planner_run("wf-a", 60, now=1000.0))
        self.assertTrue(self.store.reserve_planner_run("wf-b", 60, now=1000.0))
        self.assertTrue(
            self.store.reserve_planner_run("wf-a", 60, now=1000.0, workspace="ws-1")
        )
        self.assertFalse(
            self.store.reserve_planner_run("wf-a", 60, now=1030.0, workspace="ws-1")
        )
        self.assertTrue(
            self.store.reserve_planner_run("wf-a", 60, now=1030.0, workspace="ws-2")
        )

    def test_reserve_planner_run_rejects_corrupt_reservation(self) -> None:
        self._write_metadata("planner_last_attempt:wf", "corrupt")

        with self.assertRaisesRegex(
            StoreError, "metadata_invalid_float: planner_last_attempt:wf"
        ):
            self.store.reserve_planner_run("wf", 60, now=1000.0)


if __name__ == "__main__":
    unittest.main()
