from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from herdr_orchestrator.cli import main
from herdr_orchestrator.research_executor import (
    ResearchConfig,
    ResearchInputError,
    calculate_coverage,
    classify_input,
    decompose_question,
    parse_research_input,
)


class ResearchInputAndDecompositionTests(unittest.TestCase):
    def test_versioned_public_question_is_classified_before_matrix_decomposition(self) -> None:
        envelope = parse_research_input(
            {
                "version": 1,
                "kind": "public-question",
                "question": "How should a bounded workflow be designed?",
            }
        )
        config = ResearchConfig.from_mapping(
            {
                "decomposition": {
                    "facets": ["scope", "evidence"],
                    "perspectives": ["operator", "user"],
                    "required_cells": [
                        {"facet": "scope", "perspective": "operator"},
                        {"facet": "evidence", "perspective": "user"},
                    ],
                },
                "coverage": {
                    "threshold": 1.0,
                    "allow_multi_cell_credit": False,
                },
            }
        )

        classification = classify_input(envelope, config)
        decomposition = decompose_question(envelope, config)

        self.assertEqual(classification.kind, "public")
        self.assertTrue(classification.admitted)
        self.assertEqual([facet.id for facet in decomposition.facets], ["scope", "evidence"])
        self.assertEqual(
            [(cell.facet_id, cell.perspective_id) for cell in decomposition.required_cells],
            [("scope", "operator"), ("evidence", "user")],
        )
        self.assertEqual(decomposition.coverage_policy.threshold, 1.0)

    def test_invalid_input_and_decomposition_fail_closed(self) -> None:
        with self.assertRaisesRegex(ResearchInputError, "research_input_unknown_field"):
            parse_research_input(
                {
                    "version": 1,
                    "kind": "public-question",
                    "question": "valid",
                    "unexpected": True,
                }
            )
        with self.assertRaisesRegex(ResearchInputError, "research_input_kind_unsupported"):
            parse_research_input(
                {
                    "version": 1,
                    "kind": "unsupported",
                    "question": "valid",
                }
            )
        with self.assertRaisesRegex(ResearchInputError, "decomposition_duplicate_cell"):
            decompose_question(
                parse_research_input(
                    {
                        "version": 1,
                        "kind": "public-question",
                        "question": "valid",
                    }
                ),
                ResearchConfig.from_mapping(
                    {
                        "decomposition": {
                            "facets": ["scope"],
                            "perspectives": ["operator"],
                            "required_cells": [
                                {"facet": "scope", "perspective": "operator"},
                                {"facet": "scope", "perspective": "operator"},
                            ],
                        }
                    }
                ),
            )

    def test_coverage_is_required_cells_not_claim_or_citation_count(self) -> None:
        required = (
            ("scope", "operator"),
            ("scope", "user"),
            ("evidence", "operator"),
        )
        below = calculate_coverage(required, {("scope", "operator")})
        equal = calculate_coverage(
            required,
            {("scope", "operator"), ("scope", "user"), ("evidence", "operator")},
        )
        above = calculate_coverage(
            required,
            {
                ("scope", "operator"),
                ("scope", "user"),
                ("evidence", "operator"),
                ("unknown", "cell"),
            },
        )

        self.assertEqual((below.credited_cells, below.total_cells), (1, 3))
        self.assertAlmostEqual(below.ratio, 1 / 3)
        self.assertEqual((equal.credited_cells, equal.total_cells), (3, 3))
        self.assertEqual(equal.ratio, 1.0)
        self.assertEqual((above.credited_cells, above.total_cells), (3, 3))
        self.assertEqual(above.ratio, 1.0)


