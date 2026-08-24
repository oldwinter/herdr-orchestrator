from __future__ import annotations

import unittest
import json
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from herdr_orchestrator.research_evidence import (
    CriticalityPolicy,
    EvidenceRelation,
    ExcerptReceipt,
    ResearchEvidenceRegister,
    SourceReceipt,
    TypedClaim,
    VerificationAssignment,
    VerificationDisposition,
    derive_criticality,
)
from herdr_orchestrator.cli import main
from herdr_orchestrator.research_executor import ResearchConfig


class ResearchCriticalityTests(unittest.TestCase):
    def test_verification_policy_is_pinned_in_research_config(self) -> None:
        config = ResearchConfig.from_mapping(
            {
                "verification": {
                    "critical_claim_types": ["factual", "causal"],
                    "required_conclusions": ["conclusion-main"],
                    "require_harness_separation": False,
                }
            }
        )

        self.assertEqual(
            config.verification.critical_claim_types,
            ("factual", "causal"),
        )
        self.assertEqual(
            config.verification.required_conclusion_ids,
            ("conclusion-main",),
        )
        self.assertFalse(config.verification.require_harness_separation)

    def test_research_start_exposes_pinned_verification_policy(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            exit_code, payload = _invoke(
                "research",
                "start",
                "--workflow",
                str(workflow),
                "--dedupe-key",
                "policy-pin",
                "--question",
                "What is the claim?",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            payload["research"]["verification_policy"],
            {
                "policy_version": 1,
                "critical_claim_types": ["factual", "quantitative", "causal"],
                "required_conclusion_ids": ["conclusion-main"],
                "require_independent_logical_agent": True,
                "require_harness_separation": True,
                "forbid_source_reuse": True,
            },
        )

    def test_criticality_is_derived_from_required_conclusion_use(self) -> None:
        policy = CriticalityPolicy(
            critical_claim_types=("factual", "quantitative", "causal"),
        )
        collector_flagged = TypedClaim(
            claim_id="claim-flagged",
            claim_type="factual",
            text="A collector flag is not enough.",
            critical=True,
        )
        required_fact = TypedClaim(
            claim_id="claim-required",
            claim_type="factual",
            text="This fact is used in a required conclusion.",
            critical=False,
            required_conclusion_ids=("conclusion-main",),
        )
        recommendation = TypedClaim(
            claim_id="claim-recommendation",
            claim_type="recommendation",
            text="A recommendation is not independently critical.",
            premise_ids=("claim-required",),
            evidence_ids=("relation-support",),
            required_conclusion_ids=("conclusion-main",),
        )

        self.assertFalse(derive_criticality(collector_flagged, policy))
        self.assertTrue(derive_criticality(required_fact, policy))
        self.assertFalse(derive_criticality(recommendation, policy))


class ResearchVerificationTests(unittest.TestCase):
    def test_public_contested_fixture_preserves_both_relations_and_status(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            exit_code, payload = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "contradiction-pack",
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["code"], "claim_contested")
        evidence = payload["evidence"]
        self.assertEqual(
            {item["relation"] for item in evidence["relations"]},
            {"support", "contradiction"},
        )
        self.assertEqual(evidence["contested_claim_ids"], ["claim-critical"])
        self.assertEqual(evidence["verification_credit_claim_ids"], [])
        self.assertEqual(evidence["verification_dispositions"], [])

    def test_public_critical_fixture_requires_independent_verifier_for_gate_credit(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            unverified_exit, unverified = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "critical-unverified",
            )
            verified_exit, verified = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "critical-independent",
            )

        self.assertEqual(unverified_exit, 0)
        self.assertFalse(unverified["success"])
        self.assertEqual(unverified["code"], "critical_claim_unverified")
        self.assertTrue(unverified["verification"]["critical"])
        self.assertFalse(unverified["verification"]["gate_credit"])
        self.assertEqual(verified_exit, 0)
        self.assertTrue(verified["success"])
        self.assertEqual(verified["code"], "critical_claim_verified")
        self.assertEqual(verified["verification"]["state"], "verified")
        self.assertTrue(verified["verification"]["gate_credit"])
        self.assertEqual(
            verified["verification_assignment"]["verifier_logical_agent_id"],
            verified["evidence"]["sources"][2]["agent"],
        )
        self.assertNotEqual(
            verified["verification_assignment"]["verifier_logical_agent_id"],
            verified["verification_assignment"]["collector_logical_agent_ids"][0],
        )

    def test_public_verification_fixture_rejects_self_verification_and_downgrade(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            self_exit, self_case = _invoke(
                "research",
                "evidence-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "critical-self-verification",
            )
            downgrade_exit, downgrade = _invoke(
                "research",
                "evidence-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "critical-downgrade",
            )

        self.assertEqual(self_exit, 0)
        self.assertFalse(self_case["success"])
        self.assertEqual(
            self_case["code"],
            "verification_assignment_self_verification",
        )
        self.assertEqual(self_case["verification"]["state"], "contested")
        self.assertEqual(downgrade_exit, 0)
        self.assertFalse(downgrade["success"])
        self.assertEqual(downgrade["code"], "critical_claim_unverified")
        self.assertTrue(downgrade["verification"]["critical"])

    def test_disposition_history_is_exported_when_contested_then_verified(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            exit_code, payload = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "disposition-history",
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["code"], "critical_claim_verified")
        history = payload["evidence"]["claim_statuses"][0]["disposition_history"]
        self.assertEqual(
            [item["disposition"] for item in history],
            ["contested", "verified"],
        )
        self.assertEqual(
            [item["current"] for item in history],
            [False, True],
        )

    def test_fixture_state_is_inspectable_exportable_and_idempotent(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow = _workflow(root)
            fixture_exit, fixture = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "contested",
            )
            inspect_exit, inspected = _invoke(
                "research",
                "inspect",
                "--workflow",
                str(workflow),
                "--run-id",
                fixture["run_id"],
            )
            export_exit, exported = _invoke(
                "research",
                "export",
                "--workflow",
                str(workflow),
                "--run-id",
                fixture["run_id"],
            )
            replay_exit, replay = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "contested",
            )

            report_path = Path(exported["report_path"])
            register_path = Path(exported["register_path"])
            self.assertTrue(report_path.is_file())
            self.assertTrue(register_path.is_file())
            report = report_path.read_text(encoding="utf-8")

        self.assertEqual(fixture_exit, 0)
        self.assertEqual(inspect_exit, 0)
        self.assertEqual(export_exit, 0)
        self.assertEqual(replay_exit, 0)
        self.assertEqual(inspected["state"], "succeeded")
        self.assertEqual(
            inspected["research"]["verification"]["state"],
            "contested",
        )
        self.assertIn("relation-support", report)
        self.assertIn("relation-contradiction", report)
        self.assertIn("contested", report)
        self.assertTrue(replay["idempotent"])
        self.assertEqual(replay["events"], fixture["events"])

    def test_source_reuse_cannot_supply_independent_verification(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            exit_code, payload = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "source-reuse",
            )

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["code"], "verification_disposition_source_reuse")
        self.assertFalse(payload["verification"]["gate_credit"])

    def test_failed_verification_run_cannot_export(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            fixture_exit, fixture = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "critical-unverified",
            )
            export_exit, exported = _invoke(
                "research",
                "export",
                "--workflow",
                str(workflow),
                "--run-id",
                fixture["run_id"],
            )

        self.assertEqual(fixture_exit, 0)
        self.assertFalse(fixture["success"])
        self.assertEqual(export_exit, 64)
        self.assertFalse(exported["success"])
        self.assertEqual(
            exported["code"],
            "research_export_requires_succeeded_run",
        )

    def test_disposition_must_account_for_every_contradiction_and_current_assignment(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            missing_exit, missing = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "unaccounted-contradiction",
            )
            stale_exit, stale = _invoke(
                "research",
                "verification-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "stale-assignment",
            )

        self.assertEqual(missing_exit, 0)
        self.assertFalse(missing["success"])
        self.assertEqual(
            missing["code"],
            "verification_disposition_contradictions_unaccounted",
        )
        self.assertEqual(stale_exit, 0)
        self.assertFalse(stale["success"])
        self.assertEqual(
            stale["code"],
            "verification_disposition_assignment_not_current",
        )

    def test_opposing_evidence_is_contested_and_history_is_retained(self) -> None:
        register, claim, first, second = _register_with_opposing_evidence()

        status = register.claim_status(claim.claim_id)

        self.assertEqual(status["state"], "contested")
        self.assertEqual(
            status["contradiction_relation_ids"],
            [second.relation_id],
        )
        self.assertEqual(
            status["support_relation_ids"],
            [first.relation_id],
        )
        self.assertEqual(status["disposition_history"], [])
        self.assertEqual(register.contested_claim_ids, (claim.claim_id,))

    def test_critical_claim_requires_independent_current_verification(self) -> None:
        register, claim, supporting, contradiction = _register_with_opposing_evidence()
        verification_excerpt = next(
            excerpt
            for excerpt in register.excerpts
            if excerpt.source_id == "source-verification"
        )
        register.admit_relation(
            EvidenceRelation(
                relation_id="relation-verification",
                claim_id=claim.claim_id,
                relation="support",
                source_id="source-verification",
                excerpt_id=verification_excerpt.excerpt_id,
            )
        )
        assignment = register.admit_verification_assignment(
            VerificationAssignment(
                assignment_id="assignment-main",
                claim_id=claim.claim_id,
                verification_work_id="verify-claim",
                verifier_logical_agent_id="agent-verifier",
                verifier_harness="claude",
                collector_logical_agent_ids=("agent-collector",),
                collector_harnesses=("grok",),
                source_ids=("source-verification",),
                excerpt_ids=(verification_excerpt.excerpt_id,),
                run_id="run-1",
                verification_attempt_id="attempt-3",
                verification_fencing_token="fence-3",
            )
        )

        self.assertEqual(assignment.verifier_logical_agent_id, "agent-verifier")
        with self.assertRaisesRegex(
            ValueError,
            "verification_assignment_self_verification",
        ):
            register.admit_verification_assignment(
                VerificationAssignment(
                    assignment_id="assignment-self",
                    claim_id=claim.claim_id,
                    verification_work_id="verify-self",
                    verifier_logical_agent_id="agent-collector",
                    verifier_harness="grok",
                    collector_logical_agent_ids=("agent-collector",),
                    collector_harnesses=("grok",),
                    source_ids=("source-verifier",),
                    excerpt_ids=("excerpt-verifier",),
                )
            )

        with self.assertRaisesRegex(
            ValueError,
            "verification_disposition_source_reuse",
        ):
            collector_excerpt = next(
                excerpt
                for excerpt in register.excerpts
                if excerpt.source_id == "source-collector"
            )
            register.admit_verification_disposition(
                VerificationDisposition(
                    disposition_id="disposition-self",
                    claim_id=claim.claim_id,
                    assignment_id=assignment.assignment_id,
                    disposition="verified",
                    reason="confidence prose is not verification",
                    evidence_ids=(supporting.relation_id,),
                    contradiction_ids=(contradiction.relation_id,),
                    verifier_logical_agent_id="agent-verifier",
                    verifier_harness="claude",
                    source_ids=("source-collector",),
                    excerpt_ids=(collector_excerpt.excerpt_id,),
                )
            )

        valid = register.admit_verification_disposition(
            VerificationDisposition(
                disposition_id="disposition-valid",
                claim_id=claim.claim_id,
                assignment_id=assignment.assignment_id,
                disposition="verified",
                reason="independent matching evidence accounts for the contradiction",
                evidence_ids=("relation-verification",),
                contradiction_ids=(contradiction.relation_id,),
                verifier_logical_agent_id="agent-verifier",
                verifier_harness="claude",
                source_ids=("source-verification",),
                excerpt_ids=(verification_excerpt.excerpt_id,),
            )
        )
        self.assertEqual(valid.disposition, "verified")
        status = register.claim_status(claim.claim_id)
        self.assertEqual(status["state"], "verified")
        self.assertTrue(status["gate_credit"])
        self.assertEqual(len(status["disposition_history"]), 1)
        self.assertEqual(
            {item.relation_id for item in register.relations},
            {
                supporting.relation_id,
                contradiction.relation_id,
                "relation-verification",
            },
        )

    def test_criticality_cannot_be_downgraded_by_disposition(self) -> None:
        register, claim, _, contradiction = _register_with_opposing_evidence()
        verification_excerpt = next(
            excerpt
            for excerpt in register.excerpts
            if excerpt.source_id == "source-verifier"
        )
        assignment = register.admit_verification_assignment(
            VerificationAssignment(
                assignment_id="assignment-downgrade",
                claim_id=claim.claim_id,
                verification_work_id="verify-downgrade",
                verifier_logical_agent_id="agent-verifier",
                verifier_harness="claude",
                collector_logical_agent_ids=("agent-collector",),
                collector_harnesses=("grok",),
                source_ids=("source-verifier",),
                excerpt_ids=(verification_excerpt.excerpt_id,),
            )
        )
        register.admit_verification_disposition(
            VerificationDisposition(
                disposition_id="disposition-downgrade",
                claim_id=claim.claim_id,
                assignment_id=assignment.assignment_id,
                disposition="unverified",
                reason="verification did not admit matching evidence",
                evidence_ids=(),
                contradiction_ids=(contradiction.relation_id,),
                verifier_logical_agent_id="agent-verifier",
                verifier_harness="claude",
                source_ids=(),
                excerpt_ids=(),
            )
        )

        status = register.claim_status(claim.claim_id)
        self.assertTrue(status["critical"])
        self.assertFalse(status["gate_credit"])
        self.assertEqual(status["state"], "unverified")

    def test_verification_history_round_trips_without_losing_current_state(self) -> None:
        register, claim, _, contradiction = _register_with_opposing_evidence()
        verification_excerpt = next(
            excerpt
            for excerpt in register.excerpts
            if excerpt.source_id == "source-verification"
        )
        register.admit_relation(
            EvidenceRelation(
                relation_id="relation-verification",
                claim_id=claim.claim_id,
                relation="support",
                source_id="source-verification",
                excerpt_id=verification_excerpt.excerpt_id,
            )
        )
        assignment = register.admit_verification_assignment(
            VerificationAssignment(
                assignment_id="assignment-roundtrip",
                claim_id=claim.claim_id,
                verification_work_id="verify-claim",
                verifier_logical_agent_id="agent-verifier",
                verifier_harness="claude",
                collector_logical_agent_ids=("agent-collector",),
                collector_harnesses=("grok",),
                source_ids=("source-verification",),
                excerpt_ids=(verification_excerpt.excerpt_id,),
                run_id="run-1",
                verification_attempt_id="attempt-3",
                verification_fencing_token="fence-3",
            )
        )
        register.admit_verification_disposition(
            VerificationDisposition(
                disposition_id="disposition-roundtrip-contested",
                claim_id=claim.claim_id,
                assignment_id=assignment.assignment_id,
                disposition="contested",
                reason="retain both observations",
                evidence_ids=(),
                contradiction_ids=(contradiction.relation_id,),
                verifier_logical_agent_id="agent-verifier",
                verifier_harness="claude",
                source_ids=(),
                excerpt_ids=(),
            )
        )
        register.admit_verification_disposition(
            VerificationDisposition(
                disposition_id="disposition-roundtrip-verified",
                claim_id=claim.claim_id,
                assignment_id=assignment.assignment_id,
                disposition="verified",
                reason="new evidence accounts for both observations",
                evidence_ids=("relation-verification",),
                contradiction_ids=(contradiction.relation_id,),
                verifier_logical_agent_id="agent-verifier",
                verifier_harness="claude",
                source_ids=("source-verification",),
                excerpt_ids=(verification_excerpt.excerpt_id,),
            )
        )

        restored = ResearchEvidenceRegister.from_mapping(register.to_dict())

        self.assertEqual(restored.to_dict(), register.to_dict())
        self.assertEqual(
            [item.disposition for item in restored.verification_dispositions],
            ["contested", "verified"],
        )
        self.assertEqual(restored.claim_status(claim.claim_id)["state"], "verified")

    def test_late_contradiction_reopens_a_verified_claim(self) -> None:
        register, claim, _, contradiction = _register_with_opposing_evidence()
        verification_excerpt = next(
            excerpt
            for excerpt in register.excerpts
            if excerpt.source_id == "source-verification"
        )
        verification_relation = register.admit_relation(
            EvidenceRelation(
                relation_id="relation-verification",
                claim_id=claim.claim_id,
                relation="support",
                source_id="source-verification",
                excerpt_id=verification_excerpt.excerpt_id,
            )
        )
        assignment = register.admit_verification_assignment(
            VerificationAssignment(
                assignment_id="assignment-late",
                claim_id=claim.claim_id,
                verification_work_id="verify-claim",
                verifier_logical_agent_id="agent-verifier",
                verifier_harness="claude",
                collector_logical_agent_ids=("agent-collector",),
                collector_harnesses=("grok",),
                source_ids=("source-verification",),
                excerpt_ids=(verification_excerpt.excerpt_id,),
                run_id="run-1",
                verification_attempt_id="attempt-3",
                verification_fencing_token="fence-3",
            )
        )
        register.admit_verification_disposition(
            VerificationDisposition(
                disposition_id="disposition-late",
                claim_id=claim.claim_id,
                assignment_id=assignment.assignment_id,
                disposition="verified",
                reason="verification accounts for the known contradiction",
                evidence_ids=(verification_relation.relation_id,),
                contradiction_ids=(contradiction.relation_id,),
                verifier_logical_agent_id="agent-verifier",
                verifier_harness="claude",
                source_ids=("source-verification",),
                excerpt_ids=(verification_excerpt.excerpt_id,),
            )
        )
        self.assertTrue(register.claim_status(claim.claim_id)["gate_credit"])

        late_source = SourceReceipt.from_retrieval(
            requested_url="https://example.test/late-contradiction",
            final_url="https://example.test/late-contradiction",
            redirect_chain=(),
            retrieved_content=b"late opposing observation",
            retrieval_order=4,
            retrieved_at=1_700_000_004,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-late",
            attempt_id="attempt-4",
            fencing_token="fence-4",
            harness="grok",
            worker="collector",
            agent="agent-opposing",
            pane="pane:agent-opposing",
            source_id="source-late-contradiction",
        )
        late_excerpt = ExcerptReceipt.from_text(
            late_source,
            text="late opposing observation",
            selector="body",
        )
        register.admit_source(late_source)
        register.admit_excerpt(late_excerpt)
        register.admit_relation(
            EvidenceRelation(
                relation_id="relation-late-contradiction",
                claim_id=claim.claim_id,
                relation="contradiction",
                source_id=late_source.source_id,
                excerpt_id=late_excerpt.excerpt_id,
            )
        )

        status = register.claim_status(claim.claim_id)
        self.assertEqual(status["state"], "contested")
        self.assertFalse(status["gate_credit"])
        self.assertEqual(status["disposition_id"], None)

    def test_verification_identity_aliases_are_not_ambiguous(self) -> None:
        with self.assertRaisesRegex(
            ValueError,
            "verification_assignment_duplicate_verifier_agent",
        ):
            VerificationAssignment.from_mapping(
                {
                    "schema_version": 1,
                    "assignment_id": "assignment-duplicate",
                    "claim_id": "claim-critical",
                    "verification_work_id": "verify-claim",
                    "verifier_logical_agent_id": "agent-verifier",
                    "verifier_agent_id": "agent-verifier",
                    "verifier_harness": "claude",
                    "collector_logical_agent_ids": ["agent-collector"],
                    "collector_harnesses": ["grok"],
                }
            )
        with self.assertRaisesRegex(
            ValueError,
            "verification_disposition_duplicate_verifier_agent",
        ):
            VerificationDisposition.from_mapping(
                {
                    "schema_version": 1,
                    "disposition_id": "disposition-duplicate",
                    "claim_id": "claim-critical",
                    "assignment_id": "assignment-duplicate",
                    "disposition": "unverified",
                    "reason": "ambiguous verifier identity",
                    "evidence_ids": [],
                    "contradiction_ids": [],
                    "verifier_logical_agent_id": "agent-verifier",
                    "verifier_agent_id": "agent-verifier",
                    "verifier_harness": "claude",
                    "source_ids": [],
                    "excerpt_ids": [],
                }
            )

    def test_assignment_policy_controls_identity_independence_and_supersession(
        self,
    ) -> None:
        register = ResearchEvidenceRegister(
            verification_policy=CriticalityPolicy(
                require_independent_logical_agent=False,
                require_harness_separation=False,
            )
        )
        claim = register.admit_claim(
            TypedClaim(
                claim_id="claim-policy",
                claim_type="factual",
                text="Policy controls independence.",
                required_conclusion_ids=("conclusion-main",),
            )
        )
        first = register.admit_verification_assignment(
            VerificationAssignment(
                assignment_id="assignment-policy-first",
                claim_id=claim.claim_id,
                verification_work_id="verify-policy-first",
                verifier_logical_agent_id="agent-collector",
                verifier_harness="grok",
                collector_logical_agent_ids=("agent-collector",),
                collector_harnesses=("grok",),
            )
        )
        self.assertTrue(first.current)
        superseded = register.supersede_verification_assignment(
            first.assignment_id
        )
        self.assertFalse(superseded.current)
        replacement = register.admit_verification_assignment(
            VerificationAssignment(
                assignment_id="assignment-policy-replacement",
                claim_id=claim.claim_id,
                verification_work_id="verify-policy-replacement",
                verifier_logical_agent_id="agent-collector",
                verifier_harness="grok",
                collector_logical_agent_ids=("agent-collector",),
                collector_harnesses=("grok",),
            )
        )
        self.assertTrue(replacement.current)

    def test_deserialization_requires_the_pinned_verification_policy(self) -> None:
        policy = CriticalityPolicy(
            critical_claim_types=("factual",),
            required_conclusion_ids=("conclusion-main",),
        )
        register = ResearchEvidenceRegister(verification_policy=policy)
        serialized = register.to_dict()
        serialized["verification_policy"] = CriticalityPolicy(
            critical_claim_types=("causal",),
            required_conclusion_ids=("conclusion-main",),
        ).to_dict()

        with self.assertRaisesRegex(
            ValueError,
            "verification_policy_pinned_mismatch",
        ):
            ResearchEvidenceRegister.from_mapping(
                serialized,
                verification_policy=policy,
            )

    def test_schema_v1_register_without_verification_fields_still_loads(self) -> None:
        legacy = {
            "schema_version": 1,
            "sources": [],
            "excerpts": [],
            "claims": [],
            "relations": [],
            "creditable_relation_ids": [],
        }

        restored = ResearchEvidenceRegister.from_mapping(legacy)

        self.assertEqual(restored.claims, ())
        self.assertEqual(restored.verification_status(), ())


