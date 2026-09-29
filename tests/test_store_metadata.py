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

    def test_metadata_float_round_trips_finite_values(self) -> None:
        self.store.set_metadata_float("example", 1.25)

        self.assertEqual(self.store.metadata_float("example"), 1.25)

    def test_metadata_float_rejects_non_finite_writes(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(StoreError, "metadata_invalid_float: example"),
            ):
                self.store.set_metadata_float("example", value)

    def test_metadata_float_rejects_non_finite_stored_values(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value):
                with self.store._connect() as connection:
                    connection.execute(
                        """
                        INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET value = excluded.value
                        """,
                        ("example", value, 0.0),
                    )

                with self.assertRaisesRegex(StoreError, "metadata_invalid_float: example"):
                    self.store.metadata_float("example")

    def test_reserve_planner_run_rejects_non_finite_observation_time(self) -> None:
        with self.assertRaisesRegex(
            StoreError,
            "metadata_invalid_float: planner_last_attempt:example",
        ):
            self.store.reserve_planner_run("example", 60, now=float("nan"))

    def test_reserve_planner_run_rejects_non_finite_stored_time(self) -> None:
        key = "planner_last_attempt:example"
        with self.store._connect() as connection:
            connection.execute(
                "INSERT INTO metadata(key, value, updated_at) VALUES (?, ?, ?)",
                (key, "inf", 0.0),
            )

        with self.assertRaisesRegex(StoreError, f"metadata_invalid_float: {key}"):
            self.store.reserve_planner_run("example", 60, now=1.0)


if __name__ == "__main__":
    unittest.main()
