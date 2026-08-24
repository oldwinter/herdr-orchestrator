from __future__ import annotations

import argparse
import json
import shutil
import tempfile
from dataclasses import replace
from pathlib import Path

from herdr_orchestrator.config import ConfigError
from herdr_orchestrator.executor_artifacts import ARTIFACT_CONTRACT_VERSION, digest_bytes
from herdr_orchestrator.executor_kernel import ExecutionKernel
from herdr_orchestrator.executor_protocol import MANIFEST_DIGEST_FIELDS
from herdr_orchestrator.executor_store import ExecutorStore, ExecutorStoreError
from herdr_orchestrator.model import WorkflowConfig
from herdr_orchestrator.research_executor import (
    ResearchConfig,
    ResearchEvidenceError,
    ResearchEvidenceRegister,
    ResearchInputError,
    EvidenceRelation,
    ExcerptReceipt,
    SourceReceipt,
    TypedClaim,
    VerificationAssignment,
    VerificationDisposition,
    build_research_view,
    classify_input,
    decompose_question,
    parse_research_input,
)


def _research_kernel(config: WorkflowConfig, store: ExecutorStore) -> ExecutionKernel:
    if config.runtime_dir is None or config.executor is None:
        raise ConfigError("schema_v2_runtime_missing")
    slots = {
        worker.name: tuple(
            f"ho-{worker.harness.value}-{index:02d}"
            for index in range(1, worker.replicas + 1)
        )
        for worker in config.workers
    }
    return ExecutionKernel(
        store,
        max_parallel=config.coordinator.max_parallel,
        replica_slots=slots,
        workspace=config.workspace,
        runtime_dir=config.runtime_dir,
        lease_seconds=config.coordinator.lease_seconds,
        max_attempts=config.coordinator.max_attempts,
    )


def _research_select_run(
    store: ExecutorStore,
    workflow: str,
    *,
    run_id: str | None,
    dedupe_key: str | None,
) -> str:
    if run_id is not None:
        record = store.require_run(run_id)
        if record.workflow != workflow:
            raise ExecutorStoreError("run_not_found")
        return run_id
    if dedupe_key is not None:
        record = store.find_run(workflow, dedupe_key)
        if record is None:
            raise ExecutorStoreError("run_not_found")
        return record.run_id
    runs = store.runs(workflow)
    if not runs:
        raise ExecutorStoreError("run_not_found")
    if len(runs) != 1:
        raise ExecutorStoreError("run_selector_ambiguous")
    return runs[0].run_id


def research_command(config: WorkflowConfig, args: argparse.Namespace) -> int:
    command = args.research_command
    if command in {"evidence-fixture", "verification-fixture"}:
        return _research_evidence_fixture(
            config,
            args.case,
            route=f"research.{command}",
        )
    if command == "start":
        if args.input_json is not None and args.question is not None:
            raise ResearchInputError("research_input_duplicate_payload")
        if args.input_json is not None:
            try:
                raw_input = json.loads(args.input_json)
            except json.JSONDecodeError as exc:
                raise ResearchInputError("research_input_invalid_json") from exc
            return _research_start(
                config,
                dedupe_key=args.dedupe_key,
                input_value=raw_input,
            )
        if args.question is not None and args.question.lstrip().startswith("{"):
            try:
                raw_input = json.loads(args.question)
            except json.JSONDecodeError as exc:
                raise ResearchInputError("research_input_invalid_json") from exc
            return _research_start(
                config,
                dedupe_key=args.dedupe_key,
                input_value=raw_input,
            )
        return _research_start(
            config,
            dedupe_key=args.dedupe_key,
            input_value={
                "version": 1,
                "kind": "public-question",
                "question": args.question or "",
            },
        )
    if command == "status":
        return _research_status(config, run_id=args.run_id)
    if command == "inspect":
        return _research_inspect(
            config,
            run_id=args.run_id,
            dedupe_key=args.dedupe_key,
        )
    if command == "resume":
        return _research_resume(
            config,
            run_id=args.run_id,
            input_id=args.input_id,
            material=args.input,
        )
    if command == "export":
        return _research_export(
            config,
            run_id=args.run_id,
            output=args.output,
        )
    raise ConfigError(f"research_route_unknown: {command}")


_RESEARCH_EVIDENCE_FIXTURE_CASES = frozenset(
    {
        "valid",
        "changed-retrieval",
        "snippet-only",
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
        "contradiction-pack",
        "contested",
        "critical-unverified",
        "critical-independent",
        "critical-self-verification",
        "critical-downgrade",
        "disposition-history",
        "source-reuse",
        "unaccounted-contradiction",
        "stale-assignment",
    }
)
_RESEARCH_EVIDENCE_FIXTURE_ALIASES = {
    "search-snippet": "snippet-only",
    "snippet": "snippet-only",
    "search-snippet-only": "snippet-only",
    "bare-url-excerpt": "bare-url",
    "oversized-excerpt": "oversize-excerpt",
    "malformed-excerpt-receipt": "malformed-excerpt",
    "offsets-out-of-bounds": "offset-out-of-bounds",
    "changed-source": "changed-retrieval",
    "bad-excerpt-digest": "digest-mismatch",
    "bad-claim-type": "unknown-claim-type",
    "bad-relation": "unknown-relation",
    "contradictory-evidence": "contradiction-pack",
    "contested-claim": "contradiction-pack",
    "critical": "critical-independent",
    "critical-unverified-claim": "critical-unverified",
    "self-verification": "critical-self-verification",
    "criticality-downgrade": "critical-downgrade",
    "history": "disposition-history",
    "contradiction": "contradiction-pack",
    "support-contradiction": "contradiction-pack",
    "opposing-evidence": "contradiction-pack",
    "independent-verification": "critical-independent",
    "critical-self": "critical-self-verification",
    "reused-source": "source-reuse",
    "unaccounted": "unaccounted-contradiction",
}
_RESEARCH_VERIFICATION_FIXTURE_CASES = frozenset(
    {
        "contradiction-pack",
        "contested",
        "critical-unverified",
        "critical-independent",
        "critical-self-verification",
        "critical-downgrade",
        "disposition-history",
        "source-reuse",
        "unaccounted-contradiction",
        "stale-assignment",
    }
)


def _research_evidence_fixture(
    config: WorkflowConfig,
    case_id: str,
    *,
    route: str = "research.evidence-fixture",
) -> int:
    requested_case_id = case_id
    case_id = _RESEARCH_EVIDENCE_FIXTURE_ALIASES.get(case_id, case_id)
    if case_id not in _RESEARCH_EVIDENCE_FIXTURE_CASES:
        raise ConfigError(
            f"research_evidence_fixture_unknown_case:{requested_case_id}"
        )
    if route == "research.verification-fixture":
        return _research_evidence_fixture_in_state(
            config,
            requested_case_id=requested_case_id,
            case_id=case_id,
            route=route,
        )
    fixture_workspace = Path(
        tempfile.mkdtemp(
            prefix=".research-evidence-fixture-",
            dir=config.workspace,
        )
    )
    isolated = replace(
        config,
        workspace=fixture_workspace,
        state_db=fixture_workspace / "state.db",
        runtime_dir=fixture_workspace / "runtime",
    )
    try:
        return _research_evidence_fixture_in_state(
            isolated,
            requested_case_id=requested_case_id,
            case_id=case_id,
            route=route,
        )
    finally:
        shutil.rmtree(fixture_workspace, ignore_errors=True)


