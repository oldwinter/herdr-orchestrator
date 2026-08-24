from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from herdr_orchestrator.config import ConfigError, load_workflow
from herdr_orchestrator.executor_artifacts import (
    ARTIFACT_CONTRACT_VERSION,
    MAX_ARTIFACT_BYTES,
    ArtifactError,
    ArtifactValidationError,
    digest_bytes,
)
from herdr_orchestrator.executor_kernel import (
    ClaimLostError,
    ExecutionKernel,
    KernelError,
)
from herdr_orchestrator.executor_protocol import MANIFEST_DIGEST_FIELDS, definition_digest
from herdr_orchestrator.executor_store import (
    ExecutorStore,
    ExecutorStoreError,
)
from herdr_orchestrator.herdr import HerdrTransport, smoke_agent_name
from herdr_orchestrator.model import AgentState, Harness, WorkflowConfig
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.store import Store


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable multi-harness orchestration over Herdr.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "seed", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--workflow", required=True)
        if name == "status":
            command.add_argument("--run-id")

    v2_status = subparsers.add_parser("v2-status")
    v2_status.add_argument("--workflow", required=True)
    v2_status.add_argument("--run-id")

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--workflow", required=True)
    selector = inspect_parser.add_mutually_exclusive_group()
    selector.add_argument("--run-id", "--run")
    selector.add_argument("--dedupe-key")

    start_parser = subparsers.add_parser("start")
    start_parser.add_argument("--workflow", required=True)
    start_parser.add_argument("--dedupe-key", required=True)
    start_parser.add_argument("--input", default="")

    research_parser = subparsers.add_parser("research")
    research_subparsers = research_parser.add_subparsers(
        dest="research_command",
        required=True,
    )
    research_start = research_subparsers.add_parser("start")
    research_start.add_argument("--workflow", required=True)
    research_start.add_argument("--dedupe-key", required=True)
    research_start.add_argument("--question", "--input", dest="question")
    research_start.add_argument("--input-json")
    research_status = research_subparsers.add_parser("status")
    research_status.add_argument("--workflow", required=True)
    research_status.add_argument("--run-id")
    research_inspect = research_subparsers.add_parser("inspect")
    research_inspect.add_argument("--workflow", required=True)
    research_selector = research_inspect.add_mutually_exclusive_group()
    research_selector.add_argument("--run-id", "--run")
    research_selector.add_argument("--dedupe-key")
    research_resume = research_subparsers.add_parser("resume")
    research_resume.add_argument("--workflow", required=True)
    research_resume.add_argument("--run-id", required=True)
    research_resume.add_argument("--input-id", required=True)
    research_resume.add_argument("--input", required=True)
    research_evidence_fixture = research_subparsers.add_parser("evidence-fixture")
    research_evidence_fixture.add_argument("--workflow", required=True)
    research_evidence_fixture.add_argument(
        "--case",
        "--fixture-case",
        dest="case",
        required=True,
    )

    smoke_parser = subparsers.add_parser("smoke")
    smoke_parser.add_argument("--workflow", required=True)
    smoke_parser.add_argument(
        "--harness",
        action="append",
        choices=[item.value for item in Harness],
        help="Limit smoke to one harness; repeat for more than one.",
    )

    run = subparsers.add_parser("run")
    run.add_argument("--workflow", required=True)
    run.add_argument("--once", action="store_true")
    run.add_argument("--run-id")
    run.add_argument("--dispatcher-fixture", "--artifact-fixture", dest="dispatcher_fixture")

    artifact_fixture = subparsers.add_parser("artifact-fixture")
    artifact_fixture.add_argument("--workflow", required=True)
    artifact_fixture.add_argument("--case", "--fixture-case", dest="case")
    artifact_fixture.add_argument("--case-id")

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--workflow", required=True)
    enqueue.add_argument("--harness", required=True, choices=[item.value for item in Harness])
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--prompt-file", required=True)
    enqueue.add_argument("--dedupe-key", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config: WorkflowConfig | None = None
    try:
        config = load_workflow(args.workflow)
        match args.command:
            case "doctor":
                return doctor(config)
            case "seed":
                _require_schema_v1(config, args.command)
                added, existing = Coordinator(config).seed()
                print(json.dumps({"added": added, "existing": existing}, sort_keys=True))
                return 0
            case "enqueue":
                _require_schema_v1(config, args.command)
                prompt_file = Path(args.prompt_file).expanduser().resolve()
                job_id, created = Coordinator(config).enqueue_prompt_file(
                    harness=Harness(args.harness),
                    title=args.title,
                    prompt_file=prompt_file,
                    dedupe_key=args.dedupe_key,
                )
                print(json.dumps({"created": created, "job_id": str(job_id)}, sort_keys=True))
                return 0
            case "run":
                if config.schema_version == 2:
                    if args.dispatcher_fixture is not None:
                        return _run_artifact_fixture(
                            config,
                            args.dispatcher_fixture,
                        )
                    return _run_v2_tick(config, run_id=args.run_id)
                if args.dispatcher_fixture is not None or args.run_id is not None:
                    raise ConfigError("schema_mismatch: run_options_require_schema_v2")
                coordinator = Coordinator(config)
                if args.once:
                    print(json.dumps(coordinator.run_once(), sort_keys=True))
                    return 0
                try:
                    coordinator.run_forever()
                except KeyboardInterrupt:
                    print("coordinator_stopped", file=sys.stderr)
                return 0
            case "status":
                if config.schema_version == 2:
                    return _v2_status(config, run_id=args.run_id)
                _require_schema_v1(config, args.command)
                if args.run_id is not None:
                    raise ConfigError("schema_mismatch: status_run_id_requires_schema_v2")
                store = Store(config.state_db)
                store.initialize()
                print(
                    json.dumps(
                        {
                            "counts": store.status_counts(config.name),
                            "jobs": store.jobs(config.name),
                            "workflow": config.name,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            case "v2-status":
                _require_schema_v2(config, args.command)
                return _v2_status(config, run_id=args.run_id)
            case "inspect":
                _require_schema_v2(config, args.command)
                return _v2_inspect(
                    config,
                    run_id=args.run_id,
                    dedupe_key=args.dedupe_key,
                )
            case "start":
                _require_schema_v2(config, args.command)
                return _v2_start(
                    config,
                    dedupe_key=args.dedupe_key,
                    input_value=args.input,
                )
            case "research":
                _require_schema_v2(config, "research")
                if config.executor is None or config.executor.kind.value != "research-synthesis":
                    raise ConfigError("executor_mismatch: research_requires_research_synthesis")
                return _research_command(config, args)
            case "artifact-fixture":
                _require_schema_v2(config, args.command)
                return _run_artifact_fixture(
                    config,
                    args.case or args.case_id or _missing_fixture_case(),
                )
            case "smoke":
                _require_schema_v1(config, args.command)
                return smoke(config, selected_harnesses=args.harness)
    except (
        ConfigError,
        ArtifactError,
        ExecutorStoreError,
        KernelError,
        ValueError,
    ) as exc:
        if (
            config is not None
            and config.schema_version == 2
            and getattr(args, "command", None) in {
                "start",
                "status",
                "v2-status",
                "inspect",
                "run",
                "artifact-fixture",
                "research",
            }
        ):
            _print_v2_error(exc)
            return 64
        if config is None and getattr(args, "command", None) == "research":
            print(
                json.dumps(
                    {
                        "schema_version": 2,
                        "success": False,
                        "code": _error_code(exc),
                        "reason": str(exc),
                    },
                    sort_keys=True,
                )
            )
            return 64
        print(str(exc), file=sys.stderr)
        return 2
    return 2


def doctor(workflow: WorkflowConfig) -> int:
    checks: list[dict[str, object]] = []
    checks.append(
        {
            "check": "HERDR_ENV",
            "ok": os.environ.get("HERDR_ENV") == "1",
            "value": os.environ.get("HERDR_ENV"),
        }
    )
    checks.append(
        {
            "check": "HERDR_PANE_ID",
            "ok": bool(os.environ.get("HERDR_PANE_ID")),
            "value": os.environ.get("HERDR_PANE_ID"),
        }
    )
    checks.append(
        {
            "check": "HERDR_WORKSPACE_ID",
            "ok": bool(os.environ.get("HERDR_WORKSPACE_ID")),
            "value": os.environ.get("HERDR_WORKSPACE_ID"),
        }
    )
    herdr_path = shutil.which("herdr")
    checks.append({"check": "herdr", "ok": herdr_path is not None, "value": herdr_path})
    if herdr_path is not None:
        version = subprocess.run(
            ["herdr", "--version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
        checks.append(
            {
                "check": "herdr_version",
                "ok": version.returncode == 0,
                "value": version.stdout.strip(),
            }
        )
    for worker in workflow.workers:
        executable = shutil.which(worker.harness.value)
        checks.append(
            {
                "check": f"harness:{worker.harness.value}",
                "ok": executable is not None,
                "value": executable,
            }
        )
    ok = all(bool(check["ok"]) for check in checks)
    payload: dict[str, object] = {"checks": checks, "ok": ok}
    if workflow.schema_version == 2:
        payload.update(
            {
                "executor": workflow.executor.kind.value if workflow.executor else None,
                "schema_version": workflow.schema_version,
            }
        )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0 if ok else 1


def _require_schema_v2(workflow: WorkflowConfig, command: str) -> None:
    if workflow.schema_version != 2:
        raise ConfigError(f"schema_mismatch: {command}_requires_schema_v2")


def _v2_kernel(config: WorkflowConfig, store: ExecutorStore) -> ExecutionKernel:
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


def _v2_start(
    config: WorkflowConfig,
    *,
    dedupe_key: str,
    input_value: str,
) -> int:
    store = ExecutorStore(config.state_db)
    kernel = _v2_kernel(config, store)
    executor = config.executor
    if executor is None:
        raise ConfigError("executor_missing")
    workflow_text = config.path.read_text(encoding="utf-8")
    worker = config.workers[0]
    definitions = {
        "workflow": {
            "path": str(config.path),
            "name": config.name,
            "schema_version": config.schema_version,
            "source": workflow_text,
        },
        "config": {
            "coordinator": {
                "max_parallel": config.coordinator.max_parallel,
                "lease_seconds": config.coordinator.lease_seconds,
                "max_attempts": config.coordinator.max_attempts,
            },
            "executor": dict(executor.settings),
        },
        "input": {"text": input_value},
        "source": None,
        "route": {
            "workers": [
                {
                    "name": item.name,
                    "harness": item.harness.value,
                    "replicas": item.replicas,
                }
                for item in config.workers
            ]
        },
        "profile": {"name": "default"},
        "prompt": {"version": 1},
        "static_checks": [],
        "contract": {"version": "kernel-v2"},
        "executor": {"kind": executor.kind.value, "version": 1},
        "artifact_contract": {
            "version": ARTIFACT_CONTRACT_VERSION,
            "schema_version": 1,
        },
    }
    run_id, created = store.create_run(
        config.name,
        executor.kind.value,
        dedupe_key,
        workflow_definition=definitions["workflow"],
        config_definition=definitions["config"],
        input_value=definitions["input"],
        source_definition=definitions["source"],
        route_definition=definitions["route"],
        profile_definition=definitions["profile"],
        prompt_definition=definitions["prompt"],
        static_checks=definitions["static_checks"],
        contract_definition=definitions["contract"],
        executor_definition=definitions["executor"],
        artifact_contract_definition=definitions["artifact_contract"],
    )
    work = kernel.add_work_item(
        run_id,
        "main",
        worker=worker.name,
        harness=worker.harness.value,
        payload={
            "input": input_value,
            "assigned_path": "result.json",
            "lineage": ["main"],
        },
    )
    print(
        json.dumps(
            {
                "schema_version": 2,
                "executor": executor.kind.value,
                "route": "start",
                "success": True,
                "run_id": run_id,
                "created": created,
                "work_id": work.work_id,
                "state": store.require_run(run_id).state,
                "pinned_manifest": store.require_run(run_id).manifest.to_dict(),
            },
            sort_keys=True,
        )
    )
    return 0


def _research_command(config: WorkflowConfig, args: argparse.Namespace) -> int:
    from herdr_orchestrator.research_cli import research_command

    return research_command(config, args)


def _v2_status(config: WorkflowConfig, *, run_id: str | None) -> int:
    store = ExecutorStore(config.state_db)
    kernel = _v2_kernel(config, store)
    if run_id is not None:
        run = store.require_run(run_id)
        if run.workflow != config.name:
            raise ExecutorStoreError("run_not_found")
    payload = kernel.status(run_id, workflow=config.name)
    payload.update(
        {
            "executor": config.executor.kind.value if config.executor else None,
            "workflow": config.name,
            "route": "status",
            "success": True,
        }
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _select_v2_run(
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


def _v2_inspect(
    config: WorkflowConfig,
    *,
    run_id: str | None,
    dedupe_key: str | None,
) -> int:
    store = ExecutorStore(config.state_db)
    selected = _select_v2_run(
        store,
        config.name,
        run_id=run_id,
        dedupe_key=dedupe_key,
    )
    kernel = _v2_kernel(config, store)
    payload = kernel.inspect_run(selected)
    payload.update(
        {
            "route": "inspect",
            "executor": config.executor.kind.value,
            "success": True,
        }
    )
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


_ARTIFACT_FIXTURE_CASES = frozenset(
    {
        "valid",
        "lifecycle-only",
        "missing-output",
        "malformed",
        "unknown-key",
        "oversize",
        "wrong-run",
        "wrong-work",
        "wrong-attempt",
        "wrong-token",
        "wrong-digest",
        "wrong-input",
        "invalid-lineage",
        "absolute-path",
        "parent-path",
        "path-escape",
        "symlink-escape",
        "unassigned-output",
        "another-attempt",
        "cross-attempt",
        "stale",
        "stale-token",
    }
)
_ARTIFACT_FIXTURE_ALIASES = {
    "missing-artifact": "missing-output",
    "missing-assigned-output": "missing-output",
    "malformed-output": "malformed",
    "unknown-keys": "unknown-key",
    "oversized": "oversize",
    "wrong-run-id": "wrong-run",
    "wrong-work-id": "wrong-work",
    "wrong-attempt-id": "wrong-attempt",
    "wrong-fencing-token": "wrong-token",
    "digest-mismatch": "wrong-digest",
    "lineage-mismatch": "invalid-lineage",
    "absolute-escape": "absolute-path",
    "parent-escape": "parent-path",
    "symlink-escape": "symlink-escape",
    "unassigned": "unassigned-output",
    "cross-attempt": "another-attempt",
    "another-attempt-target": "another-attempt",
}


def _missing_fixture_case() -> str:
    raise ConfigError("artifact_fixture_case_required")


def _run_artifact_fixture(config: WorkflowConfig, case_id: str) -> int:
    fixture_workspace = Path(
        tempfile.mkdtemp(
            prefix=".artifact-fixture-state-",
            dir=config.workspace,
        )
    )
    isolated_config = replace(
        config,
        workspace=fixture_workspace,
        state_db=fixture_workspace / "state.db",
        runtime_dir=fixture_workspace / "runtime",
    )
    try:
        return _run_artifact_fixture_in_state(isolated_config, case_id)
    finally:
        shutil.rmtree(fixture_workspace)


def _run_artifact_fixture_in_state(config: WorkflowConfig, case_id: str) -> int:
    requested_case_id = case_id
    case_id = _ARTIFACT_FIXTURE_ALIASES.get(case_id, case_id)
    if case_id not in _ARTIFACT_FIXTURE_CASES:
        raise ConfigError(f"artifact_fixture_unknown_case: {requested_case_id}")
    store = ExecutorStore(config.state_db)
    kernel = _v2_kernel(config, store)
    dedupe_key = f"artifact-fixture-{requested_case_id}"
    worker = config.workers[0]
    run_id, created = store.create_run(
        config.name,
        config.executor.kind.value if config.executor else "unknown",
        dedupe_key,
        workflow_definition={"name": config.name, "schema_version": 2},
        config_definition={"fixture": requested_case_id},
        input_value={"fixture_case_id": requested_case_id},
        route_definition={"worker": worker.name},
        contract_definition={"version": "kernel-v2"},
        executor_definition={
            "kind": config.executor.kind.value if config.executor else "unknown",
            "version": 1,
        },
        artifact_contract_definition={
            "version": ARTIFACT_CONTRACT_VERSION,
            "schema_version": 1,
        },
    )
    if created:
        assigned_path = (
            "other-attempt/result.json"
            if case_id == "another-attempt"
            else "result.json"
        )
        kernel.add_work_item(
            run_id,
            "fixture-work",
            worker=worker.name,
            harness=worker.harness.value,
            payload={
                "fixture_case_id": requested_case_id,
                "assigned_path": assigned_path,
                "lineage": ["fixture-work"],
            },
        )
    existing = kernel.inspect_run(run_id)
    if not created or existing["work_items"][0]["state"] in {"succeeded", "failed"}:
        _print_fixture_result(
            case_id=requested_case_id,
            run_id=run_id,
            created=created,
            code="already_settled",
            reason="fixture_is_idempotent",
            attempt_consumed=False,
            state=existing["work_items"][0]["state"],
            run_state=existing["state"],
            inspect=existing,
            max_attempts=config.coordinator.max_attempts,
        )
        return 0
    claims = kernel.claim_ready(run_id)
    if not claims:
        current = kernel.inspect_run(run_id)
        _print_fixture_result(
            case_id=requested_case_id,
            run_id=run_id,
            created=created,
            code="already_observed",
            reason="fixture_attempt_already_active",
            attempt_consumed=False,
            state=current["work_items"][0]["state"],
            run_state=current["state"],
            inspect=current,
            max_attempts=config.coordinator.max_attempts,
        )
        return 0
    claim = claims[0]
    manifest = store.require_run(run_id).manifest
    pinned = {field: getattr(manifest, field) for field in MANIFEST_DIGEST_FIELDS}
    fixture_root = kernel.attempt_roots(claim).ensure()
    content = b'{"fixture":"valid"}'
    digest = digest_bytes(content)
    size = len(content)
    envelope = _fixture_envelope(claim, pinned, digest=digest, size=size)
    outcome_code = "artifact_invalid"
    outcome_reason = "typed_artifact_required"
    attempt_consumed = False
    final_state = "running"
    outside_sentinel: Path | None = None
    other_attempt_root: Path | None = None
    try:
        if case_id in {"stale", "stale-token"}:
            kernel.stage_artifact(claim, "result.json", b"stale")
            old_claim = claim
            now = old_claim.lease_until
            stale_kernel = ExecutionKernel(
                ExecutorStore(config.state_db),
                runtime_dir=config.runtime_dir,
                lease_seconds=1,
                clock=lambda: now,
                replica_slots={
                    worker.name: tuple(
                        f"ho-{worker.harness.value}-{index:02d}"
                        for index in range(1, worker.replicas + 1)
                    )
                },
            )
            stale_kernel.reclaim_expired(run_id, now=now)
            replacement = stale_kernel.claim_ready(run_id)[0]
            replacement_content = b'{"fixture":"replacement"}'
            replacement_digest = digest_bytes(replacement_content)
            stale_kernel.stage_artifact(
                replacement,
                "result.json",
                replacement_content,
            )
            replacement_envelope = _fixture_envelope(
                replacement,
                pinned,
                digest=replacement_digest,
                size=len(replacement_content),
            )
            stale_kernel.complete_work_item(replacement, artifact=replacement_envelope)
            stale_kernel.transition_run(run_id, "succeeded")
            try:
                kernel.complete_work_item(old_claim, artifact=envelope)
            except ClaimLostError:
                pass
            outcome_code = "stale_attempt"
            outcome_reason = "replacement_token_fenced_old_attempt"
            attempt_consumed = False
            final_state = "succeeded"
            stale_kernel.cleanup_attempt(old_claim)
            stale_kernel.cleanup_attempt(replacement)
        else:
            if case_id in {"valid", "lifecycle-only"}:
                kernel.stage_artifact(claim, "result.json", content)
                if case_id == "valid":
                    if kernel.artifacts is None:
                        raise KernelError("artifact_runtime_dir_required")
                    kernel.artifacts.stage_scratch(
                        claim,
                        "intermediate.tmp",
                        b"scratch",
                    )
                    kernel.stage_artifact(claim, "unassigned.json", b"unassigned")
            elif case_id == "missing-output":
                fixture_root.ensure()
            elif case_id == "malformed":
                kernel.stage_artifact(claim, "result.json", content)
                envelope["size_bytes"] = "not-an-integer"
            elif case_id == "unknown-key":
                kernel.stage_artifact(claim, "result.json", content)
                envelope["unknown"] = True
            elif case_id == "oversize":
                fixture_root.ensure()
                fixture_root.assigned_output("result.json").write_bytes(
                    b"x" * (MAX_ARTIFACT_BYTES + 1)
                )
                envelope["size_bytes"] = MAX_ARTIFACT_BYTES + 1
                envelope["content_digest"] = digest_bytes(b"x" * (MAX_ARTIFACT_BYTES + 1))
            elif case_id in {"absolute-path"}:
                kernel.stage_artifact(claim, "result.json", content)
                envelope["path"] = str(fixture_root.assigned_output("result.json"))
            elif case_id in {"parent-path", "path-escape"}:
                envelope["path"] = (
                    "../outside.json"
                    if case_id == "parent-path"
                    else "nested/../../outside.json"
                )
            elif case_id == "another-attempt":
                other_attempt_root = (
                    fixture_root.root.parent
                    / f"{claim.attempt_number + 1}-fence_fixture_other"
                )
                (other_attempt_root / "out").mkdir(parents=True, exist_ok=True)
                (other_attempt_root / "out" / "result.json").write_bytes(
                    b"other-attempt"
                )
                os.symlink(other_attempt_root / "out", fixture_root.out / "other-attempt")
                envelope["path"] = "other-attempt/result.json"
            elif case_id == "symlink-escape":
                sentinel_dir = Path(
                    tempfile.mkdtemp(
                        prefix=".artifact-fixture-",
                        dir=config.workspace,
                    )
                )
                outside_sentinel = sentinel_dir / "sentinel"
                outside_sentinel.write_bytes(b"sentinel")
                fixture_root.ensure()
                os.symlink(outside_sentinel, fixture_root.out / "result.json")
            elif case_id == "unassigned-output":
                kernel.stage_artifact(claim, "other.json", content)
                envelope["path"] = "other.json"
            else:
                kernel.stage_artifact(claim, "result.json", content)
            if case_id == "wrong-run":
                envelope["run_id"] = "run_wrong"
            elif case_id == "wrong-work":
                envelope["work_id"] = "work_wrong"
                envelope["logical_id"] = "work_wrong"
            elif case_id == "wrong-attempt":
                envelope["attempt_id"] = "attempt_wrong"
            elif case_id in {"wrong-token"}:
                envelope["fencing_token"] = "fence_wrong"
            elif case_id == "wrong-digest":
                envelope["content_digest"] = digest_bytes(b"wrong")
            elif case_id == "wrong-input":
                envelope["pinned_digests"] = {
                    **pinned,
                    "input_digest": digest_bytes(b"wrong-input"),
                }
            elif case_id == "invalid-lineage":
                envelope["lineage"] = []
            elif case_id == "lifecycle-only":
                observation = kernel.record_lifecycle_settlement(claim, lifecycle="done")
                outcome_code = "lifecycle_observed"
                outcome_reason = "typed_artifact_required"
                kernel.retry_work_item(claim, error_code="missing_artifact")
                final_state = kernel.get_work_item(run_id, claim.work_id).state
                _print_fixture_result(
                    case_id=requested_case_id,
                    run_id=run_id,
                    created=created,
                    code=outcome_code,
                    reason=outcome_reason,
                    attempt_consumed=True,
                    state=final_state,
                    run_state=store.require_run(run_id).state,
                    inspect=kernel.inspect_run(run_id),
                    observation=observation,
                    max_attempts=kernel.max_attempts,
                )
                kernel.cleanup_attempt(claim)
                if outside_sentinel is not None:
                    outside_sentinel.unlink(missing_ok=True)
                return 0
            kernel.complete_work_item(claim, artifact=envelope)
            kernel.transition_run(run_id, "succeeded")
            outcome_code = "artifact_admitted"
            outcome_reason = "typed_artifact_and_receipt_committed"
            attempt_consumed = True
            final_state = "succeeded"
            kernel.cleanup_attempt(claim)
    except (ArtifactValidationError, ArtifactError, ClaimLostError) as exc:
        outcome_code = _error_code(exc)
        outcome_reason = str(exc)
        try:
            kernel.retry_work_item(claim, error_code=outcome_code)
            attempt_consumed = True
        except ClaimLostError:
            pass
        final_state = kernel.get_work_item(run_id, claim.work_id).state
        kernel.cleanup_attempt(claim)
    finally:
        if outside_sentinel is not None:
            outside_sentinel.unlink(missing_ok=True)
            outside_sentinel.parent.rmdir()
        if other_attempt_root is not None and other_attempt_root.exists():
            shutil.rmtree(other_attempt_root)
    inspect = kernel.inspect_run(run_id)
    _print_fixture_result(
        case_id=requested_case_id,
        run_id=run_id,
        created=created,
        code=outcome_code,
        reason=outcome_reason,
        attempt_consumed=attempt_consumed,
        state=final_state,
        run_state=store.require_run(run_id).state,
        inspect=inspect,
        max_attempts=kernel.max_attempts,
    )
    return 0


def _fixture_envelope(
    claim: object,
    pinned: dict[str, str],
    *,
    digest: str,
    size: int,
) -> dict[str, object]:
    return {
        "contract_version": ARTIFACT_CONTRACT_VERSION,
        "artifact_type": "fixture-result",
        "run_id": claim.run_id,
        "work_id": claim.work_id,
        "logical_id": claim.work_id,
        "attempt_id": claim.attempt_id,
        "fencing_token": claim.fencing_token,
        "path": "result.json",
        "content_digest": digest,
        "size_bytes": size,
        "lineage": [claim.work_id],
        "pinned_digests": pinned,
        "harness": claim.harness,
        "worker": claim.worker,
        "agent": claim.agent_name,
        "pane": f"pane:{claim.agent_name}",
        "payload": {"fixture": "artifact"},
    }


def _print_fixture_result(
    *,
    case_id: str,
    run_id: str,
    created: bool,
    code: str,
    reason: str,
    attempt_consumed: bool,
    state: str,
    run_state: str,
    inspect: dict[str, object],
    observation: dict[str, object] | None = None,
    max_attempts: int = 1,
) -> None:
    receipts = inspect.get("receipts", [])
    artifacts = inspect.get("artifacts", [])
    has_semantic_receipt = any(item["kind"] == "semantic" for item in receipts)
    has_transport_receipt = any(item["kind"] != "semantic" for item in receipts)
    has_stale_artifact = any(item["state"] == "stale" for item in artifacts)
    if has_stale_artifact and not attempt_consumed:
        attempt_consumption_decision = "stale_rejected_no_budget"
    elif has_semantic_receipt:
        attempt_consumption_decision = "semantic_settlement"
    elif has_transport_receipt:
        attempt_consumption_decision = "transport_retry"
    else:
        attempt_consumption_decision = "not_consumed"
    payload: dict[str, object] = {
        "schema_version": 2,
        "route": "artifact-fixture",
        "success": code == "artifact_admitted",
        "fixture_case_id": case_id,
        "bound": {
            "max_bytes": MAX_ARTIFACT_BYTES,
            "max_attempts": max_attempts,
            "one_submission": True,
        },
        "run_id": run_id,
        "created": created,
        "code": code,
        "reason": reason,
        "attempt_consumed": attempt_consumed,
        "attempt_consumption_decision": attempt_consumption_decision,
        "state": state,
        "run_state": run_state,
        "admitted_artifacts": [
            item["artifact_id"]
            for item in artifacts
            if item["state"] == "admitted"
        ],
        "rejected_artifacts": [
            {
                "artifact_id": item["artifact_id"],
                "state": item["state"],
                "error_code": item["error_code"],
            }
            for item in artifacts
            if item["state"] != "admitted"
        ],
        "semantic_receipts": sum(
            1
            for item in receipts
            if item["kind"] == "semantic"
        ),
        "transport_receipts": sum(
            1
            for item in receipts
            if item["kind"] != "semantic"
        ),
        "events": len(inspect.get("events", [])),
        "inspect": inspect,
    }
    if observation is not None:
        payload["observation"] = observation
    print(json.dumps(payload, sort_keys=True))


def _run_v2_tick(config: WorkflowConfig, *, run_id: str | None) -> int:
    store = ExecutorStore(config.state_db)
    kernel = _v2_kernel(config, store)
    if run_id is not None:
        store.require_run(run_id)
        selected = [run_id]
    else:
        selected = [run.run_id for run in store.runs(config.name)]
    reclaimed: dict[str, list[str]] = {}
    claims: list[dict[str, object]] = []
    for selected_run in selected:
        reclaimed[selected_run] = kernel.reclaim_expired(selected_run)
    if selected:
        claims = [
            claim.to_dict()
            for claim in kernel.claim_ready_for_workflow(
                config.name,
                run_ids=selected,
            )
        ]
    print(
        json.dumps(
            {
                "schema_version": 2,
                "executor": config.executor.kind.value if config.executor else None,
                "route": "run",
                "success": True,
                "once": True,
                "considered_run_ids": selected,
                "reclaimed_attempt_ids": reclaimed,
                "claimed": claims,
                "states": [
                    {
                        "run_id": item,
                        "state": store.require_run(item).state,
                    }
                    for item in selected
                ],
            },
            sort_keys=True,
        )
    )
    return 0


def _error_code(error: BaseException) -> str:
    message = str(error)
    return message.split(":", 1)[0] if message else type(error).__name__.lower()


def _print_v2_error(error: BaseException) -> None:
    print(
        json.dumps(
            {
                "schema_version": 2,
                "success": False,
                "code": _error_code(error),
                "reason": str(error),
            },
            sort_keys=True,
        )
    )


def _require_schema_v1(workflow: WorkflowConfig, command: str) -> None:
    if workflow.schema_version != 1:
        raise ConfigError(f"schema_mismatch: {command}_requires_schema_v1")


def smoke(
    workflow: WorkflowConfig,
    *,
    selected_harnesses: list[str] | None = None,
) -> int:
    requested = _requested_smoke_harnesses(workflow, selected_harnesses)
    enabled = {worker.harness.value for worker in workflow.workers}
    absent = [harness for harness in requested if harness not in enabled]
    if absent:
        reason = f"harness_not_enabled:{','.join(absent)}"
        failure_rows = [
            {
                "harness": harness,
                "error": "harness_not_enabled",
            }
            for harness in absent
        ]
        _print_smoke_payload(
            failures=failure_rows,
            results=[],
            success=False,
            code="harness_not_enabled",
            reason=reason,
            workflow=_smoke_workflow_summary(workflow),
            selected_harnesses=requested,
            dispatched_harnesses=[],
        )
        return 1

    transport = HerdrTransport(workflow.name, workflow.workspace)
    failures: list[dict[str, object]] = []
    results: list[dict[str, object]] = []
    created_names: list[str] = []
    dispatched_harnesses: list[str] = []
    selected = set(requested)
    workers = [
        worker
        for worker in workflow.workers
        if worker.harness.value in selected
    ]
    try:
        for worker in workers:
            harness = worker.harness
            metadata = _smoke_probe_metadata(workflow, harness)
            metadata_json = json.dumps(
                metadata,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            prompt_metadata_digest = _smoke_metadata_digest(metadata)
            prompt = (
                "This is a read-only workflow lifecycle probe. Do not modify or create "
                "files, access the network, or perform external actions. Use only the "
                "canonical supplied workflow identified in the following metadata, and "
                "briefly acknowledge the metadata after the turn settles.\n"
                f"probe_metadata={metadata_json}\n"
                f"prompt_metadata_digest={prompt_metadata_digest}"
            )
            name = smoke_agent_name(workflow.name, harness)
            dispatched_harnesses.append(harness.value)
            try:
                outcome = transport.dispatch(
                    harness,
                    prompt,
                    timeout_seconds=workflow.coordinator.agent_timeout_seconds,
                    agent_name=name,
                )
            except Exception as exc:
                failures.append(
                    _smoke_failure(
                        harness,
                        metadata,
                        prompt_metadata_digest,
                        name,
                        error=f"dispatch_{type(exc).__name__.lower()}",
                    )
                )
                continue
            if not outcome.member_reused and outcome.agent_name == name:
                if name not in created_names:
                    created_names.append(name)
            probe = _smoke_probe_result(
                harness,
                name,
                metadata,
                prompt_metadata_digest,
                outcome,
            )
            if probe["error"] is not None:
                failures.append(probe)
            else:
                probe.pop("error")
                probe.pop("reason")
                results.append(probe)
    finally:
        owned_names = list(created_names)
        transport_owned_names = getattr(transport, "created_agent_names", ())
        if isinstance(transport_owned_names, (list, tuple, set, frozenset)):
            names = transport_owned_names
        else:
            names = ()
        for name in names:
            if name not in owned_names:
                owned_names.append(name)
        for name in reversed(owned_names):
            try:
                transport.close_created_agent(name)
            except Exception as exc:
                failures.append(
                    {
                        "harness": name,
                        "error": f"cleanup:{type(exc).__name__.lower()}",
                        "reason": "validation_owned_resource_cleanup_failed",
                    }
                )
    success = not failures and len(results) == len(workers)
    _print_smoke_payload(
        failures=failures,
        results=results,
        success=success,
        code=None if success else "smoke_failed",
        reason=None if success else "one_or_more_probes_failed",
        workflow=_smoke_workflow_summary(workflow),
        selected_harnesses=requested,
        dispatched_harnesses=dispatched_harnesses,
    )
    return 0 if success else 1


def _requested_smoke_harnesses(
    workflow: WorkflowConfig,
    selected_harnesses: list[str] | None,
) -> list[str]:
    if not selected_harnesses:
        return [worker.harness.value for worker in workflow.workers]
    # Keep the first occurrence so repeated --harness arguments cannot create
    # multiple probes for one enabled harness.
    return list(dict.fromkeys(selected_harnesses))


def _smoke_probe_metadata(
    workflow: WorkflowConfig,
    harness: Harness,
) -> dict[str, object]:
    return {
        **_smoke_workflow_summary(workflow),
        "selected_harness": harness.value,
    }


def _smoke_workflow_summary(workflow: WorkflowConfig) -> dict[str, object]:
    return {
        "workflow_path": str(workflow.path.resolve()),
        "workflow_name": workflow.name,
        "schema_version": workflow.schema_version,
        "worker_count": len(workflow.workers),
        "total_replica_capacity": sum(worker.replicas for worker in workflow.workers),
    }


def _smoke_metadata_digest(metadata: dict[str, object]) -> str:
    return definition_digest(metadata)


def _smoke_probe_result(
    harness: Harness,
    requested_name: str,
    metadata: dict[str, object],
    prompt_metadata_digest: str,
    outcome: object,
) -> dict[str, object]:
    state = getattr(outcome, "state", AgentState.UNKNOWN)
    state_value = state.value if isinstance(state, AgentState) else str(state)
    agent_name = getattr(outcome, "agent_name", None)
    pane_id = getattr(outcome, "pane_id", None)
    prompt_accepted = getattr(outcome, "prompt_accepted", None)
    dispatch_attempted = getattr(outcome, "dispatch_attempted", None)
    member_reused = getattr(outcome, "member_reused", None)
    baseline_sequence = getattr(outcome, "baseline_state_change_seq", None)
    final_sequence = getattr(outcome, "final_state_change_seq", None)
    error_code = getattr(outcome, "error_code", None)

    if dispatch_attempted is not True:
        error = "dispatch_not_confirmed"
    elif not isinstance(member_reused, bool):
        error = "resource_ownership_unverified"
    elif agent_name != requested_name:
        error = "agent_identity_mismatch"
    elif error_code is not None:
        error = str(error_code)
    elif prompt_accepted is not True:
        error = "prompt_not_accepted"
    elif state not in {AgentState.IDLE, AgentState.DONE}:
        error = state_value
    elif not isinstance(pane_id, str) or not pane_id:
        error = "pane_identity_missing"
    elif not (
        isinstance(baseline_sequence, int)
        and not isinstance(baseline_sequence, bool)
        and isinstance(final_sequence, int)
        and not isinstance(final_sequence, bool)
    ):
        error = "lifecycle_sequence_unverified"
    elif (
        isinstance(baseline_sequence, int)
        and isinstance(final_sequence, int)
        and final_sequence <= baseline_sequence
    ):
        error = "lifecycle_unchanged"
    else:
        error = None

    lifecycle_advanced = (
        final_sequence > baseline_sequence
        if isinstance(baseline_sequence, int) and isinstance(final_sequence, int)
        else error is None
    )
    result: dict[str, object] = {
        **metadata,
        "harness": harness.value,
        "agent_name": agent_name,
        "pane_id": pane_id,
        "state": state_value if error is None else None,
        "settled_state": state_value if state in {AgentState.IDLE, AgentState.DONE} else None,
        "prompt_accepted": prompt_accepted if prompt_accepted is not None else error is None,
        "dispatch_attempted": dispatch_attempted if dispatch_attempted is not None else True,
        "member_reused": member_reused,
        "created_by_validation": member_reused is False,
        "baseline_state_change_seq": baseline_sequence,
        "final_state_change_seq": final_sequence,
        "lifecycle_sequence_advanced": lifecycle_advanced,
        "prompt_metadata": metadata,
        "prompt_metadata_digest": prompt_metadata_digest,
        "error": error,
        "reason": error,
    }
    return result


def _smoke_failure(
    harness: Harness,
    metadata: dict[str, object],
    prompt_metadata_digest: str,
    agent_name: str,
    *,
    error: str,
) -> dict[str, object]:
    return {
        **metadata,
        "harness": harness.value,
        "agent_name": agent_name,
        "pane_id": None,
        "state": None,
        "settled_state": None,
        "prompt_accepted": False,
        "dispatch_attempted": True,
        "member_reused": None,
        "created_by_validation": False,
        "baseline_state_change_seq": None,
        "final_state_change_seq": None,
        "lifecycle_sequence_advanced": False,
        "prompt_metadata": metadata,
        "prompt_metadata_digest": prompt_metadata_digest,
        "error": error,
        "reason": error,
    }


def _print_smoke_payload(
    *,
    failures: list[dict[str, object]],
    results: list[dict[str, object]],
    success: bool,
    code: str | None,
    reason: str | None,
    workflow: dict[str, object],
    selected_harnesses: list[str],
    dispatched_harnesses: list[str],
) -> None:
    payload: dict[str, object] = {
        "failures": failures,
        "results": results,
        "success": success,
        "workflow": workflow,
        "selected_harnesses": selected_harnesses,
        "dispatched_harnesses": dispatched_harnesses,
    }
    if code is not None:
        payload["code"] = code
    if reason is not None:
        payload["reason"] = reason
    print(json.dumps(payload, indent=2, sort_keys=True))
