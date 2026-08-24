from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import dataclass
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.cli import main
from herdr_orchestrator.herdr import smoke_agent_name
from herdr_orchestrator.model import AgentState, DispatchOutcome, Harness


class WorkflowAwareSmokeTests(unittest.TestCase):
    def test_absent_harness_fails_before_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflow = _write_workflow(
                Path(temporary),
                name="droid-only",
                workers=(("operations", "droid", 1),),
            )
            transport = _RecordingSmokeTransport()

            with patch("herdr_orchestrator.cli.HerdrTransport", transport.factory):
                exit_code, payload = _invoke(
                    "smoke",
                    "--workflow",
                    str(workflow),
                    "--harness",
                    "droid",
                    "--harness",
                    "grok",
                )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["success"], False)
        self.assertEqual(payload["code"], "harness_not_enabled")
        self.assertEqual(payload["reason"], "harness_not_enabled:grok")
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["selected_harnesses"], ["droid", "grok"])
        self.assertEqual(payload["dispatched_harnesses"], [])
        self.assertEqual(
            payload["failures"],
            [{"harness": "grok", "error": "harness_not_enabled"}],
        )
        self.assertEqual(transport.instances, 0)
        self.assertEqual(transport.calls, [])

    def test_selected_harnesses_are_dispatched_with_workflow_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _write_workflow(
                root,
                name="custom-smoke",
                workers=(
                    ("operations", "droid", 2),
                    ("research", "grok", 3),
                ),
            )
            transport = _RecordingSmokeTransport(
                outcomes={
                    Harness.DROID: _outcome(
                        Harness.DROID,
                        workflow_name="custom-smoke",
                        baseline_state_change_seq=10,
                        final_state_change_seq=12,
                    ),
                    Harness.GROK: _outcome(
                        Harness.GROK,
                        workflow_name="custom-smoke",
                        baseline_state_change_seq=20,
                        final_state_change_seq=21,
                        member_reused=True,
                    ),
                }
            )

            with patch("herdr_orchestrator.cli.HerdrTransport", transport.factory):
                exit_code, payload = _invoke(
                    "smoke",
                    "--workflow",
                    str(workflow),
                    "--harness",
                    "grok",
                    "--harness",
                    "droid",
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["failures"], [])
        self.assertEqual(
            [call.harness for call in transport.calls],
            ["droid", "grok"],
        )
        self.assertEqual(payload["selected_harnesses"], ["grok", "droid"])
        self.assertEqual(payload["dispatched_harnesses"], ["droid", "grok"])
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual(
            transport.closed_names,
            [smoke_agent_name("custom-smoke", Harness.DROID)],
        )

        results = {item["harness"]: item for item in payload["results"]}
        self.assertEqual(set(results), {"droid", "grok"})
        for harness, baseline, final in (
            ("droid", 10, 12),
            ("grok", 20, 21),
        ):
            result = results[harness]
            self.assertEqual(result["harness"], harness)
            self.assertEqual(result["selected_harness"], harness)
            self.assertEqual(result["workflow_path"], str(workflow.resolve()))
            self.assertEqual(result["workflow_name"], "custom-smoke")
            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["worker_count"], 2)
            self.assertEqual(result["total_replica_capacity"], 5)
            self.assertEqual(
                result["agent_name"],
                smoke_agent_name("custom-smoke", Harness(harness)),
            )
            self.assertEqual(result["pane_id"], f"pane:{harness}")
            self.assertEqual(result["baseline_state_change_seq"], baseline)
            self.assertEqual(result["final_state_change_seq"], final)
            self.assertTrue(result["lifecycle_sequence_advanced"])
            self.assertRegex(
                result["prompt_metadata_digest"],
                r"^sha256:[a-f0-9]{64}$",
            )

        for call in transport.calls:
            self.assertIn(str(workflow.resolve()), call.prompt)
            self.assertIn("custom-smoke", call.prompt)
            self.assertIn("schema_version", call.prompt)
            self.assertIn("worker_count", call.prompt)
            self.assertIn("total_replica_capacity", call.prompt)
            self.assertNotIn("multi-harness.toml", call.prompt)

    def test_settled_probe_without_lifecycle_change_is_not_success(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflow = _write_workflow(
                Path(temporary),
                name="unchanged-smoke",
                workers=(("operations", "droid", 1),),
            )
            transport = _RecordingSmokeTransport(
                outcomes={
                    Harness.DROID: DispatchOutcome(
                        smoke_agent_name("unchanged-smoke", Harness.DROID),
                        AgentState.DONE,
                        False,
                        "pane:droid",
                        prompt_accepted=True,
                        baseline_state_change_seq=7,
                        final_state_change_seq=7,
                        dispatch_attempted=True,
                    )
                }
            )

            with patch("herdr_orchestrator.cli.HerdrTransport", transport.factory):
                exit_code, payload = _invoke(
                    "smoke",
                    "--workflow",
                    str(workflow),
                    "--harness",
                    "droid",
                )

        self.assertEqual(exit_code, 1)
        self.assertEqual(payload["success"], False)
        self.assertEqual(payload["code"], "smoke_failed")
        self.assertEqual(payload["results"], [])
        self.assertEqual(payload["failures"][0]["error"], "lifecycle_unchanged")
        self.assertEqual(
            transport.closed_names,
            [smoke_agent_name("unchanged-smoke", Harness.DROID)],
        )


@dataclass(frozen=True)
class _SmokeCall:
    harness: str
    prompt: str
    timeout_seconds: int
    agent_name: str


class _RecordingSmokeTransport:
    def __init__(
        self,
        *,
        outcomes: dict[Harness, DispatchOutcome] | None = None,
    ) -> None:
        self.outcomes = outcomes or {}
        self.instances = 0
        self.calls: list[_SmokeCall] = []
        self.closed_names: list[str] = []

    def factory(self, workflow_name: str, workspace: Path) -> _RecordingSmokeTransport:
        self.instances += 1
        self.workflow_name = workflow_name
        self.workspace = workspace
        return self

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
    ) -> DispatchOutcome:
        name = agent_name or f"smoke-{harness.value}"
        self.calls.append(
            _SmokeCall(
                harness=harness.value,
                prompt=prompt,
                timeout_seconds=timeout_seconds,
                agent_name=name,
            )
        )
        return self.outcomes.get(
            harness,
            DispatchOutcome(
                name,
                AgentState.DONE,
                False,
                f"pane:{harness.value}",
                baseline_state_change_seq=1,
                final_state_change_seq=2,
            ),
        )

    def close_created_agent(self, name: str) -> None:
        self.closed_names.append(name)


