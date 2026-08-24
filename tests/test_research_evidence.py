from __future__ import annotations

import hashlib
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory

from herdr_orchestrator.cli import main
from herdr_orchestrator.research_executor import (
    EvidenceRelation,
    ExcerptReceipt,
    ResearchEvidenceError,
    ResearchEvidenceRegister,
    SourceReceipt,
    TypedClaim,
)


class ResearchEvidenceReceiptTests(unittest.TestCase):
    def test_admitted_source_and_excerpt_are_digest_bound_and_attributed(self) -> None:
        payload = b'{"answer":"bounded"}'
        source = SourceReceipt.from_retrieval(
            requested_url="https://example.test/research",
            final_url="https://example.test/research",
            redirect_chain=(),
            retrieved_content=payload,
            retrieval_order=1,
            retrieved_at=1_700_000_000,
            content_type="application/json",
            role="collector",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id="attempt-1",
            fencing_token="fence-1",
            harness="grok",
            worker="collector",
            agent="research-grok-1",
            pane="pane:research-grok-1",
        )
        excerpt = ExcerptReceipt.from_text(
            source,
            text='{"answer":"bounded"}',
            selector="$.answer",
        )

        self.assertEqual(source.method, "GET")
        self.assertEqual(source.payload_digest, "sha256:" + hashlib.sha256(payload).hexdigest())
        self.assertEqual(source.final_url, "https://example.test/research")
        self.assertEqual(source.role, "collector")
        self.assertEqual(excerpt.source_id, source.source_id)
        self.assertEqual(excerpt.source_digest, source.payload_digest)
        self.assertEqual(excerpt.excerpt_digest, "sha256:" + hashlib.sha256(
            b'{"answer":"bounded"}'
        ).hexdigest())
        self.assertEqual(
            SourceReceipt.from_mapping(source.to_dict()),
            source,
        )
        self.assertEqual(
            ExcerptReceipt.from_mapping(excerpt.to_dict()),
            excerpt,
        )

    def test_later_retrieval_cannot_rewrite_an_admitted_source_receipt(self) -> None:
        first = SourceReceipt.from_retrieval(
            requested_url="https://example.test/page",
            final_url="https://example.test/page",
            redirect_chain=(),
            retrieved_content=b"version one",
            retrieval_order=1,
            retrieved_at=1_700_000_000,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id="attempt-1",
            fencing_token="fence-1",
            harness="claude",
            worker="collector",
            agent="research-claude-1",
            pane="pane:research-claude-1",
            source_id="source-page",
        )
        second = SourceReceipt.from_retrieval(
            requested_url="https://example.test/page",
            final_url="https://example.test/page",
            redirect_chain=(),
            retrieved_content=b"version two",
            retrieval_order=2,
            retrieved_at=1_700_000_001,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id="attempt-2",
            fencing_token="fence-2",
            harness="claude",
            worker="collector",
            agent="research-claude-1",
            pane="pane:research-claude-1",
            source_id="source-page",
        )
        ledger = ResearchEvidenceRegister()
        ledger.admit_source(first)

        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "source_receipt_immutable_conflict",
        ):
            ledger.admit_source(second)

        self.assertEqual(ledger.sources, (first,))

    def test_unavailable_retrieval_is_receipted_without_fabricating_excerpt(self) -> None:
        source = SourceReceipt.from_retrieval(
            requested_url="https://example.test/unavailable",
            final_url="https://example.test/unavailable",
            redirect_chain=(),
            retrieved_content=None,
            retrieval_order=1,
            retrieved_at=1_700_000_000,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id="attempt-1",
            fencing_token="fence-1",
            harness="grok",
            worker="collector",
            agent="research-grok-1",
            pane="pane:research-grok-1",
            status=503,
            outcome="unavailable",
        )
        ledger = ResearchEvidenceRegister()
        ledger.admit_source(source)

        self.assertEqual(source.outcome, "unavailable")
        self.assertEqual(source.payload_digest, "sha256:" + hashlib.sha256(b"").hexdigest())
        self.assertEqual(ledger.excerpts, ())
        self.assertEqual(ledger.creditable_relation_ids, ())

    def test_snippets_and_digest_mismatches_receive_no_evidence_credit(self) -> None:
        source = SourceReceipt.from_retrieval(
            requested_url="https://example.test/page",
            final_url="https://example.test/page",
            redirect_chain=(),
            retrieved_content=b"full page",
            retrieval_order=1,
            retrieved_at=1_700_000_000,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id="attempt-1",
            fencing_token="fence-1",
            harness="grok",
            worker="collector",
            agent="research-grok-1",
            pane="pane:research-grok-1",
        )
        ledger = ResearchEvidenceRegister()
        ledger.admit_source(source)

        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "excerpt_search_snippet_not_evidence",
        ):
            ledger.admit_excerpt(
                {
                    "schema_version": 1,
                    "excerpt_id": "excerpt-snippet",
                    "source_id": source.source_id,
                    "source_digest": source.payload_digest,
                    "capture_kind": "search_snippet",
                    "text": "snippet",
                    "selector": "result[0]",
                    "offset_start": None,
                    "offset_end": None,
                    "normalization_version": 1,
                    "excerpt_digest": "sha256:" + hashlib.sha256(b"snippet").hexdigest(),
                }
            )

        valid = ExcerptReceipt.from_text(source, text="full page", selector="body")
        invalid = valid.to_dict()
        invalid["excerpt_digest"] = "sha256:" + "0" * 64
        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "excerpt_digest_mismatch",
        ):
            ledger.admit_excerpt(invalid)

        self.assertEqual(ledger.excerpts, ())
        self.assertEqual(ledger.creditable_relation_ids, ())

    def test_bare_url_and_out_of_bounds_offsets_are_not_excerpts(self) -> None:
        source = SourceReceipt.from_retrieval(
            requested_url="https://example.test/page",
            final_url="https://example.test/page",
            redirect_chain=(),
            retrieved_content=b"full page",
            retrieval_order=1,
            retrieved_at=1_700_000_000,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id="attempt-1",
            fencing_token="fence-1",
            harness="grok",
            worker="collector",
            agent="research-grok-1",
            pane="pane:research-grok-1",
        )
        ledger = ResearchEvidenceRegister()
        ledger.admit_source(source)

        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "excerpt_bare_url_not_evidence",
        ):
            ledger.admit_excerpt(
                ExcerptReceipt.from_text(
                    source,
                    text=source.final_url,
                    selector="body",
                )
            )

    def test_excerpt_from_retrieved_content_is_bound_to_source_payload(self) -> None:
        source = SourceReceipt.from_retrieval(
            requested_url="https://example.test/page",
            final_url="https://example.test/page",
            redirect_chain=(),
            retrieved_content=b"full page",
            retrieval_order=1,
            retrieved_at=1_700_000_000,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id="attempt-1",
            fencing_token="fence-1",
            harness="grok",
            worker="collector",
            agent="research-grok-1",
            pane="pane:research-grok-1",
        )

        excerpt = ExcerptReceipt.from_content(
            source,
            source_content=b"full page",
            text="full page",
            selector="body",
        )
        self.assertEqual(excerpt.source_digest, source.payload_digest)
        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "excerpt_offsets_out_of_bounds",
        ):
            ExcerptReceipt.from_text(
                source,
                text="full page",
                offset_start=0,
                offset_end=100,
            )
        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "excerpt_source_payload_digest_mismatch",
        ):
            ExcerptReceipt.from_content(
                source,
                source_content=b"changed page",
                text="changed page",
                selector="body",
            )
        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "excerpt_text_not_in_source",
        ):
            ExcerptReceipt.from_content(
                source,
                source_content=b"full page",
                text="not present",
                selector="body",
            )