def _research_evidence_fixture_in_state(
    config: WorkflowConfig,
    *,
    requested_case_id: str,
    case_id: str,
    route: str = "research.evidence-fixture",
) -> int:
    if config.executor is None:
        raise ConfigError("executor_missing")
    store = ExecutorStore(config.state_db)
    kernel = _research_kernel(config, store)
    worker = config.workers[0]
    dedupe_case_id = (
        case_id
        if route == "research.verification-fixture"
        else requested_case_id
    )
    run_id, created = store.create_run(
        config.name,
        "research-synthesis",
        f"research-evidence-fixture-{dedupe_case_id}",
        state="pending",
        workflow_definition={
            "name": config.name,
            "schema_version": 2,
            "fixture_case_id": requested_case_id,
        },
        config_definition={"fixture": requested_case_id},
        input_value={"question": "evidence fixture"},
        route_definition={
            "worker": worker.name,
            "harness": worker.harness.value,
        },
        contract_definition={"version": "research-evidence-v1"},
        executor_definition={"kind": "research-synthesis", "version": 1},
        artifact_contract_definition={
            "version": ARTIFACT_CONTRACT_VERSION,
            "schema_version": 1,
        },
    )
    if case_id in _RESEARCH_VERIFICATION_FIXTURE_CASES:
        if not created:
            return _replay_verification_fixture(
                config,
                store=store,
                kernel=kernel,
                run_id=run_id,
                requested_case_id=requested_case_id,
                route=route,
            )
        return _research_verification_fixture_in_state(
            config,
            store=store,
            kernel=kernel,
            run_id=run_id,
            requested_case_id=requested_case_id,
            case_id=case_id,
            route=route,
        )
    ledger = ResearchEvidenceRegister()
    source = SourceReceipt.from_retrieval(
        requested_url="https://example.test/research",
        final_url="https://example.test/research",
        redirect_chain=(),
        retrieved_content=b'{"answer":"bounded"}',
        retrieval_order=1,
        retrieved_at=1_700_000_000,
        content_type="application/json",
        role="collector",
        run_id=run_id,
        work_id="collect-scope-operator",
        attempt_id="attempt-1",
        fencing_token="fixture-fence-1",
        harness=worker.harness.value,
        worker=worker.name,
        agent="research-grok-1",
        pane="pane:research-grok-1",
        source_id="source-research-page",
    )
    if case_id == "changed-retrieval":
        ledger.admit_source(source)
        later = SourceReceipt.from_retrieval(
            requested_url=source.requested_url,
            final_url=source.final_url,
            redirect_chain=source.redirect_chain,
            retrieved_content=b'{"answer":"changed"}',
            retrieval_order=2,
            retrieved_at=1_700_000_001,
            content_type=source.content_type,
            role=source.role,
            run_id=run_id,
            work_id=source.work_id,
            attempt_id="attempt-2",
            fencing_token="fixture-fence-2",
            harness=worker.harness.value,
            worker=worker.name,
            agent="research-grok-1",
            pane="pane:research-grok-1",
        )
        ledger.admit_source(later)
        outcome_code = "new_source_observation"
        reason = "later_retrieval_created_new_immutable_receipt"
    else:
        ledger.admit_source(source)
        outcome_code = "evidence_rejected"
        reason = "typed_evidence_required"
        try:
            if case_id in {
                "snippet-only",
                "bare-url",
                "offset-out-of-bounds",
                "malformed-excerpt",
                "unknown-excerpt-key",
                "unknown-claim-key",
                "oversize-excerpt",
                "digest-mismatch",
                "malformed-claim",
                "unknown-claim-type",
            }:
                _raise_fixture_case(
                    case_id,
                    source=source,
                    ledger=ledger,
                )
            else:
                excerpt = ExcerptReceipt.from_text(
                    source,
                    text='{"answer":"bounded"}',
                    selector="$.answer",
                )
                ledger.admit_excerpt(excerpt)
                factual = TypedClaim(
                    claim_id="claim-bounded",
                    claim_type="factual",
                    text="The answer is bounded.",
                )
                ledger.admit_claim(factual)
                if case_id == "valid":
                    relation = EvidenceRelation(
                        relation_id="relation-support",
                        claim_id=factual.claim_id,
                        relation="support",
                        source_id=source.source_id,
                        excerpt_id=excerpt.excerpt_id,
                    )
                    ledger.admit_relation(relation)
                    outcome_code = "evidence_admitted"
                    reason = "source_excerpt_claim_relation_admitted"
                else:
                    _raise_fixture_case(
                        case_id,
                        source=source,
                        excerpt=excerpt,
                        ledger=ledger,
                    )
        except ResearchEvidenceError as exc:
            outcome_code = str(exc).split(":", 1)[0]
            reason = str(exc)

    evidence = ledger.to_dict()
    kernel.append_event(
        run_id,
        event_key=f"research-evidence-fixture:{requested_case_id}",
        event_type="research_evidence_fixture",
        payload={
            "fixture_case_id": requested_case_id,
            "outcome_code": outcome_code,
            "evidence": evidence,
        },
        error_code=None if outcome_code in {
            "evidence_admitted",
            "new_source_observation",
        } else outcome_code,
    )
    inspected = kernel.inspect_run(run_id)
    print(
        json.dumps(
            {
                "schema_version": 2,
                "executor": "research-synthesis",
                "route": route,
                "success": outcome_code in {
                    "evidence_admitted",
                    "new_source_observation",
                },
                "fixture_case_id": requested_case_id,
                "bound": {
                    "max_source_url_bytes": 4096,
                    "max_excerpt_bytes": 16_384,
                    "max_claim_bytes": 16_384,
                    "one_submission": True,
                },
                "run_id": run_id,
                "execution_state": store.require_run(run_id).state,
                "code": outcome_code,
                "reason": reason,
                "coverage_credit": len(evidence["creditable_relation_ids"]),
                "evidence": evidence,
                "events": len(inspected["events"]),
            },
            sort_keys=True,
        )
    )
    return 0


def _replay_verification_fixture(
    config: WorkflowConfig,
    *,
    store: ExecutorStore,
    kernel: ExecutionKernel,
    run_id: str,
    requested_case_id: str,
    route: str,
) -> int:
    event = next(
        (
            item
            for item in reversed(kernel.list_events(run_id))
            if item.event_type == "research_verification_fixture"
        ),
        None,
    )
    if event is None or not isinstance(event.payload, dict):
        raise ExecutorStoreError("verification_fixture_event_missing")
    raw_evidence = event.payload.get("evidence")
    if not isinstance(raw_evidence, dict):
        raise ExecutorStoreError("verification_fixture_evidence_missing")
    research_config = ResearchConfig.from_mapping(
        config.executor.settings if config.executor is not None else {}
    )
    register = ResearchEvidenceRegister.from_mapping(
        raw_evidence,
        verification_policy=research_config.verification,
    )
    outcome_code = str(event.payload.get("outcome_code", "unknown"))
    verification = register.claim_status(register.claims[0].claim_id)
    current_disposition = next(
        (
            item.to_dict()
            for item in register.verification_dispositions
            if item.current
        ),
        None,
    )
    run = store.require_run(run_id)
    inspected = kernel.inspect_run(run_id)
    print(
        json.dumps(
            {
                "schema_version": 2,
                "executor": "research-synthesis",
                "route": route,
                "success": outcome_code
                in {"claim_contested", "critical_claim_verified"},
                "fixture_case_id": requested_case_id,
                "bound": {
                    "max_submissions": 1,
                    "max_dispositions": 2,
                    "independent_logical_agent_required": (
                        research_config.verification.require_independent_logical_agent
                    ),
                    "harness_separation_required": (
                        research_config.verification.require_harness_separation
                    ),
                },
                "run_id": run_id,
                "execution_state": run.state,
                "code": outcome_code,
                "reason": str(event.payload.get("reason", outcome_code)),
                "verification": verification,
                "verification_assignment": event.payload.get(
                    "verification_assignment"
                ),
                "verification_disposition": current_disposition,
                "evidence": register.to_dict(),
                "events": len(inspected["events"]),
                "idempotent": True,
            },
            sort_keys=True,
        )
    )
    return 0


