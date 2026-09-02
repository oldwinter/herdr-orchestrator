from __future__ import annotations

import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from herdr_orchestrator.model import Harness, NewJob, PlacementTarget
from herdr_orchestrator.store import SCHEMA_VERSION, Store


class StoreHealthCorrectnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.path = Path(self.temporary.name) / "state.db"
        self.store = Store(self.path)
        self.store.initialize()

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_health_write_rejects_older_and_same_generation_observations(self) -> None:
        self.assertTrue(self._write_health(observed_at=100.0, status="ready", expires_at=200.0))
        self.assertTrue(
            self._write_health(observed_at=101.0, status="unavailable", expires_at=None)
        )
        self.assertFalse(self._write_health(observed_at=99.0, status="ready", expires_at=199.0))
        self.assertFalse(self._write_health(observed_at=101.0, status="ready", expires_at=201.0))

        row = self.store.harness_health_rows("workflow", "/workspace")[0]
        self.assertEqual(row["status"], "unavailable")
        self.assertEqual(row["observed_at"], 101.0)
        self.assertEqual(row["revision"], 1)

    def test_late_probe_completion_is_fenced_after_lease_rotation(self) -> None:
        first = self.store.acquire_harness_probe_lease(
            workflow="workflow",
            workspace="/workspace",
            harness=Harness.CODEX,
            owner="old-probe",
            now=100.0,
            lease_seconds=10.0,
        )
        self.assertIsNotNone(first)
        assert first is not None

        second = self.store.acquire_harness_probe_lease(
            workflow="workflow",
            workspace="/workspace",
            harness=Harness.CODEX,
            owner="new-probe",
            now=111.0,
            lease_seconds=10.0,
        )
        self.assertIsNotNone(second)
        assert second is not None
        self.assertGreater(second.revision, first.revision)

        self.assertFalse(
            self._write_health(
                observed_at=112.0,
                status="ready",
                expires_at=200.0,
                expected_revision=first.revision,
                expected_owner=first.owner,
                clear_probe_lease=True,
            )
        )
        row = self.store.harness_health_rows("workflow", "/workspace")[0]
        self.assertEqual(row["probe_owner"], "new-probe")
        self.assertEqual(row["revision"], second.revision)

        self.assertTrue(
            self._write_health(
                observed_at=112.0,
                status="ready",
                expires_at=200.0,
                expected_revision=second.revision,
                expected_owner=second.owner,
                clear_probe_lease=True,
            )
        )
        row = self.store.harness_health_rows("workflow", "/workspace")[0]
        self.assertIsNone(row["probe_owner"])
        self.assertEqual(row["revision"], second.revision + 1)

    def test_pending_harnesses_workspace_scope_excludes_null_legacy_rows(self) -> None:
        self.store.enqueue(
            NewJob(
                workflow="workflow",
                workspace="/workspace-a",
                title="a",
                harness=Harness.CODEX,
                prompt="a",
                dedupe_key="a",
                max_attempts=1,
            )
        )
        self.store.enqueue(
            NewJob(
                workflow="workflow",
                workspace="/workspace-b",
                title="b",
                harness=Harness.DROID,
                prompt="b",
                dedupe_key="b",
                max_attempts=1,
            )
        )
        self.store.enqueue(
            NewJob(
                workflow="workflow",
                title="legacy",
                harness=Harness.GROK,
                prompt="legacy",
                dedupe_key="legacy",
                max_attempts=1,
            )
        )

        self.assertEqual(
            self.store.pending_harnesses("workflow", workspace="/workspace-a"),
            (Harness.CODEX,),
        )
        self.assertEqual(
            self.store.pending_harnesses("workflow", workspace="/workspace-b"),
            (Harness.DROID,),
        )
        self.assertEqual(
            self.store.pending_harnesses("workflow"),
            (Harness.CODEX, Harness.DROID, Harness.GROK),
        )

    def test_migrate_legacy_workspace_binds_only_unscoped_jobs(self) -> None:
        self.store.enqueue(
            NewJob(
                workflow="workflow",
                workspace="/workspace-a",
                title="scoped",
                harness=Harness.CODEX,
                prompt="scoped",
                dedupe_key="scoped",
                max_attempts=1,
            )
        )
        self.store.enqueue(
            NewJob(
                workflow="workflow",
                title="legacy",
                harness=Harness.GROK,
                prompt="legacy",
                dedupe_key="legacy",
                max_attempts=1,
            )
        )
        self.store.enqueue(
            NewJob(
                workflow="other",
                title="foreign",
                harness=Harness.DROID,
                prompt="foreign",
                dedupe_key="foreign",
                max_attempts=1,
            )
        )

        migrated = self.store.migrate_legacy_workspace("workflow", "/workspace-a")

        self.assertEqual(migrated, 1)
        jobs = {str(job["title"]): job["workspace"] for job in self.store.jobs("workflow")}
        self.assertEqual(jobs["scoped"], "/workspace-a")
        self.assertEqual(jobs["legacy"], "/workspace-a")
        self.assertIsNone(self.store.jobs("other")[0]["workspace"])
        self.assertEqual(
            self.store.pending_harnesses("workflow", workspace="/workspace-a"),
            (Harness.CODEX, Harness.GROK),
        )
        self.assertEqual(self.store.migrate_legacy_workspace("workflow", "/workspace-a"), 0)

    def test_claim_rechecks_fresh_health_without_incrementing_attempts(self) -> None:
        with patch("herdr_orchestrator.store.time.time", return_value=100.0):
            job_id, _ = self.store.enqueue(
                NewJob(
                    workflow="workflow",
                    workspace="/workspace",
                    title="fresh claim",
                    harness=Harness.CODEX,
                    prompt="claim",
                    dedupe_key="fresh-claim",
                    max_attempts=2,
                )
            )
        self.assertTrue(self._write_health(observed_at=100.0, status="ready", expires_at=110.0))

        with patch("herdr_orchestrator.store.time.time", return_value=111.0):
            self.assertEqual(
                self.store.claim(
                    "workflow",
                    limit=1,
                    lease_seconds=30,
                    allowed_harnesses=(Harness.CODEX,),
                    workspace="/workspace",
                    require_fresh_health=True,
                ),
                [],
            )
        self.assertEqual(self.store.jobs("workflow")[0]["id"], job_id)
        self.assertEqual(self.store.jobs("workflow")[0]["attempts"], 0)

        self.assertTrue(self._write_health(observed_at=112.0, status="ready", expires_at=120.0))
        with patch("herdr_orchestrator.store.time.time", return_value=113.0):
            claimed = self.store.claim(
                "workflow",
                limit=1,
                lease_seconds=30,
                allowed_harnesses=(Harness.CODEX,),
                workspace="/workspace",
                require_fresh_health=True,
            )
        self.assertEqual(len(claimed), 1)
        self.assertEqual(self.store.jobs("workflow")[0]["attempts"], 1)

    def test_claim_status_and_unplaced_queries_are_workspace_scoped(self) -> None:
        self.store.enqueue(
            NewJob(
                workflow="workflow",
                workspace="/workspace-a",
                title="a",
                harness=Harness.CODEX,
                prompt="a",
                dedupe_key="claim-a",
                max_attempts=1,
                placement=None,
            )
        )
        self.store.enqueue(
            NewJob(
                workflow="workflow",
                workspace="/workspace-b",
                title="b",
                harness=Harness.CODEX,
                prompt="b",
                dedupe_key="claim-b",
                max_attempts=1,
                placement=None,
            )
        )
        self.assertEqual(
            len(
                self.store.unplaced_jobs(
                    "workflow",
                    workspace="/workspace-a",
                )
            ),
            1,
        )
        self.assertEqual(
            self.store.status_counts("workflow", workspace="/workspace-a")["pending"],
            1,
        )

        self.store.set_placement(1, PlacementTarget.TAB)
        claimed = self.store.claim(
            "workflow",
            limit=1,
            lease_seconds=30,
            workspace="/workspace-a",
        )

        self.assertEqual(len(claimed), 1)
        self.assertEqual(claimed[0].job_id, 1)
        self.assertEqual(
            self.store.status_counts("workflow", workspace="/workspace-b")["pending"],
            1,
        )

    def test_claim_rechecks_static_validator_inside_transaction(self) -> None:
        job_id, _ = self.store.enqueue(
            NewJob(
                workflow="workflow",
                workspace="/workspace",
                title="static race",
                harness=Harness.CODEX,
                prompt="task",
                dedupe_key="static-race",
                max_attempts=1,
            )
        )
        self.assertEqual(
            self.store.claim(
                "workflow",
                limit=1,
                lease_seconds=30,
                allowed_harnesses=(Harness.CODEX,),
                workspace="/workspace",
                require_fresh_health=False,
                static_validator=lambda _harness: False,
            ),
            [],
        )
        self.assertEqual(self.store.jobs("workflow")[0]["id"], job_id)
        self.assertEqual(self.store.jobs("workflow")[0]["attempts"], 0)

    def test_v7_health_schema_migration_is_restart_safe(self) -> None:
        path = Path(self.temporary.name) / "v7.db"
        _create_v7_schema(path)

        store = Store(path)
        store.initialize()
        store.initialize()

        with closing(sqlite3.connect(path)) as connection, connection:
            version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            job_columns = {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
            health_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(harness_health)")
            }
            row = connection.execute(
                "SELECT status, observed_at, revision FROM harness_health"
            ).fetchone()

        self.assertEqual(version, SCHEMA_VERSION)
        self.assertIn("workspace", job_columns)
        self.assertIn("revision", health_columns)
        self.assertEqual(row, ("unknown", 100.0, 0))

    def test_interrupted_v7_migration_rolls_back_before_restart(self) -> None:
        path = Path(self.temporary.name) / "v7-interrupted.db"
        _create_v7_schema(path)
        store = Store(path)

        def interrupt(connection: sqlite3.Connection) -> None:
            store._add_column_if_missing(connection, "jobs", "workspace", "TEXT")
            raise RuntimeError("migration_interrupted")

        with (
            patch.object(store, "_migrate_v7_to_v8", side_effect=interrupt),
            self.assertRaisesRegex(RuntimeError, "migration_interrupted"),
        ):
            store.initialize()

        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(connection.execute("SELECT version FROM schema_meta").fetchone()[0], 7)
            self.assertNotIn(
                "workspace",
                {row[1] for row in connection.execute("PRAGMA table_info(jobs)")},
            )

        store.initialize()
        with closing(sqlite3.connect(path)) as connection, connection:
            self.assertEqual(
                connection.execute("SELECT version FROM schema_meta").fetchone()[0],
                SCHEMA_VERSION,
            )

    def _write_health(
        self,
        *,
        observed_at: float,
        status: str,
        expires_at: float | None,
        expected_revision: int | None = None,
        expected_owner: str | None = None,
        clear_probe_lease: bool = False,
    ) -> bool:
        return self.store.upsert_harness_health(
            workflow="workflow",
            workspace="/workspace",
            harness=Harness.CODEX,
            status=status,
            reason="readiness_ready" if status == "ready" else "agent_auth_required",
            source="test",
            observed_at=observed_at,
            expires_at=expires_at,
            cooldown_until=None,
            retryable_failures=0,
            expected_revision=expected_revision,
            expected_owner=expected_owner,
            clear_probe_lease=clear_probe_lease,
        )