class ResearchTypedClaimTests(unittest.TestCase):
    def test_all_supported_claim_types_and_relations_are_admitted(self) -> None:
        source = SourceReceipt.from_retrieval(
            requested_url="https://example.test/facts",
            final_url="https://example.test/facts",
            redirect_chain=(),
            retrieved_content=b"facts",
            retrieval_order=1,
            retrieved_at=1_700_000_000,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id="attempt-1",
            fencing_token="fence-1",
            harness="grok",
            worker="collector",
            agent="research-grok-1",
            pane="pane:research-grok-1",
        )
        excerpt = ExcerptReceipt.from_text(source, text="facts", selector="body")
        ledger = ResearchEvidenceRegister()
        ledger.admit_source(source)
        ledger.admit_excerpt(excerpt)

        factual = TypedClaim(
            claim_id="claim-factual",
            claim_type="factual",
            text="The service is bounded.",
        )
        quantitative = TypedClaim(
            claim_id="claim-quantitative",
            claim_type="quantitative",
            text="The service handles 10 requests.",
            quantity=10,
            unit="requests",
            value_range={"min": 10, "max": 10},
            time="2026-08-24",
            population="sampled requests",
        )
        causal = TypedClaim(
            claim_id="claim-causal",
            claim_type="causal",
            text="A bounded queue prevents overflow.",
            causal_direction="queue bound -> overflow prevention",
        )
        inference = TypedClaim(
            claim_id="claim-inference",
            claim_type="inference",
            text="The service is likely stable.",
            premise_ids=("claim-factual",),
            evidence_ids=("relation-support",),
        )
        recommendation = TypedClaim(
            claim_id="claim-recommendation",
            claim_type="recommendation",
            text="Keep the queue bound.",
            premise_ids=("claim-causal",),
            evidence_ids=("relation-support",),
        )
        for claim in (factual, quantitative, causal):
            ledger.admit_claim(claim)

        support = EvidenceRelation(
            relation_id="relation-support",
            claim_id=factual.claim_id,
            relation="support",
            source_id=source.source_id,
            excerpt_id=excerpt.excerpt_id,
        )
        context = EvidenceRelation(
            relation_id="relation-context",
            claim_id=recommendation.claim_id,
            relation="context",
            source_id=source.source_id,
            excerpt_id=excerpt.excerpt_id,
        )
        ledger.admit_relation(support)
        ledger.admit_claim(inference)
        ledger.admit_claim(recommendation)
        ledger.admit_relation(context)

        self.assertEqual(
            {claim.claim_type for claim in ledger.claims},
            {"factual", "quantitative", "causal", "inference", "recommendation"},
        )
        self.assertEqual(
            {relation.relation for relation in ledger.relations},
            {"support", "context"},
        )
        self.assertEqual(
            set(ledger.creditable_relation_ids),
            {"relation-support", "relation-context"},
        )

    def test_unknown_types_relations_and_unadmitted_ids_do_not_enter_register(self) -> None:
        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "claim_type_unsupported",
        ):
            TypedClaim(
                claim_id="claim-unknown",
                claim_type="opinion",
                text="Not an admitted type.",
            )

        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "claim_quantitative_fields_required",
        ):
            TypedClaim(
                claim_id="claim-incomplete",
                claim_type="quantitative",
                text="Missing machine fields.",
                quantity=10,
            )

        ledger = ResearchEvidenceRegister()
        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "evidence_relation_unknown_relation",
        ):
            ledger.admit_relation(
                {
                    "schema_version": 1,
                    "relation_id": "relation-invalid",
                    "claim_id": "claim-missing",
                    "relation": "depends_on",
                    "source_id": "source-missing",
                    "excerpt_id": "excerpt-missing",
                }
            )

        self.assertEqual(ledger.claims, ())
        self.assertEqual(ledger.relations, ())
        self.assertEqual(ledger.creditable_relation_ids, ())

    def test_register_round_trip_recomputes_creditable_relations(self) -> None:
        source = SourceReceipt.from_retrieval(
            requested_url="https://example.test/facts",
            final_url="https://example.test/facts",
            redirect_chain=(),
            retrieved_content=b"facts",
            retrieval_order=1,
            retrieved_at=1_700_000_000,
            content_type="text/plain",
            role="collector",
            run_id="run-1",
            work_id="collect-scope",
            attempt_id="attempt-1",
            fencing_token="fence-1",
            harness="grok",
            worker="collector",
            agent="research-grok-1",
            pane="pane:research-grok-1",
        )
        excerpt = ExcerptReceipt.from_text(source, text="facts", selector="body")
        register = ResearchEvidenceRegister()
        register.admit_source(source)
        register.admit_excerpt(excerpt)
        claim = register.admit_claim(
            TypedClaim(
                claim_id="claim-factual",
                claim_type="factual",
                text="Facts are admitted.",
            )
        )
        register.admit_relation(
            EvidenceRelation(
                relation_id="relation-support",
                claim_id=claim.claim_id,
                relation="support",
                source_id=source.source_id,
                excerpt_id=excerpt.excerpt_id,
            )
        )

        restored = ResearchEvidenceRegister.from_mapping(register.to_dict())
        self.assertEqual(restored.to_dict(), register.to_dict())
        tampered = register.to_dict()
        tampered["creditable_relation_ids"] = []
        with self.assertRaisesRegex(
            ResearchEvidenceError,
            "creditable_relation_ids_mismatch",
        ):
            ResearchEvidenceRegister.from_mapping(tampered)