def _research_verification_fixture_in_state(
    config: WorkflowConfig,
    *,
    store: ExecutorStore,
    kernel: ExecutionKernel,
    run_id: str,
    requested_case_id: str,
    case_id: str,
    route: str,
) -> int:
    if config.executor is None:
        raise ConfigError("executor_missing")
    research_config = ResearchConfig.from_mapping(config.executor.settings)
    workers_by_name = {item.name: item for item in config.workers}
    collector_name = research_config.roles.get("collector")
    verifier_name = (
        research_config.roles.get("verifier")
        or research_config.roles.get("independent_verifier")
    )
    collector = workers_by_name.get(collector_name) or config.workers[0]
    verifier = workers_by_name.get(verifier_name)
    if verifier is None:
        verifier = config.workers[1] if len(config.workers) > 1 else collector

    register = ResearchEvidenceRegister(
        verification_policy=research_config.verification,
    )
    kernel.add_work_items(
        run_id,
        [
            {
                "work_id": "collect-support",
                "worker": collector.name,
                "harness": collector.harness.value,
                "payload": {
                    "research_kind": "collection",
                    "cell_id": "scope:operator",
                    "assigned_path": "evidence/support.json",
                    "lineage": ["collect-support"],
                },
            },
            {
                "work_id": "collect-contradiction",
                "worker": collector.name,
                "harness": collector.harness.value,
                "payload": {
                    "research_kind": "collection",
                    "cell_id": "scope:operator",
                    "assigned_path": "evidence/contradiction.json",
                    "lineage": ["collect-contradiction"],
                },
            },
            {
                "work_id": "verify-claim-critical",
                "worker": verifier.name,
                "harness": verifier.harness.value,
                "depends_on": [
                    "collect-support",
                    "collect-contradiction",
                ],
                "payload": {
                    "research_kind": "verification",
                    "claim_id": "claim-critical",
                    "assigned_path": "evidence/verification.json",
                    "lineage": [
                        "collect-support",
                        "collect-contradiction",
                        "verify-claim-critical",
                    ],
                },
            },
        ],
    )
    sources: dict[str, SourceReceipt] = {}
    excerpts: dict[str, ExcerptReceipt] = {}
    claim = register.admit_claim(
        TypedClaim(
            claim_id="claim-critical",
            claim_type="factual",
            text="The disputed observation is true.",
            # This is intentionally producer metadata.  Derived criticality
            # comes from required conclusion use and the pinned policy.
            critical=False,
            required_conclusion_ids=("conclusion-main",),
        )
    )
    collection_specs = (
        (
            "source-support",
            "The disputed observation supports the claim.",
        ),
        (
            "source-contradiction",
            "The disputed observation contradicts the claim.",
        ),
    )
    for work_id, (source_id, text) in zip(
        ("collect-support", "collect-contradiction"),
        collection_specs,
    ):
        collection_claims = kernel.claim_ready(run_id, limit=1)
        if len(collection_claims) != 1:
            raise ExecutorStoreError("verification_fixture_collection_not_ready")
        collection_claim = collection_claims[0]
        source = SourceReceipt.from_retrieval(
            requested_url=f"https://example.test/{source_id}",
            final_url=f"https://example.test/{source_id}",
            redirect_chain=(),
            retrieved_content=text.encode("utf-8"),
            retrieval_order=1 if work_id == "collect-support" else 2,
            retrieved_at=1_700_000_000 + (1 if work_id == "collect-support" else 2),
            content_type="text/plain",
            role="collector",
            run_id=run_id,
            work_id=collection_claim.work_id,
            attempt_id=collection_claim.attempt_id,
            fencing_token=collection_claim.fencing_token,
            harness=collection_claim.harness,
            worker=collection_claim.worker,
            agent=collection_claim.agent_name,
            pane=f"pane:{collection_claim.agent_name}",
            source_id=source_id,
        )
        excerpt = ExcerptReceipt.from_text(source, text=text, selector="body")
        register.admit_source(source)
        register.admit_excerpt(excerpt)
        sources[source_id] = source
        excerpts[source_id] = excerpt
        _settle_verification_fixture_work(
            kernel,
            collection_claim,
            artifact_type="research-collection",
            payload={
                "source_id": source.source_id,
                "excerpt_id": excerpt.excerpt_id,
                "logical_agent_id": source.agent,
            },
        )
    support = register.admit_relation(
        EvidenceRelation(
            relation_id="relation-support",
            claim_id=claim.claim_id,
            relation="support",
            source_id=sources["source-support"].source_id,
            excerpt_id=excerpts["source-support"].excerpt_id,
        )
    )
    contradiction = register.admit_relation(
        EvidenceRelation(
            relation_id="relation-contradiction",
            claim_id=claim.claim_id,
            relation="contradiction",
            source_id=sources["source-contradiction"].source_id,
            excerpt_id=excerpts["source-contradiction"].excerpt_id,
        )
    )
    verification_claims = kernel.claim_ready(run_id, limit=1)
    if len(verification_claims) != 1:
        raise ExecutorStoreError("verification_fixture_verification_not_ready")
    verification_claim = verification_claims[0]
    verification_text = "Independent verification matches the claim."
    verification_source = SourceReceipt.from_retrieval(
        requested_url="https://example.test/source-verification",
        final_url="https://example.test/source-verification",
        redirect_chain=(),
        retrieved_content=verification_text.encode("utf-8"),
        retrieval_order=3,
        retrieved_at=1_700_000_003,
        content_type="text/plain",
        role="verifier",
        run_id=run_id,
        work_id=verification_claim.work_id,
        attempt_id=verification_claim.attempt_id,
        fencing_token=verification_claim.fencing_token,
        harness=verification_claim.harness,
        worker=verification_claim.worker,
        agent=verification_claim.agent_name,
        pane=f"pane:{verification_claim.agent_name}",
        source_id="source-verification",
    )
    verification_excerpt = ExcerptReceipt.from_text(
        verification_source,
        text=verification_text,
        selector="body",
    )
    register.admit_source(verification_source)
    register.admit_excerpt(verification_excerpt)
    sources[verification_source.source_id] = verification_source
    excerpts[verification_source.source_id] = verification_excerpt
    _settle_verification_fixture_work(
        kernel,
        verification_claim,
        artifact_type="research-verification",
        payload={
            "claim_id": claim.claim_id,
            "source_id": verification_source.source_id,
            "excerpt_id": verification_excerpt.excerpt_id,
            "logical_agent_id": verification_source.agent,
        },
    )

    assignment: VerificationAssignment | None = None
    rejection_code: str | None = None
    disposition: VerificationDisposition | None = None
    if case_id in {
        "critical-independent",
        "critical-downgrade",
        "disposition-history",
        "source-reuse",
        "unaccounted-contradiction",
        "stale-assignment",
    }:
        collector_agent_ids = tuple(
            sorted(
                {
                    sources["source-support"].agent,
                    sources["source-contradiction"].agent,
                }
            )
        )
        collector_harnesses = (collector.harness.value,)
        verifier_agent_id = sources["source-verification"].agent
        assignment = VerificationAssignment(
            assignment_id="assignment-critical",
            claim_id=claim.claim_id,
            verification_work_id="verify-claim-critical",
            verifier_logical_agent_id=verifier_agent_id,
            verifier_harness=verifier.harness.value,
            collector_logical_agent_ids=collector_agent_ids,
            collector_harnesses=collector_harnesses,
            collector_work_ids=tuple(
                sources[source_id].work_id
                for source_id in ("source-support", "source-contradiction")
            ),
            source_ids=(sources["source-verification"].source_id,),
            excerpt_ids=(excerpts["source-verification"].excerpt_id,),
            run_id=run_id,
            verification_attempt_id=verification_claim.attempt_id,
            verification_fencing_token=verification_claim.fencing_token,
        )
        try:
            register.admit_verification_assignment(assignment)
            if case_id == "stale-assignment":
                assignment = register.supersede_verification_assignment(
                    assignment.assignment_id
                )
        except ResearchEvidenceError as exc:
            rejection_code = str(exc).split(":", 1)[0]
            assignment = None
    elif case_id == "critical-self-verification":
        try:
            register.admit_verification_assignment(
                VerificationAssignment(
                    assignment_id="assignment-self",
                    claim_id=claim.claim_id,
                    verification_work_id="verify-claim-critical",
                    verifier_logical_agent_id=sources["source-support"].agent,
                    verifier_harness=collector.harness.value,
                    collector_logical_agent_ids=(sources["source-support"].agent,),
                    collector_harnesses=(collector.harness.value,),
                    source_ids=(sources["source-support"].source_id,),
                    excerpt_ids=(excerpts["source-support"].excerpt_id,),
                )
            )
        except ResearchEvidenceError as exc:
            rejection_code = str(exc).split(":", 1)[0]
    if case_id == "disposition-history" and assignment is not None:
        first = VerificationDisposition(
            disposition_id="disposition-contested",
            claim_id=claim.claim_id,
            assignment_id=assignment.assignment_id,
            disposition="contested",
            reason="opposing admitted observations remain unresolved",
            evidence_ids=(),
            contradiction_ids=(contradiction.relation_id,),
            verifier_logical_agent_id=assignment.verifier_logical_agent_id,
            verifier_harness=assignment.verifier_harness,
            source_ids=(),
            excerpt_ids=(),
        )
        register.admit_verification_disposition(first)
    if case_id in {"critical-independent", "disposition-history"} and assignment:
        verification_relation = register.admit_relation(
            EvidenceRelation(
                relation_id="relation-verification",
                claim_id=claim.claim_id,
                relation="support",
                source_id=sources["source-verification"].source_id,
                excerpt_id=excerpts["source-verification"].excerpt_id,
            )
        )
        disposition = VerificationDisposition(
            disposition_id="disposition-verified",
            claim_id=claim.claim_id,
            assignment_id=assignment.assignment_id,
            disposition="verified",
            reason="current independent evidence accounts for every contradiction",
            evidence_ids=(verification_relation.relation_id,),
            contradiction_ids=(contradiction.relation_id,),
            verifier_logical_agent_id=assignment.verifier_logical_agent_id,
            verifier_harness=assignment.verifier_harness,
            source_ids=(verification_relation.source_id,),
            excerpt_ids=(verification_relation.excerpt_id,),
        )
        register.admit_verification_disposition(disposition)
    elif case_id == "unaccounted-contradiction" and assignment:
        verification_relation = register.admit_relation(
            EvidenceRelation(
                relation_id="relation-verification",
                claim_id=claim.claim_id,
                relation="support",
                source_id=sources["source-verification"].source_id,
                excerpt_id=excerpts["source-verification"].excerpt_id,
            )
        )
        try:
            register.admit_verification_disposition(
                VerificationDisposition(
                    disposition_id="disposition-unaccounted",
                    claim_id=claim.claim_id,
                    assignment_id=assignment.assignment_id,
                    disposition="verified",
                    reason="contradiction was omitted",
                    evidence_ids=(verification_relation.relation_id,),
                    contradiction_ids=(),
                    verifier_logical_agent_id=assignment.verifier_logical_agent_id,
                    verifier_harness=assignment.verifier_harness,
                    source_ids=(verification_relation.source_id,),
                    excerpt_ids=(verification_relation.excerpt_id,),
                )
            )
        except ResearchEvidenceError as exc:
            rejection_code = str(exc).split(":", 1)[0]
    elif case_id == "stale-assignment" and assignment:
        try:
            register.admit_verification_disposition(
                VerificationDisposition(
                    disposition_id="disposition-stale",
                    claim_id=claim.claim_id,
                    assignment_id=assignment.assignment_id,
                    disposition="verified",
                    reason="late verification attempt",
                    evidence_ids=(),
                    contradiction_ids=(contradiction.relation_id,),
                    verifier_logical_agent_id=assignment.verifier_logical_agent_id,
                    verifier_harness=assignment.verifier_harness,
                    source_ids=(),
                    excerpt_ids=(),
                )
            )
        except ResearchEvidenceError as exc:
            rejection_code = str(exc).split(":", 1)[0]
    elif case_id == "critical-downgrade" and assignment is not None:
        # A producer attempting to downgrade the derived criticality cannot
        # create a verified disposition or gate credit.
        try:
            register.admit_verification_disposition(
                VerificationDisposition(
                    disposition_id="disposition-downgrade",
                    claim_id=claim.claim_id,
                    assignment_id=assignment.assignment_id,
                    disposition="unverified",
                    reason="producer confidence cannot downgrade a critical claim",
                    evidence_ids=(),
                    contradiction_ids=(contradiction.relation_id,),
                    verifier_logical_agent_id=assignment.verifier_logical_agent_id,
                    verifier_harness=assignment.verifier_harness,
                    source_ids=(),
                    excerpt_ids=(),
                )
            )
        except ResearchEvidenceError as exc:
            rejection_code = str(exc).split(":", 1)[0]
    elif case_id == "source-reuse" and assignment is not None:
        try:
            register.admit_verification_disposition(
                VerificationDisposition(
                    disposition_id="disposition-reuse",
                    claim_id=claim.claim_id,
                    assignment_id=assignment.assignment_id,
                    disposition="verified",
                    reason="reused collector source",
                    evidence_ids=(support.relation_id,),
                    contradiction_ids=(contradiction.relation_id,),
                    verifier_logical_agent_id=assignment.verifier_logical_agent_id,
                    verifier_harness=assignment.verifier_harness,
                    source_ids=(support.source_id,),
                    excerpt_ids=(support.excerpt_id,),
                )
            )
        except ResearchEvidenceError as exc:
            rejection_code = str(exc).split(":", 1)[0]

    verification = register.claim_status(claim.claim_id)
    if case_id == "contradiction-pack" or case_id == "contested":
        outcome_code = "claim_contested"
        reason = "opposing admitted evidence remains contested"
        success = True
    elif verification["state"] == "verified":
        outcome_code = "critical_claim_verified"
        reason = "independent verification admitted with contradiction accounting"
        success = True
    elif rejection_code and case_id in {
        "critical-self-verification",
        "source-reuse",
        "unaccounted-contradiction",
        "stale-assignment",
    }:
        outcome_code = rejection_code
        reason = rejection_code
        success = False
    else:
        outcome_code = "critical_claim_unverified"
        reason = rejection_code or "critical_claim_requires_independent_verification"
        success = False
    evidence = register.to_dict()
    kernel.transition_run(
        run_id,
        "succeeded" if verification["state"] == "verified" else "failed",
    )
    kernel.append_event(
        run_id,
        event_key=f"research-verification-fixture:{requested_case_id}",
        event_type="research_verification_fixture",
        payload={
            "fixture_case_id": requested_case_id,
            "outcome_code": outcome_code,
            "reason": reason,
            "verification": verification,
            "verification_assignment": (
                None if assignment is None else assignment.to_dict()
            ),
            "verification_disposition": (
                None if disposition is None else disposition.to_dict()
            ),
            "evidence": evidence,
        },
        error_code=None if success else outcome_code,
    )
    inspected = kernel.inspect_run(run_id)
    print(
        json.dumps(
            {
                "schema_version": 2,
                "executor": "research-synthesis",
                "route": route,
                "success": success,
                "fixture_case_id": requested_case_id,
                "bound": {
                    "max_submissions": 1,
                    "max_dispositions": 2,
                    "independent_logical_agent_required": (
                        research_config.verification.require_independent_logical_agent
                    ),
                    "harness_separation_required": (
                        research_config.verification.require_harness_separation
                    ),
                },
                "run_id": run_id,
                "execution_state": store.require_run(run_id).state,
                "code": outcome_code,
                "reason": reason,
                "verification": verification,
                "verification_assignment": (
                    None if assignment is None else assignment.to_dict()
                ),
                "verification_disposition": (
                    None if disposition is None else disposition.to_dict()
                ),
                "evidence": evidence,
                "events": len(inspected["events"]),
            },
            sort_keys=True,
        )
    )
    return 0