def _register_with_opposing_evidence() -> tuple[
    ResearchEvidenceRegister,
    TypedClaim,
    EvidenceRelation,
    EvidenceRelation,
]:
    register = ResearchEvidenceRegister()
    sources = []
    excerpts = []
    for source_id, agent, harness, text, order in (
        ("source-collector", "agent-collector", "grok", "supports", 1),
        ("source-verifier", "agent-verifier", "claude", "contradicts", 2),
    ):
        payload = text.encode("utf-8")
        source = SourceReceipt.from_retrieval(
            requested_url=f"https://example.test/{source_id}",
            final_url=f"https://example.test/{source_id}",
            redirect_chain=(),
            retrieved_content=payload,
            retrieval_order=order,
            retrieved_at=1_700_000_000 + order,
            content_type="text/plain",
            role="collector" if order == 1 else "verifier",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id=f"attempt-{order}",
            fencing_token=f"fence-{order}",
            harness=harness,
            worker="collector" if order == 1 else "verifier",
            agent=agent,
            pane=f"pane:{agent}",
            source_id=source_id,
        )
        excerpt = ExcerptReceipt.from_text(source, text=text, selector="body")
        register.admit_source(source)
        register.admit_excerpt(excerpt)
        sources.append(source)
        excerpts.append(excerpt)
    claim = register.admit_claim(
        TypedClaim(
            claim_id="claim-critical",
            claim_type="factual",
            text="The disputed fact is true.",
            required_conclusion_ids=("conclusion-main",),
        )
    )
    supporting = register.admit_relation(
        EvidenceRelation(
            relation_id="relation-support",
            claim_id=claim.claim_id,
            relation="support",
            source_id=sources[0].source_id,
            excerpt_id=excerpts[0].excerpt_id,
        )
    )
    contradiction = register.admit_relation(
        EvidenceRelation(
            relation_id="relation-contradiction",
            claim_id=claim.claim_id,
            relation="contradiction",
            source_id=sources[1].source_id,
            excerpt_id=excerpts[1].excerpt_id,
        )
    )
    verification_source = SourceReceipt.from_retrieval(
        requested_url="https://example.test/verification",
        final_url="https://example.test/verification",
        redirect_chain=(),
        retrieved_content=b"matching verification",
        retrieval_order=3,
        retrieved_at=1_700_000_003,
        content_type="text/plain",
        role="verifier",
        run_id="run-1",
        work_id="verify-claim",
        attempt_id="attempt-3",
        fencing_token="fence-3",
        harness="claude",
        worker="verifier",
        agent="agent-verifier",
        pane="pane:agent-verifier",
        source_id="source-verification",
    )
    verification_excerpt = ExcerptReceipt.from_text(
        verification_source,
        text="matching verification",
        selector="body",
    )
    register.admit_source(verification_source)
    register.admit_excerpt(verification_excerpt)
    return register, claim, supporting, contradiction


