from __future__ import annotations

import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from herdr_orchestrator.cli import main
from herdr_orchestrator.research_executor import (
    NovelLead,
    ResearchBudgetState,
    ResearchConfig,
    ResearchInputError,
    SourceAvailabilityDecision,
    SourceReceipt,
    build_progress_signature,
    decide_novel_lead,
    decide_source_availability,
    progress_changed,
)


class ResearchRoundDomainTests(unittest.TestCase):
    def test_budget_configuration_is_pinned_and_round_counters_are_bounded(self) -> None:
        config = ResearchConfig.from_mapping(
            {
                "budgets": {
                    "max_lead_rounds": 2,
                    "max_work_items": 5,
                    "max_turns": 7,
                    "max_seconds": 11,
                    "max_stagnant_rounds": 3,
                    "max_public_route_attempts": 4,
                }
            }
        )

        self.assertEqual(config.budgets.max_lead_rounds, 2)
        self.assertEqual(config.budgets.max_work_items, 5)
        self.assertEqual(config.budgets.max_turns, 7)
        self.assertEqual(config.budgets.max_seconds, 11)
        self.assertEqual(config.budgets.max_stagnant_rounds, 3)
        self.assertEqual(config.budgets.max_public_route_attempts, 4)
        self.assertEqual(config.budgets.to_dict()["schema_version"], 1)
        with self.assertRaisesRegex(
            ResearchInputError,
            "research_budget_conflict:max_lead_rounds",
        ):
            ResearchConfig.from_mapping(
                {
                    "loop": {"max_rounds": 2},
                    "budgets": {"max_lead_rounds": 3},
                }
            )

    def test_novel_lead_acceptance_is_canonical_and_targeted(self) -> None:
        lead = NovelLead.from_mapping(
            {
                "schema_version": 1,
                "lead_id": "lead-next",
                "canonical_id": "canonical-next",
                "origin_evidence_ids": ["evidence-1"],
                "target_cell_id": "scope:user",
                "declared_round": 0,
            }
        )

        accepted = decide_novel_lead(
            lead,
            admitted_evidence_ids={"evidence-1"},
            required_cell_ids={"scope:operator", "scope:user"},
            covered_cell_ids={"scope:operator"},
            canonical_lead_ids=set(),
            current_round=0,
            max_lead_rounds=2,
        )
        self.assertTrue(accepted.accepted)
        self.assertEqual(accepted.code, "lead_accepted")
        self.assertEqual(accepted.next_round, 1)

        duplicate = decide_novel_lead(
            lead,
            admitted_evidence_ids={"evidence-1"},
            required_cell_ids={"scope:operator", "scope:user"},
            covered_cell_ids={"scope:operator"},
            canonical_lead_ids={"canonical-next"},
            current_round=0,
            max_lead_rounds=2,
        )
        self.assertFalse(duplicate.accepted)
        self.assertEqual(duplicate.code, "lead_duplicate")

        already_covered = decide_novel_lead(
            lead,
            admitted_evidence_ids={"evidence-1"},
            required_cell_ids={"scope:user"},
            covered_cell_ids={"scope:user"},
            canonical_lead_ids=set(),
            current_round=0,
            max_lead_rounds=2,
        )
        self.assertFalse(already_covered.accepted)
        self.assertEqual(already_covered.code, "lead_already_covered")

    def test_progress_signature_ignores_order_and_nonsemantic_edits(self) -> None:
        first = build_progress_signature(
            canonical_evidence_ids={"evidence-1"},
            credited_required_cell_ids={"scope:operator"},
            resolved_verification_ids=set(),
            contested_disposition_transitions=[],
        )
        replay = build_progress_signature(
            canonical_evidence_ids=["evidence-1"],
            credited_required_cell_ids=["scope:operator"],
            resolved_verification_ids=[],
            contested_disposition_transitions=[],
        )
        progressed = build_progress_signature(
            canonical_evidence_ids={"evidence-1", "evidence-2"},
            credited_required_cell_ids={"scope:operator"},
            resolved_verification_ids=set(),
            contested_disposition_transitions=[],
        )

        self.assertEqual(first.digest, replay.digest)
        self.assertFalse(progress_changed(first, replay))
        self.assertTrue(progress_changed(first, progressed))
        generated_evidence = "source_" + ("a" * 64)
        generated_signature = build_progress_signature(
            canonical_evidence_ids=[generated_evidence],
            credited_required_cell_ids={"scope:operator"},
            resolved_verification_ids=set(),
            contested_disposition_transitions=[],
        )
        self.assertIn(generated_evidence, generated_signature.canonical_evidence_ids)

        contested = build_progress_signature(
            canonical_evidence_ids={"evidence-1"},
            credited_required_cell_ids={"scope:operator"},
            resolved_verification_ids=set(),
            contested_disposition_transitions=[
                {
                    "disposition_id": "claim-1",
                    "disposition": "contested",
                    "evidence_ids": ["evidence-1"],
                }
            ],
        )
        resolved = build_progress_signature(
            canonical_evidence_ids={"evidence-1"},
            credited_required_cell_ids={"scope:operator"},
            resolved_verification_ids=set(),
            contested_disposition_transitions=[
                {
                    "disposition_id": "claim-1",
                    "disposition": "verified",
                    "evidence_ids": ["evidence-1", "evidence-2"],
                }
            ],
        )
        self.assertTrue(progress_changed(contested, resolved))
        self.assertFalse(progress_changed(resolved, contested))

    def test_budget_state_only_resets_stagnation_for_qualifying_deltas(self) -> None:
        first = build_progress_signature(
            canonical_evidence_ids={"evidence-1"},
            credited_required_cell_ids={"scope:operator"},
            resolved_verification_ids=set(),
            contested_disposition_transitions=[],
        )
        duplicate = build_progress_signature(
            canonical_evidence_ids=["evidence-1"],
            credited_required_cell_ids=["scope:operator"],
            resolved_verification_ids=[],
            contested_disposition_transitions=[],
        )
        changed = build_progress_signature(
            canonical_evidence_ids={"evidence-1", "evidence-2"},
            credited_required_cell_ids={"scope:operator"},
            resolved_verification_ids=set(),
            contested_disposition_transitions=[],
        )
        state = ResearchBudgetState()
        self.assertTrue(state.observe_signature(first))
        self.assertFalse(state.observe_signature(duplicate))
        self.assertEqual(state.stagnant_rounds, 1)
        self.assertTrue(state.observe_signature(changed))
        self.assertEqual(state.stagnant_rounds, 0)
        restored = ResearchBudgetState.from_mapping(state.to_dict())
        self.assertEqual(restored.to_dict(), state.to_dict())

    def test_every_budget_has_a_deterministic_terminal(self) -> None:
        cases = (
            ("work_items_used", "work_item_budget_exhausted"),
            ("turns_used", "turn_budget_exhausted"),
            ("elapsed_seconds", "time_budget_exhausted"),
            ("lead_rounds_used", "lead_round_budget_exhausted"),
            ("public_route_attempts_used", "required_public_route_exhausted"),
        )
        for field, expected_code in cases:
            with self.subTest(field=field):
                state = ResearchBudgetState(**{field: 1})
                terminal = state.terminal(
                    max_work_items=1,
                    max_turns=1,
                    max_seconds=1,
                    max_lead_rounds=1,
                    max_public_route_attempts=1,
                    max_stagnant_rounds=10,
                    unresolved_coverage=True,
                )
                self.assertIsNotNone(terminal)
                self.assertEqual(terminal.code, expected_code)

        stagnant = ResearchBudgetState(stagnant_rounds=2).terminal(
            max_work_items=10,
            max_turns=10,
            max_seconds=10,
            max_lead_rounds=10,
            max_public_route_attempts=10,
            max_stagnant_rounds=2,
            unresolved_coverage=True,
        )
        self.assertIsNotNone(stagnant)
        self.assertEqual(stagnant.code, "coverage_stagnant")

    def test_unavailable_and_changed_sources_never_become_silent_success(self) -> None:
        unavailable = SourceReceipt.from_retrieval(
            requested_url="https://example.test/unavailable",
            final_url="https://example.test/unavailable",
            redirect_chain=(),
            retrieved_content=None,
            retrieval_order=1,
            retrieved_at=1_700_000_000,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-1",
            attempt_id="attempt-1",
            fencing_token="fence-1",
            harness="grok",
            worker="collector",
            agent="agent-1",
            pane="pane:agent-1",
            status=503,
            outcome="unavailable",
        )
        retry = decide_source_availability(
            unavailable,
            attempts_used=1,
            max_attempts=2,
            replacement_available=False,
        )
        self.assertIsInstance(retry, SourceAvailabilityDecision)
        self.assertEqual(retry.action, "retry")
        self.assertFalse(retry.coverage_credit)

        changed = SourceReceipt.from_retrieval(
            requested_url="https://example.test/changed",
            final_url="https://example.test/changed",
            redirect_chain=(),
            retrieved_content=b"new bytes",
            retrieval_order=2,
            retrieved_at=1_700_000_001,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-1",
            attempt_id="attempt-2",
            fencing_token="fence-2",
            harness="grok",
            worker="collector",
            agent="agent-1",
            pane="pane:agent-1",
            outcome="changed",
        )
        replace = decide_source_availability(
            changed,
            attempts_used=1,
            max_attempts=1,
            replacement_available=True,
        )
        self.assertEqual(replace.action, "replace")
        self.assertEqual(replace.code, "source_changed_replace")
        self.assertFalse(replace.coverage_credit)

        failed = decide_source_availability(
            changed,
            attempts_used=1,
            max_attempts=1,
            replacement_available=False,
        )
        self.assertEqual(failed.action, "fail")
        self.assertEqual(failed.code, "required_public_route_exhausted")
        self.assertFalse(failed.coverage_credit)