def _settle_verification_fixture_work(
    kernel: ExecutionKernel,
    claim: object,
    *,
    artifact_type: str,
    payload: dict[str, object],
) -> None:
    """Settle a scripted collector/verifier turn through the shared kernel."""

    if not hasattr(claim, "run_id") or not hasattr(claim, "payload"):
        raise ExecutorStoreError("verification_fixture_claim_required")
    assigned_path = claim.payload.get("assigned_path")
    lineage = claim.payload.get("lineage")
    if not isinstance(assigned_path, str) or not isinstance(lineage, list):
        raise ExecutorStoreError("verification_fixture_assignment_invalid")
    content = json.dumps(payload, sort_keys=True).encode("utf-8")
    kernel.stage_artifact(claim, assigned_path, content)
    manifest = kernel.store.require_run(claim.run_id).manifest
    envelope = {
        "contract_version": ARTIFACT_CONTRACT_VERSION,
        "artifact_type": artifact_type,
        "run_id": claim.run_id,
        "work_id": claim.work_id,
        "logical_id": claim.work_id,
        "attempt_id": claim.attempt_id,
        "fencing_token": claim.fencing_token,
        "path": assigned_path,
        "content_digest": digest_bytes(content),
        "size_bytes": len(content),
        "lineage": lineage,
        "pinned_digests": {
            field: getattr(manifest, field)
            for field in MANIFEST_DIGEST_FIELDS
        },
        "harness": claim.harness,
        "worker": claim.worker,
        "agent": claim.agent_name,
        "pane": f"pane:{claim.agent_name}",
        "payload": payload,
    }
    kernel.complete_work_item(
        claim,
        artifact=envelope,
        receipt_payload={
            "research_kind": claim.payload.get("research_kind"),
            "payload": payload,
        },
        expected_lineage=lineage,
        expected_path=assigned_path,
    )
    kernel.cleanup_attempt(claim)