class ResearchEvidenceCliTests(unittest.TestCase):
    def test_public_fixture_projects_attributed_receipts_and_typed_register(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            exit_code, payload = _invoke(
                "research",
                "evidence-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "valid",
            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(payload["success"])
        self.assertEqual(payload["route"], "research.evidence-fixture")
        self.assertEqual(payload["code"], "evidence_admitted")
        evidence = payload["evidence"]
        self.assertEqual(len(evidence["sources"]), 1)
        self.assertEqual(len(evidence["excerpts"]), 1)
        self.assertEqual(evidence["claims"][0]["claim_type"], "factual")
        self.assertEqual(evidence["relations"][0]["relation"], "support")
        self.assertEqual(
            evidence["creditable_relation_ids"],
            [evidence["relations"][0]["relation_id"]],
        )
        source = evidence["sources"][0]
        self.assertEqual(source["method"], "GET")
        self.assertEqual(source["harness"], "grok")
        self.assertTrue(source["requested_url"].startswith("https://"))
        self.assertTrue(source["receipt_digest"].startswith("sha256:"))

    def test_public_fixture_rejects_snippet_without_semantic_credit(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            exit_code, payload = _invoke(
                "research",
                "evidence-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "snippet-only",
            )

        self.assertEqual(exit_code, 0)
        self.assertFalse(payload["success"])
        self.assertEqual(payload["code"], "excerpt_search_snippet_not_evidence")
        self.assertEqual(len(payload["evidence"]["sources"]), 1)
        self.assertEqual(payload["evidence"]["excerpts"], [])
        self.assertEqual(payload["evidence"]["claims"], [])
        self.assertEqual(payload["evidence"]["creditable_relation_ids"], [])

    def test_public_fixture_rejects_unadmitted_relation_and_claim_type(self) -> None:
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            relation_exit, relation = _invoke(
                "research",
                "evidence-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "unknown-relation",
            )
            claim_exit, claim = _invoke(
                "research",
                "evidence-fixture",
                "--workflow",
                str(workflow),
                "--case",
                "unknown-claim-type",
            )

        self.assertEqual(relation_exit, 0)
        self.assertFalse(relation["success"])
        self.assertEqual(relation["code"], "evidence_relation_unknown_relation")
        self.assertEqual(relation["evidence"]["creditable_relation_ids"], [])
        self.assertEqual(claim_exit, 0)
        self.assertFalse(claim["success"])
        self.assertEqual(claim["code"], "claim_type_unsupported")
        self.assertEqual(claim["evidence"]["creditable_relation_ids"], [])

    def test_public_negative_fixture_matrix_is_bounded_and_non_crediting(self) -> None:
        cases = (
            "changed-retrieval",
            "bare-url",
            "offset-out-of-bounds",
            "malformed-excerpt",
            "unknown-source-key",
            "unknown-excerpt-key",
            "unknown-claim-key",
            "unknown-relation-key",
            "missing-excerpt",
            "oversize-excerpt",
            "digest-mismatch",
            "malformed-claim",
            "unknown-claim-type",
            "unknown-relation",
            "unadmitted-source",
            "unadmitted-excerpt",
            "unadmitted-premise",
            "unadmitted-evidence",
        )
        with TemporaryDirectory() as temporary:
            workflow = _workflow(Path(temporary))
            for case in cases:
                exit_code, payload = _invoke(
                    "research",
                    "evidence-fixture",
                    "--workflow",
                    str(workflow),
                    "--case",
                    case,
                )
                self.assertEqual(exit_code, 0, case)
                self.assertEqual(payload["fixture_case_id"], case)
                if case == "changed-retrieval":
                    self.assertTrue(payload["success"], case)
                    self.assertEqual(len(payload["evidence"]["sources"]), 2)
                else:
                    self.assertFalse(payload["success"], case)
                    self.assertEqual(
                        payload["evidence"]["creditable_relation_ids"],
                        [],
                        case,
                    )


def _invoke(*argv: str) -> tuple[int, dict[str, object]]:
    output = StringIO()
    with redirect_stdout(output):
        exit_code = main(list(argv))
    value = json.loads(output.getvalue())
    if not isinstance(value, dict):
        raise AssertionError(value)
    return exit_code, value


def _workflow(root: Path) -> Path:
    workflow = root / "workflow.toml"
    workflow.write_text(
        """
schema_version = 2
name = "research-evidence-case"
workspace = "."
state_db = ".orchestrator/state.db"
runtime_dir = ".orchestrator/runs/research-evidence-case"

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

[research.roles]
collector = "collector"

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
