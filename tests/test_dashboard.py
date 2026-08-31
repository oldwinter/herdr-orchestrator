from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from copy import deepcopy
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from urllib.request import urlopen

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.dashboard.observer import (
    HerdrObservation,
    HerdrObserver,
    QueueObservation,
    SqliteObserver,
)
from herdr_orchestrator.dashboard.projector import RuntimeProjector
from herdr_orchestrator.dashboard.server import DashboardServer, SnapshotFeed
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    NewJob,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
)
from herdr_orchestrator.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeQueueObserver:
    def __init__(self, observation: QueueObservation) -> None:
        self.observation = observation

    def observe(self) -> QueueObservation:
        return self.observation


class FakeHerdrObserver:
    def __init__(self, observation: HerdrObservation) -> None:
        self.observation = observation

    def observe(self) -> HerdrObservation:
        return self.observation


class FakeProjector:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.value = snapshot

    def snapshot(self) -> dict[str, object]:
        return self.value


class DashboardTests(unittest.TestCase):
    def test_server_does_not_create_missing_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            path = Path(temporary) / "missing" / "state.db"
            config = replace(base, state_db=path)

            with self.assertRaisesRegex(ValueError, "dashboard_state_db_not_found"):
                DashboardServer(config, port=0, projector=FakeProjector({}))

            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_server_does_not_migrate_incompatible_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            path = Path(temporary) / "legacy.db"
            Store(path).initialize()
            with sqlite3.connect(path) as connection:
                connection.execute("UPDATE schema_meta SET version = 3")

            config = replace(base, state_db=path)
            server = None
            try:
                with self.assertRaisesRegex(ValueError, "dashboard_state_db_incompatible"):
                    server = DashboardServer(config, port=0, projector=FakeProjector({}))
            finally:
                if server is not None:
                    server.httpd.server_close()

            with sqlite3.connect(path) as connection:
                version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            self.assertEqual(version, 3)

    def test_server_opens_existing_state_db_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            path = Path(temporary) / "readonly.db"
            Store(path).initialize()
            with sqlite3.connect(path) as connection:
                connection.execute("PRAGMA journal_mode = DELETE")
            before = path.stat().st_mtime_ns
            path.chmod(0o444)
            Path(temporary).chmod(0o555)
            config = replace(base, state_db=path)
            server = None
            try:
                server = DashboardServer(config, port=0, projector=FakeProjector({}))
            finally:
                if server is not None:
                    server.httpd.server_close()
                Path(temporary).chmod(0o700)
                path.chmod(0o644)

            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertFalse(path.with_name(f"{path.name}-wal").exists())
            self.assertFalse(path.with_name(f"{path.name}-journal").exists())

    def test_sqlite_observer_never_reads_prompt_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            store = Store(path)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow="example",
                    title="Safe title",
                    harness=Harness.CODEX,
                    prompt="TOP SECRET PROMPT",
                    dedupe_key="secret-v1",
                    max_attempts=2,
                    placement=PlacementTarget.TAB,
                )
            )

            observation = SqliteObserver(path, "example").observe()
            serialized = json.dumps(observation.jobs)

        self.assertNotIn("prompt", observation.jobs[0])
        self.assertNotIn("TOP SECRET PROMPT", serialized)

    def test_sqlite_observer_handles_uri_reserved_path_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state?query.db"
            store = Store(path)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow="example",
                    title="Safe title",
                    harness=Harness.CODEX,
                    prompt="Read only.",
                    dedupe_key="reserved-path-v1",
                    max_attempts=1,
                )
            )

            observation = SqliteObserver(path, "example").observe()

        self.assertEqual([row["title"] for row in observation.jobs], ["Safe title"])

    def test_sqlite_observer_excludes_receipt_values_and_transcripts(self) -> None:
        prompt = "TOP SECRET PROMPT"
        receipt_value = "TOP SECRET RECEIPT PREFIX"
        terminal_output = "TOP SECRET TERMINAL OUTPUT"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            store = Store(path)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow="example",
                    title="Safe title",
                    harness=Harness.CODEX,
                    prompt=prompt,
                    dedupe_key="secret-v1",
                    max_attempts=2,
                    placement=PlacementTarget.TAB,
                    receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, receipt_value),
                )
            )
            claimed = store.claim("example", limit=1, lease_seconds=60)[0]
            store.record_outcome(
                claimed,
                DispatchOutcome(
                    "worker",
                    AgentState.DONE,
                    False,
                    "w1:p1",
                    placement=PlacementTarget.TAB,
                    execution_path="/repo",
                    herdr_workspace_id="w1",
                ),
            )
            with sqlite3.connect(path) as connection:
                connection.execute(
                    "UPDATE jobs SET error_summary = NULL WHERE id = ?",
                    (claimed.job_id,),
                )
                connection.execute(
                    "UPDATE receipts SET error_summary = ? WHERE job_id = ?",
                    (terminal_output, claimed.job_id),
                )

            observation = SqliteObserver(path, "example").observe()
            snapshot = RuntimeProjector(
                "example",
                FakeQueueObserver(observation),
                FakeHerdrObserver(HerdrObservation("ok", None, (), (), (), (), ())),
                clock=lambda: 1.0,
            ).snapshot()
            serialized = json.dumps(
                {
                    "jobs": observation.jobs,
                    "receipts": observation.receipts,
                    "snapshot": snapshot,
                }
            )

        self.assertNotIn(prompt, serialized)
        self.assertNotIn(receipt_value, serialized)
        self.assertNotIn(terminal_output, serialized)
        self.assertNotIn("receipt_value", observation.jobs[0])
        self.assertNotIn("error_summary", observation.receipts[0])

    def test_projector_correlates_jobs_and_reports_runtime_drift(self) -> None:
        now = 2_000.0
        jobs = (
            _job_row(1, "Missing agent", "running", now - 400, "worker-one"),
            _job_row(2, "Still working", "succeeded", now - 10, "worker-two"),
        )
        receipts = (
            {
                "id": 1,
                "job_id": 2,
                "attempt": 1,
                "state": "succeeded",
                "agent_name": "worker-two",
                "agent_state": "done",
                "member_reused": 0,
                "pane_id": "w1:p2",
                "error_code": None,
                "placement": "tab",
                "execution_path": "/repo",
                "herdr_workspace_id": "w1",
                "observed_at": now - 10,
            },
        )
        herdr = HerdrObservation(
            "ok",
            None,
            (
                {
                    "workspace_id": "w1",
                    "label": "repo",
                    "pane_count": 1,
                    "tab_count": 1,
                },
            ),
            (
                {
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                    "label": "Still working",
                },
            ),
            (
                {
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                    "pane_id": "w1:p2",
                    "cwd": "/repo",
                },
            ),
            (
                {
                    "name": "worker-two",
                    "agent": "codex",
                    "agent_status": "working",
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                    "pane_id": "w1:p2",
                    "cwd": "/repo",
                },
            ),
            (
                {
                    "path": "/repo/.orchestrator/worktrees/task-2",
                    "branch": "orchestrator/task-2",
                    "label": "Task 2",
                    "open_workspace_id": "w1",
                    "is_linked_worktree": True,
                },
            ),
        )
        projector = RuntimeProjector(
            "example",
            FakeQueueObserver(QueueObservation(jobs, receipts)),
            FakeHerdrObserver(herdr),
            clock=lambda: now,
        )

        snapshot = projector.snapshot()

        self.assertEqual(snapshot["summary"]["running"], 1)
        self.assertEqual(snapshot["summary"]["active_agents"], 1)
        self.assertLessEqual(
            {
                "schema_version",
                "workflow",
                "generated_at",
                "source_health",
                "summary",
                "jobs",
                "attention",
                "topology",
                "timeline",
            },
            set(snapshot),
        )
        self.assertLessEqual({"workspaces", "projects"}, set(snapshot["topology"]))
        self.assertIn("running_agent_missing", snapshot["jobs"][0]["drift"])
        self.assertIn(
            "terminal_job_agent_working",
            snapshot["jobs"][1]["drift"],
        )
        self.assertEqual(snapshot["jobs"][1]["runtime"]["pane_id"], "w1:p2")
        self.assertIs(snapshot["jobs"][1]["agent_settled"], True)
        self.assertIs(snapshot["jobs"][1]["task_verified"], True)
        self.assertEqual(
            snapshot["topology"]["workspaces"][0]["tabs"][0]["panes"][0]["agent"]["name"],
            "worker-two",
        )
        project = snapshot["topology"]["projects"][0]
        worktree = project["worktrees"][0]
        self.assertEqual(project["label"], "example")
        self.assertEqual(worktree["workspace_id"], "w1")
        self.assertEqual(worktree["branch"], "orchestrator/task-2")
        self.assertTrue(worktree["is_linked_worktree"])
        self.assertEqual(worktree["tabs"][0]["panes"][0]["pane_id"], "w1:p2")
        self.assertEqual(snapshot["timeline"][0]["type"], "receipt")
        attention_codes = {item["code"] for item in snapshot["attention"]}
        self.assertLessEqual(
            {
                "running_agent_missing",
                "terminal_job_agent_working",
                "lease_expired",
                "job_stale",
            },
            attention_codes,
        )

    def test_topology_does_not_cross_join_foreign_entities(self) -> None:
        herdr = HerdrObservation(
            "ok",
            None,
            (
                {"workspace_id": "local", "label": "local"},
                {"workspace_id": "foreign", "label": "foreign"},
            ),
            (
                {"workspace_id": "local", "tab_id": "shared:t1", "label": "local"},
                {"workspace_id": "foreign", "tab_id": "shared:t1", "label": "foreign"},
            ),
            (
                {"workspace_id": "local", "tab_id": "shared:t1", "pane_id": "shared:p1"},
                {"workspace_id": "foreign", "tab_id": "shared:t1", "pane_id": "shared:p1"},
            ),
            (
                {
                    "name": "local-agent",
                    "workspace_id": "local",
                    "tab_id": "shared:t1",
                    "pane_id": "shared:p1",
                    "agent_status": "idle",
                },
                {
                    "name": "foreign-agent",
                    "workspace_id": "foreign",
                    "tab_id": "shared:t1",
                    "pane_id": "shared:p1",
                    "agent_status": "working",
                },
            ),
            (),
        )
        original_herdr = deepcopy(herdr)

        snapshot = RuntimeProjector(
            "example",
            FakeQueueObserver(QueueObservation((), ())),
            FakeHerdrObserver(herdr),
            clock=lambda: 2_000.0,
        ).snapshot()

        workspaces = {
            workspace["workspace_id"]: workspace for workspace in snapshot["topology"]["workspaces"]
        }
        local_panes = workspaces["local"]["tabs"][0]["panes"]
        foreign_panes = workspaces["foreign"]["tabs"][0]["panes"]
        self.assertEqual(len(local_panes), 1)
        self.assertEqual(len(foreign_panes), 1)
        self.assertEqual(local_panes[0]["agent"]["name"], "local-agent")
        self.assertEqual(foreign_panes[0]["agent"]["name"], "foreign-agent")
        self.assertEqual(herdr, original_herdr)

    def test_herdr_observer_scopes_and_whitelists_runtime_fields(self) -> None:
        workspace = Path("/repo")

        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: int | None,
        ) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == ["agent", "list"]:
                result = {
                    "agents": [
                        {
                            "name": "worker",
                            "agent": "codex",
                            "agent_status": "working",
                            "workspace_id": "w1",
                            "tab_id": "w1:t1",
                            "pane_id": "w1:p1",
                            "cwd": "/repo",
                            "terminal_title": "must not leak",
                        },
                        {
                            "name": "outside",
                            "agent": "codex",
                            "agent_status": "working",
                            "workspace_id": "w1",
                            "cwd": "/outside",
                            "terminal_title": "outside output must not leak",
                        },
                        {
                            "name": "malformed-path",
                            "agent": "codex",
                            "agent_status": "working",
                            "workspace_id": "w1",
                            "cwd": "\x00",
                            "terminal_title": "malformed output must not leak",
                        },
                        {
                            "name": "other",
                            "workspace_id": "w9",
                            "cwd": "/other",
                        },
                    ]
                }
            elif argv[1:3] == ["workspace", "list"]:
                result = {
                    "workspaces": [
                        {
                            "workspace_id": "w1",
                            "label": "repo",
                            "worktree": {
                                "repo_root": "/repo",
                                "secret": "must not leak",
                            },
                        },
                        {
                            "workspace_id": "w9",
                            "label": "other",
                            "worktree": {"repo_root": "/other"},
                        },
                    ]
                }
            elif argv[1:3] == ["worktree", "list"]:
                result = {
                    "worktrees": [
                        {
                            "path": "/repo",
                            "branch": "main",
                            "open_workspace_id": "w1",
                            "secret": "must not leak",
                        },
                        {
                            "path": "/repo/.orchestrator/worktrees/empty",
                            "branch": "empty",
                            "open_workspace_id": "",
                        },
                        {
                            "path": "/other",
                            "branch": "other",
                            "open_workspace_id": "w9",
                        },
                    ]
                }
            elif argv[1:3] == ["tab", "list"]:
                result = {
                    "tabs": [
                        {
                            "workspace_id": argv[-1],
                            "tab_id": f"{argv[-1]}:t1",
                            "label": "Task",
                        },
                        {
                            "workspace_id": "w9",
                            "tab_id": "w9:t9",
                            "label": "foreign tab must not leak",
                        },
                    ]
                }
            elif argv[1:3] == ["pane", "list"]:
                result = {
                    "panes": [
                        {
                            "workspace_id": argv[-1],
                            "tab_id": f"{argv[-1]}:t1",
                            "pane_id": f"{argv[-1]}:p1",
                            "cwd": "/repo",
                            "agent": {"terminal_output": "must not leak nested"},
                            "terminal_id": "must not leak",
                        },
                        {
                            "workspace_id": "w9",
                            "tab_id": "w9:t9",
                            "pane_id": "w9:p9",
                            "cwd": "/other",
                            "terminal_id": "foreign output must not leak",
                        },
                        {
                            "workspace_id": "w1",
                            "tab_id": "w1:t1",
                            "pane_id": "w1:p-outside",
                            "cwd": "/outside",
                            "terminal_id": "outside output must not leak",
                        },
                    ]
                }
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"id": "test", "result": result}),
                "",
            )

        observation = HerdrObserver(workspace, runner=runner).observe()
        serialized = json.dumps(
            observation.__dict__
            if hasattr(observation, "__dict__")
            else {
                "workspaces": observation.workspaces,
                "tabs": observation.tabs,
                "panes": observation.panes,
                "agents": observation.agents,
                "worktrees": observation.worktrees,
            }
        )

        self.assertEqual(observation.health, "ok")
        self.assertEqual([row["workspace_id"] for row in observation.workspaces], ["w1"])
        self.assertEqual([row["name"] for row in observation.agents], ["worker"])
        self.assertEqual([row["workspace_id"] for row in observation.tabs], ["w1"])
        self.assertEqual([row["workspace_id"] for row in observation.panes], ["w1"])
        self.assertEqual([row["pane_id"] for row in observation.panes], ["w1:p1"])
        self.assertNotIn("must not leak", serialized)
        self.assertNotIn("secret", serialized)

    def test_herdr_observer_rejects_non_object_rows(self) -> None:
        workspace = Path("/repo")

        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: int | None,
        ) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == ["agent", "list"]:
                result = {"agents": ["malformed terminal output"]}
            else:
                result = {argv[1] + "s": []}
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"id": "test", "result": result}),
                "",
            )

        observation = HerdrObserver(workspace, runner=runner).observe()

        self.assertEqual(observation.health, "unavailable")
        self.assertEqual(observation.error_code, "herdr_invalid_response")

    def test_snapshot_feed_waits_for_new_event(self) -> None:
        feed = SnapshotFeed()
        self.assertEqual(feed.current(), (0, None))

        first_id = feed.publish({"value": 1})
        same_id, none = feed.wait_after(first_id, timeout=0.01)
        second_id = feed.publish({"value": 2})
        observed_id, snapshot = feed.wait_after(first_id, timeout=0.01)
        reset_id, reset_snapshot = feed.wait_after(100, timeout=0.01)

        self.assertEqual(first_id, 1)
        self.assertEqual((same_id, none), (first_id, None))
        self.assertEqual((second_id, observed_id), (2, 2))
        self.assertEqual(snapshot, {"value": 2})
        self.assertEqual((reset_id, reset_snapshot), (2, {"value": 2}))

    def test_sse_future_id_recovers_before_first_snapshot(self) -> None:
        feed = SnapshotFeed()
        result: list[tuple[int, dict[str, object] | None]] = []
        waiter = threading.Thread(
            target=lambda: result.append(feed.wait_after(99, timeout=0.5)),
        )
        waiter.start()
        time.sleep(0.02)
        feed.publish({"value": 1})
        waiter.join(timeout=1)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [(1, {"value": 1})])

    def test_server_shutdown_closes_open_sse_clients(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(base, state_db=Path(temporary) / "state.db")
            Store(config.state_db).initialize()
            projector = FakeProjector(
                {
                    "schema_version": 1,
                    "workflow": "example",
                    "generated_at": 1.0,
                    "source_health": {"queue": "ok", "herdr": "ok"},
                    "summary": {},
                    "jobs": [],
                    "attention": [],
                    "topology": {"workspaces": []},
                    "timeline": [],
                }
            )
            server = DashboardServer(
                config,
                port=0,
                poll_seconds=60,
                projector=projector,
            )
            serve_thread = threading.Thread(target=server.serve_forever)
            connection: HTTPConnection | None = None
            reader: threading.Thread | None = None
            result: list[tuple[str, bytes | None]] = []
            try:
                serve_thread.start()
                deadline = time.monotonic() + 2
                while server.feed.current()[1] is None:
                    if time.monotonic() >= deadline:
                        self.fail("dashboard monitor did not publish a snapshot")
                    time.sleep(0.01)

                host, port = server.address
                event_id, _ = server.feed.current()
                connection = HTTPConnection(host, port, timeout=0.5)
                connection.request(
                    "GET",
                    "/api/events",
                    headers={"Last-Event-ID": str(event_id)},
                )
                response = connection.getresponse()

                def read_response() -> None:
                    try:
                        result.append(("ok", response.read(1)))
                    except Exception as exc:
                        result.append((type(exc).__name__, None))

                reader = threading.Thread(target=read_response, daemon=True)
                reader.start()
                time.sleep(0.05)
                server.shutdown()
                serve_thread.join(timeout=2)
                reader.join(timeout=1)

                self.assertFalse(serve_thread.is_alive())
                self.assertFalse(reader.is_alive())
                self.assertEqual(result, [("ok", b"")])
            finally:
                if connection is not None:
                    connection.close()
                if serve_thread.is_alive():
                    server.shutdown()
                    serve_thread.join(timeout=2)

    def test_server_shutdown_before_serving_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(base, state_db=Path(temporary) / "state.db")
            Store(config.state_db).initialize()
            server = DashboardServer(config, port=0, projector=FakeProjector({}))

            server.shutdown()
            server.shutdown()
            serve_thread = threading.Thread(target=server.serve_forever)
            serve_thread.start()
            serve_thread.join(timeout=1)

        self.assertFalse(serve_thread.is_alive())

    def test_http_server_serves_snapshot_and_packaged_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(base, state_db=Path(temporary) / "state.db")
            Store(config.state_db).initialize()
            projector = FakeProjector(
                {
                    "schema_version": 1,
                    "workflow": "example",
                    "generated_at": 1.0,
                    "source_health": {"queue": "ok", "herdr": "ok"},
                    "summary": {},
                    "jobs": [],
                    "attention": [],
                    "topology": {"workspaces": []},
                    "timeline": [],
                }
            )
            server = DashboardServer(
                config,
                port=0,
                poll_seconds=0.25,
                projector=projector,
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                host, port = server.address
                base_url = f"http://{host}:{port}"
                health = _wait_for_json(f"{base_url}/api/health")
                with urlopen(f"{base_url}/", timeout=2) as response:
                    index = response.read().decode()
                    policy = response.headers["Content-Security-Policy"]
                snapshot = _wait_for_json(f"{base_url}/api/snapshot")
                with urlopen(f"{base_url}/assets/dashboard.css", timeout=2) as response:
                    content_type = response.headers["Content-Type"]
                with urlopen(f"{base_url}/assets/cytoscape.min.js", timeout=2) as response:
                    cytoscape_content_type = response.headers["Content-Type"]
                    cytoscape_data = response.read()
                with urlopen(f"{base_url}/assets/dashboard.js", timeout=2) as response:
                    dashboard_script = response.read().decode()
                with urlopen(f"{base_url}/assets/topology.js", timeout=2) as response:
                    topology_script = response.read().decode()
                connection = HTTPConnection(host, port, timeout=2)
                connection.request(
                    "GET",
                    "/api/health",
                    headers={"Host": "attacker.example"},
                )
                rejected_host = connection.getresponse().status
                connection.close()
            finally:
                server.shutdown()
                thread.join(timeout=3)

        self.assertTrue(health["ok"])
        self.assertIn("Herdr Operations", index)
        self.assertIn("default-src 'self'", policy)
        self.assertEqual(snapshot["snapshot"]["workflow"], "example")
        self.assertEqual(content_type, "text/css; charset=utf-8")
        self.assertEqual(cytoscape_content_type, "text/javascript; charset=utf-8")
        self.assertGreater(len(cytoscape_data), 400_000)
        self.assertIn("The Cytoscape Consortium", cytoscape_data[:180].decode())
        self.assertIn("/assets/cytoscape.min.js", index)
        self.assertIn("/assets/topology.js", index)
        self.assertIn("topologyGraph", topology_script)
        self.assertIn("agent settled", dashboard_script)
        self.assertIn("task verified", dashboard_script)
        self.assertIn("error_summary", dashboard_script)
        self.assertEqual(rejected_host, 421)
        self.assertFalse(thread.is_alive())

    def test_server_rejects_non_loopback_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(base, state_db=Path(temporary) / "state.db")

            with self.assertRaisesRegex(ValueError, "dashboard_host_must_be_loopback"):
                DashboardServer(config, host="0.0.0.0")


def _job_row(
    job_id: int,
    title: str,
    state: str,
    updated_at: float,
    agent_name: str,
) -> dict[str, object]:
    return {
        "id": job_id,
        "title": title,
        "harness": "codex",
        "dedupe_key": f"job-{job_id}",
        "placement": "tab",
        "state": state,
        "attempts": 1,
        "max_attempts": 2,
        "available_at": updated_at,
        "lease_until": updated_at + 60 if state == "running" else None,
        "agent_name": agent_name,
        "error_code": None,
        "agent_settled": state == "succeeded",
        "task_verified": True if state == "succeeded" else None,
        "execution_path": "/repo",
        "herdr_workspace_id": "w1",
        "created_at": updated_at - 100,
        "updated_at": updated_at,
    }


def _wait_for_json(url: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    while True:
        try:
            with urlopen(url, timeout=1) as response:
                return json.loads(response.read())
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