def _raise_fixture_case(
    case_id: str,
    *,
    source: SourceReceipt,
    excerpt: ExcerptReceipt | None = None,
    ledger: ResearchEvidenceRegister,
) -> None:
    if case_id == "snippet-only":
        excerpt = excerpt or _fixture_excerpt(source)
        value = excerpt.to_dict()
        value["capture_kind"] = "search_snippet"
        ledger.admit_excerpt(value)
    elif case_id == "unknown-source-key":
        value = source.to_dict()
        value["unexpected"] = True
        SourceReceipt.from_mapping(value)
    elif case_id == "bare-url":
        ledger.admit_excerpt(
            ExcerptReceipt.from_text(
                source,
                text=source.final_url,
                selector="body",
            )
        )
    elif case_id == "offset-out-of-bounds":
        ledger.admit_excerpt(
            ExcerptReceipt.from_text(
                source,
                text="bounded",
                offset_start=0,
                offset_end=100,
            )
        )
    elif case_id == "malformed-excerpt":
        excerpt = excerpt or _fixture_excerpt(source)
        value = excerpt.to_dict()
        value["selector"] = None
        value["offset_start"] = None
        value["offset_end"] = None
        ledger.admit_excerpt(value)
    elif case_id == "unknown-excerpt-key":
        excerpt = excerpt or _fixture_excerpt(source)
        value = excerpt.to_dict()
        value["unexpected"] = True
        ExcerptReceipt.from_mapping(value)
    elif case_id == "missing-excerpt":
        ledger.admit_relation(
            {
                "schema_version": 1,
                "relation_id": "relation-missing-excerpt",
                "claim_id": "claim-bounded",
                "relation": "support",
                "source_id": source.source_id,
                "excerpt_id": "excerpt-missing",
            }
        )
    elif case_id == "oversize-excerpt":
        excerpt = excerpt or _fixture_excerpt(source)
        value = excerpt.to_dict()
        value["text"] = "x" * 16_385
        value["excerpt_digest"] = digest_bytes(value["text"].encode("utf-8"))
        ledger.admit_excerpt(value)
    elif case_id == "digest-mismatch":
        excerpt = excerpt or _fixture_excerpt(source)
        value = excerpt.to_dict()
        value["excerpt_digest"] = "sha256:" + "0" * 64
        ledger.admit_excerpt(value)
    elif case_id == "malformed-claim":
        ledger.admit_claim(
            {
                "schema_version": 1,
                "claim_id": "claim-malformed",
                "text": "Missing claim type.",
            }
        )
    elif case_id == "unknown-claim-key":
        ledger.admit_claim(
            {
                "schema_version": 1,
                "claim_id": "claim-unknown-key",
                "claim_type": "factual",
                "text": "Unknown claim key.",
                "unexpected": True,
            }
        )
    elif case_id == "unknown-claim-type":
        ledger.admit_claim(
            {
                "schema_version": 1,
                "claim_id": "claim-unknown",
                "claim_type": "opinion",
                "text": "Unsupported claim type.",
            }
        )
    elif case_id == "unknown-relation":
        ledger.admit_relation(
            {
                "schema_version": 1,
                "relation_id": "relation-unknown",
                "claim_id": "claim-bounded",
                "relation": "depends_on",
                "source_id": source.source_id,
                "excerpt_id": excerpt.excerpt_id,
            }
        )
    elif case_id == "unknown-relation-key":
        ledger.admit_relation(
            {
                "schema_version": 1,
                "relation_id": "relation-unknown-key",
                "claim_id": "claim-bounded",
                "relation": "support",
                "source_id": source.source_id,
                "excerpt_id": excerpt.excerpt_id,
                "unexpected": True,
            }
        )
    elif case_id == "unadmitted-source":
        ledger.admit_relation(
            {
                "schema_version": 1,
                "relation_id": "relation-source-missing",
                "claim_id": "claim-bounded",
                "relation": "support",
                "source_id": "source-missing",
                "excerpt_id": excerpt.excerpt_id,
            }
        )
    elif case_id == "unadmitted-excerpt":
        ledger.admit_relation(
            {
                "schema_version": 1,
                "relation_id": "relation-excerpt-missing",
                "claim_id": "claim-bounded",
                "relation": "support",
                "source_id": source.source_id,
                "excerpt_id": "excerpt-missing",
            }
        )
    elif case_id == "unadmitted-premise":
        ledger.admit_claim(
            TypedClaim(
                claim_id="claim-inference",
                claim_type="inference",
                text="Unadmitted premise.",
                premise_ids=("claim-missing",),
                evidence_ids=("relation-support",),
            )
        )
    elif case_id == "unadmitted-evidence":
        ledger.admit_claim(
            TypedClaim(
                claim_id="claim-inference",
                claim_type="inference",
                text="Unadmitted evidence.",
                premise_ids=("claim-bounded",),
                evidence_ids=("relation-missing",),
            )
        )