class ResearchCliContractTests(unittest.TestCase):
    def test_start_pins_classification_and_required_matrix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _workflow(root)

            exit_code, payload = _invoke(
                "research",
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "research-1",
                "--question",
                "What changed?",
            )

            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["success"])
            self.assertEqual(payload["executor"], "research-synthesis")
            self.assertEqual(payload["route"], "research.start")
            self.assertEqual(payload["research"]["classification"]["kind"], "public")
            self.assertEqual(
                len(payload["research"]["decomposition"]["required_cells"]),
                4,
            )
            self.assertEqual(payload["research"]["coverage"]["total_cells"], 4)
            self.assertEqual(payload["state"], "running")
            self.assertNotEqual(payload["state"], "succeeded")

            inspect_exit, inspected = _invoke(
                "research",
                "inspect",
                "--workflow",
                str(workflow),
                "--run-id",
                payload["run_id"],
            )
            self.assertEqual(inspect_exit, 0)
            self.assertEqual(
                inspected["research"]["decomposition"]["facets"],
                [{"id": "scope", "label": "Scope"}, {"id": "evidence", "label": "Evidence"}],
            )
            work_items = inspected["work_items"]
            collection = [
                item
                for item in work_items
                if item["payload"].get("research_kind") == "collection"
            ]
            self.assertEqual(len(collection), 4)
            self.assertEqual(
                {item["state"] for item in collection},
                {"pending"},
            )
            self.assertEqual(
                {item["ready"] for item in collection},
                {True},
            )
            self.assertEqual(
                {
                    item["payload"]["cell_id"]
                    for item in collection
                },
                {
                    "scope:operator",
                    "scope:user",
                    "evidence:operator",
                    "evidence:user",
                },
            )
            frontier = [
                barrier
                for barrier in inspected["barriers"]
                if barrier["barrier_id"] == "research-frontier-0-ready"
            ][0]
            self.assertEqual(frontier["state"], "succeeded")
            self.assertEqual(
                set(frontier["release_work_ids"]),
                {item["work_id"] for item in collection},
            )

    def test_start_accepts_a_versioned_public_question_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            exit_code, payload = _invoke(
                "research",
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "envelope-1",
                "--input",
                json.dumps(
                    {
                        "version": 1,
                        "kind": "public-question",
                        "question": "What changed?",
                    }
                ),
            )
            self.assertEqual(exit_code, 0)
            self.assertTrue(payload["success"])
            self.assertEqual(payload["research"]["input"]["kind"], "public-question")

    def test_missing_required_input_blocks_and_resume_reuses_same_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _workflow(root, requires_private=True)

            start_exit, started = _invoke(
                "research",
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "private-1",
                "--question",
                "Investigate the private deployment.",
            )
            self.assertEqual(start_exit, 0)
            self.assertEqual(started["state"], "blocked")
            run_id = started["run_id"]

            status_exit, status = _invoke(
                "research",
                "status",
                "--workflow",
                str(workflow),
                "--run-id",
                run_id,
            )
            self.assertEqual(status_exit, 0)
            self.assertEqual(status["state"], "blocked")
            self.assertEqual(status["research"]["phase"], "input_required")
            required = status["research"]["required_input"]
            self.assertEqual(required["input_id"], "deployment-context")
            self.assertEqual(required["reason"], "input_required")
            self.assertEqual(status["work_items"], [])

            resume_exit, resumed = _invoke(
                "research",
                "resume",
                "--workflow",
                str(workflow),
                "--run-id",
                run_id,
                "--input-id",
                "deployment-context",
                "--input",
                "redacted private context",
            )
            self.assertEqual(resume_exit, 0)
            self.assertEqual(resumed["run_id"], run_id)
            self.assertEqual(resumed["state"], "running")
            self.assertTrue(resumed["research"]["required_input"]["admitted"])
            self.assertGreater(len(resumed["work_items"]), 0)
            self.assertEqual(
                {
                    item["payload"]["research_kind"]
                    for item in resumed["work_items"]
                    if item["payload"].get("research_kind") == "collection"
                },
                {"collection"},
            )

            repeated_exit, repeated = _invoke(
                "research",
                "resume",
                "--workflow",
                str(workflow),
                "--run-id",
                run_id,
                "--input-id",
                "deployment-context",
                "--input",
                "redacted private context",
            )
            self.assertEqual(repeated_exit, 0)
            self.assertEqual(repeated["run_id"], run_id)
            self.assertEqual(repeated["code"], "already_admitted")

            mismatch_exit, mismatch = _invoke(
                "research",
                "resume",
                "--workflow",
                str(workflow),
                "--run-id",
                run_id,
                "--input-id",
                "deployment-context",
                "--input",
                "different private context",
            )
            self.assertEqual(mismatch_exit, 64)
            self.assertEqual(mismatch["code"], "research_input_conflict")

    def test_run_once_claims_frontier_with_shared_capacity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _workflow(root, replicas=2, max_parallel=3)
            _, started = _invoke(
                "research",
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "frontier-1",
                "--question",
                "What changed?",
            )

            tick_exit, tick = _invoke(
                "run",
                "--workflow",
                str(workflow),
                "--once",
            )
            self.assertEqual(tick_exit, 0)
            self.assertEqual(tick["executor"], "research-synthesis")
            self.assertEqual(len(tick["claimed"]), 2)
            self.assertEqual(
                {claim["replica_slot"] for claim in tick["claimed"]},
                {"ho-grok-01", "ho-grok-02"},
            )
            self.assertEqual(
                {claim["payload"]["research_kind"] for claim in tick["claimed"]},
                {"collection"},
            )

    def test_invalid_start_does_not_create_run_or_runtime_state(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _workflow(root)
            exit_code, payload = _invoke(
                "research",
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "invalid-1",
                "--question",
                "",
            )
            self.assertEqual(exit_code, 64)
            self.assertFalse(payload["success"])
            self.assertEqual(payload["code"], "research_input_missing_question")
            self.assertFalse((root / ".orchestrator" / "state.db").exists())

    def test_invalid_decomposition_is_rejected_before_dispatch_or_state_creation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _workflow(root).read_text(encoding="utf-8")
            workflow = workflow.replace(
                '{facet = "evidence", perspective = "user"},',
                '{facet = "unknown", perspective = "user"},',
            )
            path = root / "invalid-workflow.toml"
            path.write_text(workflow, encoding="utf-8")
            exit_code, payload = _invoke(
                "research",
                "start",
                "--workflow",
                str(path),
                "--dedupe-key",
                "invalid-decomposition",
                "--question",
                "What changed?",
            )
            self.assertEqual(exit_code, 64)
            self.assertFalse(payload["success"])
            self.assertEqual(payload["code"], "decomposition_unknown_facet")
            self.assertFalse((root / ".orchestrator" / "state.db").exists())


def _invoke(*argv: str) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(list(argv))
    value = json.loads(output.getvalue())
    if not isinstance(value, dict):
        raise AssertionError(value)
    return exit_code, value


def _workflow(
    root: Path,
    *,
    requires_private: bool = False,
    replicas: int = 1,
    max_parallel: int = 4,
) -> Path:
    workflow = root / "workflow.toml"
    private_block = (
        """
[research.input]
requires_user_material = true
required_input_id = "deployment-context"
accepted_form = "text"
max_bytes = 4096
"""
        if requires_private
        else ""
    )
    workflow.write_text(
        f"""
schema_version = 2
name = "research-case"
workspace = "."
state_db = ".orchestrator/state.db"
runtime_dir = ".orchestrator/runs/research-case"

[coordinator]
poll_seconds = 1
max_parallel = {max_parallel}
lease_seconds = 120
max_attempts = 2
agent_timeout_seconds = 10

[executor]
kind = "research-synthesis"

[research.decomposition]
max_facets = 4
max_perspectives = 4
facets = ["scope", "evidence"]
perspectives = ["operator", "user"]
required_cells = [
  {{facet = "scope", perspective = "operator"}},
  {{facet = "scope", perspective = "user"}},
  {{facet = "evidence", perspective = "operator"}},
  {{facet = "evidence", perspective = "user"}},
]

[research.coverage]
threshold = 1.0
allow_multi_cell_credit = false

{private_block}
[research.roles]
collector = "collector"

[[workers]]
name = "collector"
harness = "grok"
capabilities = ["research.collect"]
replicas = {replicas}
""".strip(),
        encoding="utf-8",
    )
    return workflow


if __name__ == "__main__":
    unittest.main()
