from __future__ import annotations

import json
import subprocess
import tempfile
import threading
import time
import unittest
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
from herdr_orchestrator.model import Harness, NewJob, PlacementTarget
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
        self.assertGreaterEqual(snapshot["summary"]["needs_attention"], 3)

    def test_topology_does_not_attach_foreign_agent_to_local_pane(self) -> None:
        herdr = HerdrObservation(
            "ok",
            None,
            ({"workspace_id": "local", "label": "local"},),
            ({"workspace_id": "local", "tab_id": "local:t1", "label": "local"},),
            ({"workspace_id": "local", "tab_id": "local:t1", "pane_id": "local:p1"},),
            (
                {
                    "name": "foreign-agent",
                    "workspace_id": "foreign",
                    "tab_id": "foreign:t1",
                    "pane_id": "local:p1",
                    "agent_status": "working",
                },
            ),
            (),
        )

        snapshot = RuntimeProjector(
            "example",
            FakeQueueObserver(QueueObservation((), ())),
            FakeHerdrObserver(herdr),
            clock=lambda: 2_000.0,
        ).snapshot()

        pane = snapshot["topology"]["workspaces"][0]["tabs"][0]["panes"][0]
        self.assertIsNone(pane["agent"])

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
                        }
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
                            "terminal_id": "must not leak",
                        }
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
        self.assertNotIn("must not leak", serialized)
        self.assertNotIn("secret", serialized)

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

    def test_http_server_serves_snapshot_and_packaged_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(base, state_db=Path(temporary) / "state.db")
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