def _fixture_excerpt(source: SourceReceipt) -> ExcerptReceipt:
    return ExcerptReceipt.from_text(
        source,
        text='{"answer":"bounded"}',
        selector="$.answer",
    )


def _research_start(
    config: WorkflowConfig,
    *,
    dedupe_key: str,
    input_value: object,
) -> int:
    if config.executor is None:
        raise ConfigError("executor_missing")
    research_config = ResearchConfig.from_mapping(config.executor.settings)
    if isinstance(input_value, str):
        input_value = {
            "version": 1,
            "kind": "public-question",
            "question": input_value,
        }
    parsed = parse_research_input(input_value)
    classification = classify_input(parsed, research_config)
    decomposition = decompose_question(parsed, research_config)
    domain_view = build_research_view(
        parsed,
        classification,
        decomposition,
        input_policy=research_config.input_policy,
        verification_policy=research_config.verification,
    )
    initial_state = "blocked" if classification.requires_input else "pending"
    store = ExecutorStore(config.state_db)
    kernel = _research_kernel(config, store)
    workflow_text = config.path.read_text(encoding="utf-8")
    route_definition = {
        "roles": dict(research_config.roles),
        "workers": [
            {
                "name": worker.name,
                "harness": worker.harness.value,
                "replicas": worker.replicas,
            }
            for worker in config.workers
        ],
    }
    definitions = {
        "workflow": {
            "path": str(config.path),
            "name": config.name,
            "schema_version": 2,
            "source": workflow_text,
        },
        "config": {
            "coordinator": {
                "max_parallel": config.coordinator.max_parallel,
                "lease_seconds": config.coordinator.lease_seconds,
                "max_attempts": config.coordinator.max_attempts,
            },
            "research": dict(config.executor.settings),
        },
        "input": input_value,
        "route": route_definition,
        "profile": {"name": "default"},
        "prompt": {"version": 1},
        "static_checks": [],
        "contract": {"version": "research-v1"},
        "executor": {"kind": "research-synthesis", "version": 1},
        "artifact_contract": {
            "version": ARTIFACT_CONTRACT_VERSION,
            "schema_version": 1,
        },
    }
    run_id, created = store.create_run(
        config.name,
        "research-synthesis",
        dedupe_key,
        state=initial_state,
        workflow_definition=definitions["workflow"],
        config_definition=definitions["config"],
        input_value=definitions["input"],
        route_definition=definitions["route"],
        profile_definition=definitions["profile"],
        prompt_definition=definitions["prompt"],
        static_checks=definitions["static_checks"],
        contract_definition=definitions["contract"],
        executor_definition=definitions["executor"],
        artifact_contract_definition=definitions["artifact_contract"],
    )
    if created:
        kernel.append_event(
            run_id,
            event_key="research-classified",
            event_type="research_classified",
            payload={
                "input": domain_view["input"],
                "classification": domain_view["classification"],
            },
        )
        kernel.append_event(
            run_id,
            event_key="research-decomposed",
            event_type="research_decomposed",
            payload={
                "decomposition": domain_view["decomposition"],
                "coverage": domain_view["coverage"],
                "phase": domain_view["phase"],
                "required_input": domain_view["required_input"],
                "verification_policy": domain_view["verification_policy"],
            },
        )
        if classification.requires_input:
            required = domain_view["required_input"]
            kernel.append_event(
                run_id,
                event_key="research-input-required",
                event_type="research_input_required",
                payload=required,
                error_code="input_required",
            )
        else:
            _create_research_frontier(
                config,
                kernel,
                run_id,
                decomposition,
                worker_role="collector",
            )
    else:
        # A deduped start must not create a second frontier or append a second
        # semantic record.  The existing durable domain events are authoritative.
        domain_view = _research_domain_view(kernel, run_id)
    inspect = kernel.inspect_run(run_id)
    payload = {
        "schema_version": 2,
        "executor": "research-synthesis",
        "route": "research.start",
        "success": True,
        "run_id": run_id,
        "created": created,
        "state": store.require_run(run_id).state,
        "pinned_manifest": store.require_run(run_id).manifest.to_dict(),
        "research": domain_view,
        "work_items": inspect["work_items"],
    }
    print(json.dumps(payload, sort_keys=True))
    return 0