def _outcome(
    harness: Harness,
    *,
    workflow_name: str,
    baseline_state_change_seq: int,
    final_state_change_seq: int,
    member_reused: bool = False,
) -> DispatchOutcome:
    return DispatchOutcome(
        smoke_agent_name(workflow_name, harness),
        AgentState.DONE,
        member_reused,
        f"pane:{harness.value}",
        prompt_accepted=True,
        dispatch_attempted=True,
        baseline_state_change_seq=baseline_state_change_seq,
        final_state_change_seq=final_state_change_seq,
    )


def _write_workflow(
    root: Path,
    *,
    name: str,
    workers: tuple[tuple[str, str, int], ...],
) -> Path:
    prompt = root / "planner.md"
    prompt.write_text("Read only.", encoding="utf-8")
    worker_rows = "\n".join(
        f"""
[[workers]]
name = "{worker_name}"
harness = "{harness}"
capabilities = []
replicas = {replicas}
""".strip()
        for worker_name, harness, replicas in workers
    )
    workflow = root / f"{name}.toml"
    workflow.write_text(
        f"""
schema_version = 1
name = "{name}"
workspace = "."
state_db = ".orchestrator/state.db"

[coordinator]
poll_seconds = 1
max_parallel = 2
lease_seconds = 120
max_attempts = 2
agent_timeout_seconds = 10

[planner]
enabled = false
harness = "droid"
interval_seconds = 60
prompt_file = "{prompt.name}"
output_file = ".orchestrator/plans/{name}.json"
max_tasks = 2

{worker_rows}
""".strip(),
        encoding="utf-8",
    )
    return workflow


def _invoke(*argv: str) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(list(argv))
    payload = json.loads(output.getvalue())
    if not isinstance(payload, dict):
        raise AssertionError(payload)
    return exit_code, payload


if __name__ == "__main__":
    unittest.main()