class ResearchRoundFixtureCliTests(unittest.TestCase):
    def test_normal_run_tick_applies_research_work_item_budget_before_claiming(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary), max_work_items=1)
            _, started = _invoke(
                "research",
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "normal-budget",
                "--question",
                "What changed?",
            )
            first_exit, first = _invoke(
                "run",
                "--workflow",
                str(workflow),
                "--once",
            )
            second_exit, second = _invoke(
                "run",
                "--workflow",
                str(workflow),
                "--once",
            )

        self.assertEqual(first_exit, 0)
        self.assertEqual(second_exit, 0)
        self.assertEqual(first["claimable_run_ids"], [])
        self.assertEqual(first["claimed"], [])
        self.assertEqual(first["states"][0]["state"], "failed")
        self.assertEqual(second["claimed"], [])
        self.assertEqual(second["states"][0]["state"], "failed")
        self.assertEqual(started["state"], "failed")

    def test_start_pins_round_budgets_and_initial_progress_signature(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            exit_code, payload = _invoke(
                "research",
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "budget-pin",
                "--question",
                "What changed?",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(payload["research"]["budgets"]["max_turns"], 2)
        self.assertEqual(payload["research"]["progress"]["stagnant_rounds"], 0)

    def test_novel_lead_fixture_records_lineage_and_suppresses_duplicate(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            exit_code, payload = _invoke(
                "research",
                "round-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "novel-lead",
            )
            inspect_exit, inspected = _invoke(
                "research",
                "inspect",
                "--workflow",
                str(workflow),
                "--run-id",
                payload["run_id"],
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["code"], "lead_accepted")
        self.assertEqual(payload["terminal_state"], "succeeded")
        self.assertEqual(payload["lead_decisions"][0]["code"], "lead_accepted")
        self.assertEqual(payload["lead_decisions"][1]["code"], "lead_duplicate")
        self.assertEqual(payload["next_round_work"][0]["round"], 1)
        self.assertEqual(payload["progress"]["stagnant_rounds"], 0)
        self.assertEqual(inspect_exit, 0)
        admitted_lineages = [
            artifact["lineage"]
            for artifact in inspected["artifacts"]
            if artifact["work_id"] == "research-round-1-scope-user"
        ]
        self.assertEqual(admitted_lineages, [[
            "research-round-1-scope-user",
            "research-round-0-origin",
        ]])
        self.assertEqual(
            inspected["research"]["lead_decisions"][0]["lead"]["origin_evidence_ids"],
            ["evidence-initial"],
        )

    def test_rejected_lead_replay_preserves_domain_negative_result(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            first_exit, first = _invoke(
                "research",
                "lead-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "duplicate-lead",
            )
            replay_exit, replay = _invoke(
                "research",
                "lead-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "duplicate-lead",
            )

        self.assertEqual(first_exit, 0)
        self.assertEqual(replay_exit, 0)
        self.assertFalse(first["success"])
        self.assertFalse(replay["success"])
        self.assertEqual(replay["code"], "lead_duplicate")
        self.assertTrue(replay["idempotent"])

    def test_stagnant_round_is_terminal_and_quiescent(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary), max_stagnant_rounds=2)
            exit_code, payload = _invoke(
                "research",
                "budget-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "stagnant",
            )

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["code"], "coverage_stagnant")
        self.assertEqual(payload["terminal_state"], "failed")
        self.assertEqual(payload["progress"]["stagnant_rounds"], 2)
        self.assertEqual(payload["dispatch_log_after_terminal"], [])
        self.assertEqual(payload["budget"]["max_work_items"], 2)
        self.assertEqual(payload["terminal"]["budget"], "stagnant_rounds")

    def test_duplicate_and_prose_only_fixture_changes_do_not_reset_stagnation(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary), max_stagnant_rounds=1)
            for case in ("duplicate-evidence", "prose-only"):
                with self.subTest(case=case):
                    exit_code, payload = _invoke(
                        "research",
                        "progress-fixture",
                        "--workflow",
                        str(workflow),
                        "--case",
                        case,
                    )
                    self.assertEqual(exit_code, 0)
                    self.assertFalse(payload["success"])
                    self.assertEqual(payload["code"], "coverage_stagnant")
                    self.assertEqual(payload["progress"]["stagnant_rounds"], 1)
                    self.assertFalse(payload["progress"]["progress_changed"])

    def test_each_configured_exhaustion_has_stable_code(self) -> None:
        cases = {
            "work-item-exhaustion": "work_item_budget_exhausted",
            "turn-exhaustion": "turn_budget_exhausted",
            "time-exhaustion": "time_budget_exhausted",
            "lead-round-exhaustion": "lead_round_budget_exhausted",
            "required-route-exhaustion": "required_public_route_exhausted",
        }
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary), max_stagnant_rounds=99)
            for case, expected_code in cases.items():
                with self.subTest(case=case):
                    exit_code, payload = _invoke(
                        "research",
                        "round-fixture",
                        "--workflow",
                        str(workflow),
                        "--case",
                        case,
                    )
                    self.assertEqual(exit_code, 0)
                    self.assertFalse(payload["success"])
                    self.assertEqual(payload["code"], expected_code)
                    self.assertEqual(payload["terminal_state"], "failed")
                    self.assertEqual(payload["dispatch_log_after_terminal"], [])

    def test_unavailable_and_changed_fixture_is_explicit_and_bounded(self) -> None:
        cases = {
            "unavailable-retry": ("unavailable", "retry", "source_unavailable_retry"),
            "unavailable-replacement": (
                "unavailable",
                "replace",
                "source_unavailable_replace",
            ),
            "unavailable-fail": (
                "unavailable",
                "fail",
                "required_public_route_exhausted",
            ),
            "changed-replacement": (
                "changed",
                "replace",
                "source_changed_replace",
            ),
        }
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            for case, (outcome, action, code) in cases.items():
                with self.subTest(case=case):
                    exit_code, payload = _invoke(
                        "research",
                        "source-fixture",
                        "--workflow",
                        str(workflow),
                        "--case",
                        case,
                    )
                    self.assertEqual(exit_code, 0)
                    self.assertFalse(payload["success"])
                    self.assertEqual(payload["source"]["outcome"], outcome)
                    self.assertEqual(payload["source_decision"]["action"], action)
                    self.assertEqual(payload["source_decision"]["code"], code)
                    self.assertEqual(payload["source"]["excerpt_ids"], [])
                    self.assertFalse(payload["source_decision"]["coverage_credit"])

    def test_round_fixture_inspect_exposes_durable_terminal_and_source_decision(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            fixture_exit, fixture = _invoke(
                "research",
                "source-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "changed-without-excerpt",
            )
            inspect_exit, inspected = _invoke(
                "research",
                "inspect",
                "--workflow",
                str(workflow),
                "--run-id",
                fixture["run_id"],
            )

        self.assertEqual(fixture_exit, 0)
        self.assertEqual(inspect_exit, 0)
        self.assertEqual(inspected["research"]["phase"], "round-fixture")
        self.assertEqual(
            inspected["research"]["source"]["outcome"],
            "changed",
        )
        self.assertEqual(
            inspected["research"]["source_decision"]["action"],
            "replace",
        )


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
    max_stagnant_rounds: int = 2,
    max_work_items: int = 2,
) -> Path:
    workflow = root / "workflow.toml"
    workflow.write_text(
        f"""
schema_version = 2
name = "research-round-case"
workspace = "."
state_db = ".orchestrator/state.db"
runtime_dir = ".orchestrator/runs/research-round-case"

[coordinator]
poll_seconds = 1
max_parallel = 2
lease_seconds = 120
max_attempts = 2
agent_timeout_seconds = 10

[executor]
kind = "research-synthesis"

[research.budgets]
max_lead_rounds = 2
max_work_items = {max_work_items}
max_turns = 2
max_seconds = 2
max_stagnant_rounds = {max_stagnant_rounds}
max_public_route_attempts = 2

[[workers]]
name = "collector"
harness = "grok"
capabilities = ["research.collect"]
replicas = 1
""".strip(),
        encoding="utf-8",
    )
    return workflow


if __name__ == "__main__":
    unittest.main()