def _research_status(config: WorkflowConfig, *, run_id: str | None) -> int:
    store = ExecutorStore(config.state_db)
    kernel = _research_kernel(config, store)
    if run_id is None:
        runs = store.runs(config.name)
        if len(runs) != 1:
            if not runs:
                raise ExecutorStoreError("run_not_found")
            raise ExecutorStoreError("run_selector_ambiguous")
        run_id = runs[0].run_id
    run = store.require_run(run_id)
    if run.workflow != config.name:
        raise ExecutorStoreError("run_not_found")
    inspected = kernel.inspect_run(run_id)
    payload = {
        "schema_version": 2,
        "executor": "research-synthesis",
        "route": "research.status",
        "success": True,
        "run_id": run_id,
        "workflow": config.name,
        "state": run.state,
        "work_counts": inspected["work_counts"],
        "work_items": inspected["work_items"],
        "research": _research_domain_view(kernel, run_id),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _research_export(
    config: WorkflowConfig,
    *,
    run_id: str,
    output: str | None,
) -> int:
    store = ExecutorStore(config.state_db)
    run = store.require_run(run_id)
    if run.workflow != config.name or run.executor_kind != "research-synthesis":
        raise ExecutorStoreError("run_not_found")
    if run.state not in {"succeeded", "failed"}:
        raise ResearchEvidenceError("research_export_requires_terminal_run")
    kernel = _research_kernel(config, store)
    verification_event = next(
        (
            event
            for event in reversed(kernel.list_events(run_id))
            if event.event_type == "research_verification_fixture"
        ),
        None,
    )
    if verification_event is None or not isinstance(verification_event.payload, dict):
        raise ResearchEvidenceError("research_export_evidence_missing")
    raw_evidence = verification_event.payload.get("evidence")
    if not isinstance(raw_evidence, dict):
        raise ResearchEvidenceError("research_export_evidence_missing")
    research_config = ResearchConfig.from_mapping(
        config.executor.settings if config.executor is not None else {}
    )
    register = ResearchEvidenceRegister.from_mapping(
        raw_evidence,
        verification_policy=research_config.verification,
    )
    outcome_code = str(verification_event.payload.get("outcome_code", "unknown"))
    if (
        not register.verification_credit_claim_ids
        and outcome_code != "claim_contested"
    ):
        raise ResearchEvidenceError("research_export_critical_verification_required")

    export_root = (config.workspace / ".orchestrator" / "exports").resolve()
    report_path = (
        (export_root / run_id / "report.md")
        if output is None
        else _resolve_export_path(config.workspace, output)
    )
    if output is not None and report_path.suffix.lower() != ".md":
        raise ResearchEvidenceError("research_export_output_must_be_markdown")
    register_path = report_path.with_name(
        f"{report_path.stem}.source-claim-register.json"
    )
    for path in (report_path, register_path):
        if not path.parent.resolve().is_relative_to(export_root):
            raise ResearchEvidenceError("research_export_path_outside_root")
    register_bytes = (
        json.dumps(register.to_dict(), indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    report_lines = [
        "# Research verification export",
        "",
        f"- Run: `{run_id}`",
        f"- Pinned manifest: `{run.manifest.manifest_digest}`",
        f"- Terminal state: `{run.state}`",
        f"- Fixture outcome: `{outcome_code}`",
        "- Verification policy is pinned in the run definition.",
        "",
        "## Claims",
        "",
    ]
    for claim in register.claims:
        status = register.claim_status(claim.claim_id)
        label = str(status["state"])
        report_lines.extend(
            [
                f"### `{claim.claim_id}` ({label})",
                "",
                claim.text,
                "",
                f"- Critical: `{str(status['critical']).lower()}`",
                f"- Gate credit: `{str(status['gate_credit']).lower()}`",
                f"- Support relations: {', '.join(status['support_relation_ids']) or 'none'}",
                "- Contradiction relations: "
                + (
                    ", ".join(status["contradiction_relation_ids"])
                    or "none"
                ),
                "",
            ]
        )
        if status["disposition_history"]:
            report_lines.append("Disposition history:")
            for item in status["disposition_history"]:
                report_lines.append(
                    f"- `{item['disposition_id']}`: `{item['disposition']}` "
                    f"(current={str(item['current']).lower()}) "
                    f"{item['reason']}"
                )
            report_lines.append("")
    report_lines.extend(
        [
            "## Evidence relations",
            "",
            "| Relation | Claim | Source | Excerpt |",
            "| --- | --- | --- | --- |",
        ]
    )
    for relation in register.relations:
        report_lines.append(
            f"| `{relation.relation}` | `{relation.claim_id}` | "
            f"`{relation.source_id}` | `{relation.excerpt_id}` |"
        )
    report_bytes = ("\n".join(report_lines) + "\n").encode("utf-8")
    _check_export_target(report_path, report_bytes)
    _check_export_target(register_path, register_bytes)
    _atomic_export_bytes(report_path, report_bytes)
    _atomic_export_bytes(register_path, register_bytes)
    print(
        json.dumps(
            {
                "schema_version": 2,
                "executor": "research-synthesis",
                "route": "research.export",
                "success": True,
                "run_id": run_id,
                "terminal_state": run.state,
                "fixture_outcome": outcome_code,
                "verification_gate_credit": bool(
                    register.verification_credit_claim_ids
                ),
                "pinned_manifest_digest": run.manifest.manifest_digest,
                "report_path": str(report_path),
                "report_digest": digest_bytes(report_bytes),
                "register_path": str(register_path),
                "register_digest": digest_bytes(register_bytes),
            },
            sort_keys=True,
        )
    )
    return 0


def _resolve_export_path(workspace: Path, value: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ResearchEvidenceError("research_export_output_required")
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = workspace / ".orchestrator" / "exports" / candidate
    return candidate.resolve()


def _atomic_export_bytes(path: Path, content: bytes) -> None:
    _check_export_target(path, content)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary.write_bytes(content)
        temporary.replace(path)
    except OSError as exc:
        if temporary.exists():
            temporary.unlink()
        raise ResearchEvidenceError("research_export_write_failed") from exc


def _check_export_target(path: Path, content: bytes) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise ResearchEvidenceError("research_export_existing_path_invalid")
        if path.read_bytes() != content:
            raise ResearchEvidenceError("research_export_overwrite_conflict")
        return
    temporary = path.with_name(f".{path.name}.tmp")
    if temporary.exists():
        raise ResearchEvidenceError("research_export_temporary_exists")


def _research_inspect(
    config: WorkflowConfig,
    *,
    run_id: str | None,
    dedupe_key: str | None,
) -> int:
    store = ExecutorStore(config.state_db)
    selected = _research_select_run(
        store,
        config.name,
        run_id=run_id,
        dedupe_key=dedupe_key,
    )
    kernel = _research_kernel(config, store)
    payload = kernel.inspect_run(selected)
    payload.update(
        {
            "route": "research.inspect",
            "executor": "research-synthesis",
            "success": True,
            "research": _research_domain_view(kernel, selected),
        }
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _research_resume(
    config: WorkflowConfig,
    *,
    run_id: str,
    input_id: str,
    material: str,
) -> int:
    if not material:
        raise ResearchInputError("research_input_material_missing")
    store = ExecutorStore(config.state_db)
    kernel = _research_kernel(config, store)
    run = store.require_run(run_id)
    if run.workflow != config.name or run.executor_kind != "research-synthesis":
        raise ExecutorStoreError("run_not_found")
    domain = _research_domain_view(kernel, run_id)
    required = domain.get("required_input")
    if not isinstance(required, dict) or required.get("input_id") != input_id:
        raise ResearchInputError("research_required_input_mismatch")
    material_bytes = material.encode("utf-8")
    max_bytes = int(required.get("max_bytes", 16_384))
    if len(material_bytes) > max_bytes:
        raise ResearchInputError("research_input_material_over_limit")
    material_digest = digest_bytes(material_bytes)
    admitted_digest = required.get("content_digest")
    if admitted_digest is not None:
        if admitted_digest != material_digest:
            raise ResearchInputError("research_input_conflict")
        existing_items = kernel.list_work_items(run_id)
        if not any(
            item.payload.get("research_kind") == "collection"
            for item in existing_items
            if isinstance(item.payload, dict)
        ):
            if run.state == "blocked":
                kernel.transition_run(run_id, "running")
            _create_research_frontier(
                config,
                kernel,
                run_id,
                _decomposition_from_view(domain),
                worker_role="collector",
            )
            domain = _research_domain_view(kernel, run_id)
            inspected = kernel.inspect_run(run_id)
            print(
                json.dumps(
                    {
                        "schema_version": 2,
                        "executor": "research-synthesis",
                        "route": "research.resume",
                        "success": True,
                        "code": "input_admitted",
                        "run_id": run_id,
                        "state": store.require_run(run_id).state,
                        "research": domain,
                        "work_items": inspected["work_items"],
                    },
                    sort_keys=True,
                )
            )
            return 0
        inspected = kernel.inspect_run(run_id)
        print(
            json.dumps(
                {
                    "schema_version": 2,
                    "executor": "research-synthesis",
                    "route": "research.resume",
                    "success": True,
                    "code": "already_admitted",
                    "run_id": run_id,
                    "state": run.state,
                    "research": domain,
                    "work_items": inspected["work_items"],
                },
                sort_keys=True,
            )
        )
        return 0
    kernel.append_event(
        run_id,
        event_key=f"research-input-admitted:{input_id}",
        event_type="research_input_admitted",
        payload={
            "input_id": input_id,
            "content_digest": material_digest,
            "size_bytes": len(material_bytes),
            "accepted_form": required.get("accepted_form", "text"),
            "public_route_permitted": False,
        },
    )
    kernel.transition_run(run_id, "running")
    # The original question bytes are intentionally not retained in the
    # domain view.  The decomposition is already pinned, so only the durable
    # matrix is needed to release the frontier.
    decomposition = _decomposition_from_view(domain)
    _create_research_frontier(
        config,
        kernel,
        run_id,
        decomposition,
        worker_role="collector",
    )
    updated_domain = _research_domain_view(kernel, run_id)
    inspected = kernel.inspect_run(run_id)
    print(
        json.dumps(
            {
                "schema_version": 2,
                "executor": "research-synthesis",
                "route": "research.resume",
                "success": True,
                "code": "input_admitted",
                "run_id": run_id,
                "state": store.require_run(run_id).state,
                "research": updated_domain,
                "work_items": inspected["work_items"],
            },
            sort_keys=True,
        )
    )
    return 0


def _create_research_frontier(
    config: WorkflowConfig,
    kernel: ExecutionKernel,
    run_id: str,
    decomposition: object,
    *,
    worker_role: str,
) -> None:
    if not hasattr(decomposition, "required_cells"):
        raise ResearchInputError("decomposition_required")
    required_cells = decomposition.required_cells
    workers_by_name = {worker.name: worker for worker in config.workers}
    role_worker = None
    if config.executor is not None:
        settings = config.executor.settings
        roles = settings.get("roles", {})
        if isinstance(roles, dict):
            role_worker = roles.get(worker_role)
    worker = workers_by_name.get(role_worker) or config.workers[0]
    collection_ids = [
        f"collect-{cell.facet_id}-{cell.perspective_id}"
        for cell in required_cells
    ]
    specifications = [
        {
            "work_id": "classify",
            "worker": worker.name,
            "harness": worker.harness.value,
            "payload": {
                "research_kind": "classification",
                "assigned_path": "classification.json",
                "lineage": ["classify"],
            },
        }
    ]
    for work_id, cell in zip(collection_ids, required_cells):
        specifications.append(
            {
                "work_id": work_id,
                "worker": worker.name,
                "harness": worker.harness.value,
                "payload": {
                    "research_kind": "collection",
                    "frontier_id": "frontier-0",
                    "cell_id": cell.id,
                    "facet_id": cell.facet_id,
                    "perspective_id": cell.perspective_id,
                    "assigned_path": f"collection/{cell.id}.json",
                    "lineage": [work_id],
                },
            }
        )
    kernel.add_work_items(run_id, specifications)
    kernel.create_barrier(
        run_id,
        "research-frontier-0-ready",
        required_work_ids=("classify",),
        release_work_ids=tuple(collection_ids),
    )
    kernel.create_barrier(
        run_id,
        "research-frontier-0-fanin",
        required_work_ids=tuple(collection_ids),
        release_work_ids=(),
    )
    claim = kernel.claim_ready(run_id, limit=1)
    if not claim:
        return
    _settle_classification_artifact(kernel, claim[0])
    kernel.append_event(
        run_id,
        event_key="research-frontier-0-released",
        event_type="research_frontier_ready",
        payload={
            "frontier_id": "frontier-0",
            "barrier_id": "research-frontier-0-ready",
            "work_ids": collection_ids,
            "ready_at_barrier": True,
        },
    )


def _settle_classification_artifact(
    kernel: ExecutionKernel,
    claim: object,
) -> None:
    content = b'{"classification":"admitted"}'
    path = "classification.json"
    kernel.stage_artifact(claim, path, content)
    manifest = kernel.store.require_run(claim.run_id).manifest
    envelope = {
        "contract_version": ARTIFACT_CONTRACT_VERSION,
        "artifact_type": "research-classification",
        "run_id": claim.run_id,
        "work_id": claim.work_id,
        "logical_id": claim.work_id,
        "attempt_id": claim.attempt_id,
        "fencing_token": claim.fencing_token,
        "path": path,
        "content_digest": digest_bytes(content),
        "size_bytes": len(content),
        "lineage": ["classify"],
        "pinned_digests": {
            field: getattr(manifest, field)
            for field in MANIFEST_DIGEST_FIELDS
        },
        "harness": claim.harness,
        "worker": claim.worker,
        "agent": claim.agent_name,
        "pane": f"pane:{claim.agent_name}",
        "payload": {"classification": "admitted"},
    }
    kernel.complete_work_item(claim, artifact=envelope)
    kernel.cleanup_attempt(claim)


def _research_domain_view(kernel: ExecutionKernel, run_id: str) -> dict[str, object]:
    events = kernel.list_events(run_id)
    definition: dict[str, object] = {}
    admitted: dict[str, object] | None = None
    for event in events:
        if event.event_type == "research_classified":
            if isinstance(event.payload, dict):
                definition.update(json.loads(json.dumps(event.payload)))
        elif event.event_type == "research_decomposed":
            if isinstance(event.payload, dict):
                definition.update(json.loads(json.dumps(event.payload)))
        elif event.event_type == "research_input_admitted":
            if isinstance(event.payload, dict):
                admitted = dict(event.payload)
        elif event.event_type == "research_verification_fixture":
            if isinstance(event.payload, dict):
                verification = json.loads(json.dumps(event.payload))
                definition["verification"] = verification.get("verification")
                definition["verification_assignment"] = verification.get(
                    "verification_assignment"
                )
                definition["verification_disposition"] = verification.get(
                    "verification_disposition"
                )
                definition["evidence"] = verification.get("evidence")
    if not definition or "classification" not in definition:
        fixture = next(
            (
                event.payload
                for event in reversed(events)
                if event.event_type == "research_verification_fixture"
                and isinstance(event.payload, dict)
            ),
            None,
        )
        if isinstance(fixture, dict):
            return {
                "phase": "verification-fixture",
                "fixture_case_id": fixture.get("fixture_case_id"),
                "verification_policy": ResearchConfig.from_mapping(
                    {
                        "verification": (
                            fixture.get("evidence", {}).get(
                                "verification_policy",
                                {},
                            )
                            if isinstance(fixture.get("evidence"), dict)
                            else {}
                        )
                    }
                ).verification.to_dict(),
                "verification": fixture.get("verification"),
                "verification_assignment": fixture.get(
                    "verification_assignment"
                ),
                "verification_disposition": fixture.get(
                    "verification_disposition"
                ),
                "evidence": fixture.get("evidence"),
            }
        raise ExecutorStoreError("research_definition_missing")
    required = definition.get("required_input")
    if isinstance(required, dict) and admitted is not None:
        required = {
            **required,
            "admitted": True,
            "reason": "admitted",
            "content_digest": admitted.get("content_digest"),
        }
        definition["required_input"] = required
        definition["phase"] = "decomposed"
    return definition


def _decomposition_from_view(view: dict[str, object]) -> object:
    decomposition = view.get("decomposition")
    if not isinstance(decomposition, dict):
        raise ResearchInputError("research_decomposition_missing")
    raw_cells = decomposition.get("required_cells")
    if not isinstance(raw_cells, list):
        raise ResearchInputError("research_required_cells_missing")
    # Re-parse the pinned matrix through the public domain parser rather than
    # trusting an arbitrary resume payload.
    return ResearchConfig.from_mapping(
        {
            "decomposition": {
                "facets": decomposition.get("facets", []),
                "perspectives": decomposition.get("perspectives", []),
                "required_cells": raw_cells,
                "max_facets": decomposition.get("max_facets", 8),
                "max_perspectives": decomposition.get("max_perspectives", 8),
                "max_required_cells": decomposition.get("max_required_cells", 256),
            },
            "coverage": {
                key: decomposition.get("coverage_policy", {}).get(key)
                for key in ("threshold", "allow_multi_cell_credit")
                if isinstance(decomposition.get("coverage_policy"), dict)
                and key in decomposition["coverage_policy"]
            },
        }
    ).decomposition