def _invoke(*argv: str) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(list(argv))
    payload = json.loads(output.getvalue())
    if not isinstance(payload, dict):
        raise AssertionError(payload)
    return exit_code, payload


def _workflow(root: Path) -> Path:
    workflow = root / "workflow.toml"
    workflow.write_text(
        """
schema_version = 2
name = "research-verification-case"
workspace = "."
state_db = ".orchestrator/state.db"
runtime_dir = ".orchestrator/runs/research-verification-case"

[coordinator]
poll_seconds = 1
max_parallel = 2
lease_seconds = 120
max_attempts = 2
agent_timeout_seconds = 10

[executor]
kind = "research-synthesis"

[research.decomposition]
facets = ["scope"]
perspectives = ["operator"]
required_cells = [{facet = "scope", perspective = "operator"}]

[research.verification]
critical_claim_types = ["factual", "quantitative", "causal"]
required_conclusions = ["conclusion-main"]
require_harness_separation = true
forbid_source_reuse = true

[research.roles]
collector = "collector"
verifier = "verifier"

[[workers]]
name = "collector"
harness = "grok"
capabilities = ["research.collect"]
replicas = 1

[[workers]]
name = "verifier"
harness = "claude"
capabilities = ["research.verify"]
replicas = 1
""".strip(),
        encoding="utf-8",
    )
    return workflow


if __name__ == "__main__":
    unittest.main()
