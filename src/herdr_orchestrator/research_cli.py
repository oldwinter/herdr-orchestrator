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
    if command == "evidence-fixture":
        return _research_evidence_fixture(config, args.case)
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
}


def _research_evidence_fixture(config: WorkflowConfig, case_id: str) -> int:
    requested_case_id = case_id
    case_id = _RESEARCH_EVIDENCE_FIXTURE_ALIASES.get(case_id, case_id)
    if case_id not in _RESEARCH_EVIDENCE_FIXTURE_CASES:
        raise ConfigError(
            f"research_evidence_fixture_unknown_case:{requested_case_id}"
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
        )
    finally:
        shutil.rmtree(fixture_workspace, ignore_errors=True)


def _research_evidence_fixture_in_state(
    config: WorkflowConfig,
    *,
    requested_case_id: str,
    case_id: str,
) -> int:
    if config.executor is None:
        raise ConfigError("executor_missing")
    store = ExecutorStore(config.state_db)
    kernel = _research_kernel(config, store)
    worker = config.workers[0]
    run_id, _ = store.create_run(
        config.name,
        "research-synthesis",
        f"research-evidence-fixture-{requested_case_id}",
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
                "route": "research.evidence-fixture",
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
    if not definition or "classification" not in definition:
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