def _create_v7_schema(path: Path) -> None:
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.executescript("""
            CREATE TABLE schema_meta (version INTEGER NOT NULL);
            INSERT INTO schema_meta(version) VALUES (7);
            CREATE TABLE jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow TEXT NOT NULL,
                title TEXT NOT NULL,
                harness TEXT NOT NULL,
                prompt TEXT NOT NULL,
                dedupe_key TEXT NOT NULL,
                state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL,
                available_at REAL NOT NULL,
                lease_until REAL,
                agent_name TEXT,
                error_code TEXT,
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(workflow, dedupe_key)
            );
            CREATE TABLE receipts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                job_id INTEGER NOT NULL REFERENCES jobs(id),
                attempt INTEGER NOT NULL,
                state TEXT NOT NULL,
                agent_name TEXT NOT NULL,
                agent_state TEXT NOT NULL,
                member_reused INTEGER NOT NULL,
                pane_id TEXT,
                error_code TEXT,
                observed_at REAL NOT NULL
            );
            CREATE TABLE metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at REAL NOT NULL
            );
            CREATE TABLE harness_health (
                workflow TEXT NOT NULL,
                workspace TEXT NOT NULL,
                harness TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT NOT NULL,
                source TEXT NOT NULL,
                observed_at REAL NOT NULL,
                expires_at REAL,
                cooldown_until REAL,
                retryable_failures INTEGER NOT NULL DEFAULT 0,
                probe_lease_until REAL,
                probe_owner TEXT,
                PRIMARY KEY(workflow, workspace, harness)
            );
            INSERT INTO harness_health(
                workflow, workspace, harness, status, reason, source, observed_at
            ) VALUES (
                'workflow', '/workspace', 'codex', 'unknown', 'health_unknown', 'none', 100.0
            );
            """)


if __name__ == "__main__":
    unittest.main()
