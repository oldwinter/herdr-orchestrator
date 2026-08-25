from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import replace
from pathlib import Path

from herdr_orchestrator.catalog import (
    CatalogError,
    full_profile_payload,
    profile_for_harness,
    profiles_for_workers,
    render_compact_catalog,
)
from herdr_orchestrator.config import ConfigError, load_workflow
from herdr_orchestrator.dashboard import DashboardServer
from herdr_orchestrator.delivery import (
    DeliveryError,
    DeliveryEscalation,
    StandardizedDelivery,
)
from herdr_orchestrator.delivery_protocol import DeliveryArtifactError
from herdr_orchestrator.git_workspace import GitWorkspaceError
from herdr_orchestrator.herdr import HerdrTransport, doctor_agent_name, smoke_agent_name
from herdr_orchestrator.model import (
    AgentState,
    DispatchContext,
    Harness,
    HarnessProfile,
    JobState,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
    TrackerBackend,
    WayfinderMode,
    WorkflowConfig,
)
from herdr_orchestrator.protocol import TransportError
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.store import Store, StoreError
from herdr_orchestrator.tracker import TrackerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable multi-harness orchestration over Herdr.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("seed", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--workflow", required=True)

    doctor_parser = subparsers.add_parser("doctor")
    doctor_parser.add_argument("--workflow", required=True)
    doctor_parser.add_argument("--probe-timeout-seconds", type=int, default=30)
    doctor_parser.add_argument(
        "--harness",
        action="append",
        choices=[item.value for item in Harness],
        help="Limit readiness probes to one enabled harness; repeat for more than one.",
    )

    retry_parser = subparsers.add_parser("retry")
    retry_parser.add_argument("--workflow", required=True)
    retry_parser.add_argument("--job-id", type=int, required=True)
    retry_parser.add_argument("--extra-attempts", type=int, choices=range(1, 11), default=1)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--workflow", required=True)
    resume_parser.add_argument("--job-id", type=int, required=True)
    resume_parser.add_argument("--response-file", required=True)

    gc_parser = subparsers.add_parser("gc")
    gc_parser.add_argument("--workflow", required=True)
    gc_scope = gc_parser.add_mutually_exclusive_group(required=True)
    gc_scope.add_argument("--succeeded-agents", action="store_true")
    gc_scope.add_argument("--failed-agents", action="store_true")
    gc_parser.add_argument("--apply", action="store_true")

    smoke_parser = subparsers.add_parser("smoke")
    smoke_parser.add_argument("--workflow", required=True)
    smoke_parser.add_argument(
        "--harness",
        action="append",
        choices=[item.value for item in Harness],
        help="Limit smoke to one harness; repeat for more than one.",
    )

    catalog_parser = subparsers.add_parser("catalog")
    catalog_parser.add_argument("--workflow", required=True)
    catalog_parser.add_argument("--format", choices=("json", "text"), default="json")

    profile_parser = subparsers.add_parser("profile")
    profile_parser.add_argument("--workflow", required=True)
    profile_parser.add_argument("harness", choices=[item.value for item in Harness])
    profile_parser.add_argument("--format", choices=("json", "text"), default="text")

    dashboard = subparsers.add_parser("dashboard")
    dashboard.add_argument("--workflow", required=True)
    dashboard.add_argument("--host", default="127.0.0.1")
    dashboard.add_argument("--port", type=int, default=8765)
    dashboard.add_argument("--poll-seconds", type=float, default=2.0)

    run = subparsers.add_parser("run")
    run.add_argument("--workflow", required=True)
    run_mode = run.add_mutually_exclusive_group()
    run_mode.add_argument("--once", action="store_true")
    run_mode.add_argument(
        "--until-idle",
        "--drain",
        dest="until_idle",
        action="store_true",
    )
    run.add_argument("--drain-timeout-seconds", type=int, default=3600)
    _add_selection_arguments(run)

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--workflow", required=True)
    enqueue.add_argument(
        "--harness",
        choices=["auto", *(item.value for item in Harness)],
        default="auto",
    )
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--prompt-file", required=True)
    enqueue.add_argument("--dedupe-key", required=True)
    enqueue.add_argument(
        "--placement",
        choices=["auto", *(item.value for item in PlacementTarget)],
        default="auto",
        help="Override topology selection for this task.",
    )
    receipt = enqueue.add_mutually_exclusive_group()
    receipt.add_argument(
        "--receipt-prefix",
        help="Require this exact prefix in bounded agent output before success.",
    )
    receipt.add_argument(
        "--receipt-file",
        help="Require this non-empty path relative to the task execution root.",
    )
    _add_selection_arguments(enqueue)

    deliver = subparsers.add_parser("deliver")
    deliver.add_argument("--workflow", required=True)
    deliver.add_argument("--goal-file", required=True)
    deliver.add_argument(
        "--tracker-backend",
        choices=[item.value for item in TrackerBackend],
    )
    deliver.add_argument("--tracker-root")
    deliver.add_argument("--github-repository")
    deliver.add_argument(
        "--wayfinder",
        choices=[item.value for item in WayfinderMode],
    )
    deliver.add_argument("--max-parallel", type=int, choices=range(1, 4))
    deliver.add_argument("--review-repair-rounds", type=int, choices=range(0, 3))
    _add_selection_arguments(deliver)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = load_workflow(args.workflow)
        match args.command:
            case "doctor":
                if not 5 <= args.probe_timeout_seconds <= 300:
                    raise ValueError("doctor_probe_timeout_out_of_range")
                return doctor(
                    config,
                    probe_timeout_seconds=args.probe_timeout_seconds,
                    selected_harnesses=args.harness,
                )
            case "seed":
                added, existing = Coordinator(config).seed()
                print(json.dumps({"added": added, "existing": existing}, sort_keys=True))
                return 0
            case "retry":
                store = Store(config.state_db)
                store.initialize()
                result = store.retry_failed(
                    config.name,
                    args.job_id,
                    extra_attempts=args.extra_attempts,
                )
                print(json.dumps(result, sort_keys=True))
                return 0
            case "resume":
                response_file = Path(args.response_file).expanduser().resolve()
                if not response_file.is_file():
                    raise ValueError(f"response_file_not_found: {response_file}")
                result = Coordinator(config).resume_blocked(
                    args.job_id,
                    response_file.read_text(encoding="utf-8").strip(),
                )
                print(json.dumps(result, sort_keys=True))
                return 0 if result["state"] == JobState.SUCCEEDED.value else 1
            case "gc":
                coordinator = Coordinator(config)
                result = (
                    coordinator.gc_succeeded_agents(dry_run=not args.apply)
                    if args.succeeded_agents
                    else coordinator.gc_failed_agents(dry_run=not args.apply)
                )
                print(json.dumps(result, indent=2, sort_keys=True))
                return 0
            case "enqueue":
                prompt_file = Path(args.prompt_file).expanduser().resolve()
                job_id, created, selected = _coordinator_from_args(
                    config,
                    args,
                ).enqueue_prompt_file(
                    harness=None if args.harness == "auto" else Harness(args.harness),
                    title=args.title,
                    prompt_file=prompt_file,
                    dedupe_key=args.dedupe_key,
                    placement=(
                        None
                        if args.placement == "auto"
                        else PlacementTarget(args.placement)
                    ),
                    receipt=_task_receipt_from_args(args),
                )
                print(
                    json.dumps(
                        {
                            "created": created,
                            "harness": selected.value,
                            "job_id": job_id,
                        },
                        sort_keys=True,
                    )
                )
                return 0
            case "deliver":
                delivery_config = config.standardized_delivery
                if args.tracker_backend is not None:
                    delivery_config = replace(
                        delivery_config,
                        tracker_backend=TrackerBackend(args.tracker_backend),
                    )
                if args.tracker_root is not None:
                    delivery_config = replace(
                        delivery_config,
                        tracker_root=Path(args.tracker_root).expanduser().resolve(),
                    )
                if args.github_repository is not None:
                    delivery_config = replace(
                        delivery_config,
                        github_repository=args.github_repository,
                    )
                if args.wayfinder is not None:
                    delivery_config = replace(
                        delivery_config,
                        wayfinder=WayfinderMode(args.wayfinder),
                    )
                if args.max_parallel is not None:
                    delivery_config = replace(
                        delivery_config,
                        max_parallel=args.max_parallel,
                    )
                if args.review_repair_rounds is not None:
                    delivery_config = replace(
                        delivery_config,
                        review_repair_rounds=args.review_repair_rounds,
                    )
                if (
                    delivery_config.tracker_backend is TrackerBackend.GITHUB
                    and delivery_config.github_repository is None
                ):
                    raise ValueError("github_repository_required")
                delivery = StandardizedDelivery(
                    replace(config, standardized_delivery=delivery_config),
                    **_selection_kwargs(args),
                )
                result = delivery.run(Path(args.goal_file))
                print(
                    json.dumps(
                        {
                            "artifact_root": str(result.artifact_root),
                            "integration_branch": result.integration_branch,
                            "integration_commit": result.integration_commit,
                            "review_rounds": result.review_rounds,
                            "run_id": result.run_id,
                            "status": result.status,
                            "tickets_completed": result.tickets_completed,
                            "tracker_references": result.tracker_references,
                        },
                        indent=2,
                        sort_keys=True,
                    )
                )
                return 0
            case "run":
                coordinator = _coordinator_from_args(config, args)
                if args.once:
                    print(json.dumps(coordinator.run_once(), sort_keys=True))
                    return 0
                if args.until_idle:
                    if not 1 <= args.drain_timeout_seconds <= 86_400:
                        raise ValueError("drain_timeout_out_of_range")
                    result = coordinator.run_until_idle(
                        timeout_seconds=args.drain_timeout_seconds,
                    )
                    print(json.dumps(result, sort_keys=True))
                    return 0 if result["idle"] else 1
                try:
                    coordinator.run_forever()
                except KeyboardInterrupt:
                    print("coordinator_stopped", file=sys.stderr)
                return 0
            case "status":
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
            case "smoke":
                return smoke(config, selected_harnesses=args.harness)
            case "catalog":
                profiles = profiles_for_workers(config.profiles, config.workers)
                if args.format == "json":
                    print(render_compact_catalog(profiles))
                else:
                    print(_catalog_text(profiles))
                return 0
            case "dashboard":
                dashboard = DashboardServer(
                    config,
                    host=args.host,
                    port=args.port,
                    poll_seconds=args.poll_seconds,
                )
                host, port = dashboard.address
                print(
                    json.dumps(
                        {
                            "status": "dashboard_started",
                            "url": f"http://{host}:{port}",
                        },
                        sort_keys=True,
                    ),
                    flush=True,
                )
                try:
                    dashboard.serve_forever()
                except KeyboardInterrupt:
                    print("dashboard_stopped", file=sys.stderr)
                return 0
            case "profile":
                profile = profile_for_harness(
                    profiles_for_workers(config.profiles, config.workers),
                    Harness(args.harness),
                )
                payload = full_profile_payload(profile)
                if args.format == "json":
                    print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
                else:
                    profile_payload = payload["profile"]
                    assert isinstance(profile_payload, dict)
                    print(profile_payload["context"])
                return 0
    except DeliveryEscalation as exc:
        print(str(exc), file=sys.stderr)
        return 3
    except (
        CatalogError,
        ConfigError,
        DeliveryArtifactError,
        DeliveryError,
        GitWorkspaceError,
        StoreError,
        TransportError,
        TrackerError,
        ValueError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 2


def _task_receipt_from_args(args: argparse.Namespace) -> TaskReceipt | None:
    if args.receipt_prefix is not None:
        value = str(args.receipt_prefix).strip()
        if not value or len(value) > 256 or "\n" in value or "\r" in value:
            raise ValueError("receipt_prefix_invalid")
        return TaskReceipt(ReceiptKind.OUTPUT_PREFIX, value)
    if args.receipt_file is not None:
        value = str(args.receipt_file).strip()
        path = Path(value)
        if (
            not value
            or len(value) > 500
            or path.is_absolute()
            or not path.parts
            or ".." in path.parts
        ):
            raise ValueError("receipt_file_invalid")
        return TaskReceipt(ReceiptKind.FILE, path.as_posix())
    return None


ReadinessProbe = Callable[[WorkflowConfig, Harness, int], Mapping[str, object]]


def doctor(
    workflow: WorkflowConfig,
    *,
    environ: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    version_runner: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
    readiness_probe: ReadinessProbe | None = None,
    probe_timeout_seconds: int = 30,
    selected_harnesses: list[str] | None = None,
) -> int:
    current_environ = os.environ if environ is None else environ
    probe = readiness_probe or probe_harness_readiness
    checks: list[dict[str, object]] = []
    checks.append(
        {
            "check": "HERDR_ENV",
            "ok": current_environ.get("HERDR_ENV") == "1",
            "value": current_environ.get("HERDR_ENV"),
        }
    )
    checks.append(
        {
            "check": "HERDR_PANE_ID",
            "ok": bool(current_environ.get("HERDR_PANE_ID")),
            "value": current_environ.get("HERDR_PANE_ID"),
        }
    )
    checks.append(
        {
            "check": "HERDR_WORKSPACE_ID",
            "ok": bool(current_environ.get("HERDR_WORKSPACE_ID")),
            "value": current_environ.get("HERDR_WORKSPACE_ID"),
        }
    )
    herdr_path = which("herdr")
    checks.append({"check": "herdr", "ok": herdr_path is not None, "value": herdr_path})
    if herdr_path is not None:
        try:
            version = version_runner(
                ["herdr", "--version"],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            version = subprocess.CompletedProcess(["herdr", "--version"], 1, "", "")
        checks.append(
            {
                "check": "herdr_version",
                "ok": version.returncode == 0,
                "value": version.stdout.strip(),
            }
        )
    git_path = which("git")
    checks.append({"check": "git", "ok": git_path is not None, "value": git_path})
    if workflow.standardized_delivery.tracker_backend is TrackerBackend.GITHUB:
        github_path = which("gh")
        checks.append(
            {
                "check": "tracker:github",
                "ok": github_path is not None,
                "value": github_path,
            }
        )
    harnesses = [worker.harness for worker in workflow.workers]
    if workflow.planner.harness is not None and workflow.planner.harness not in harnesses:
        harnesses.append(workflow.planner.harness)
    if selected_harnesses:
        enabled = set(harnesses)
        selected = list(dict.fromkeys(Harness(value) for value in selected_harnesses))
        unavailable = [harness.value for harness in selected if harness not in enabled]
        if unavailable:
            raise ValueError(f"doctor_harness_not_enabled: {','.join(unavailable)}")
        harnesses = selected
    readiness_ms = 0
    for harness in harnesses:
        executable = which(harness.value)
        checks.append(
            {
                "check": f"harness:{harness.value}",
                "ok": executable is not None,
                "value": executable,
            }
        )
        profile = profile_for_harness(workflow.profiles, harness)
        profile_ok = profile.context_file.is_file()
        checks.append(
            {
                "check": f"profile:{harness.value}",
                "ok": profile_ok,
                "value": str(profile.context_file),
            }
        )
        environment_ready = (
            current_environ.get("HERDR_ENV") == "1"
            and bool(current_environ.get("HERDR_PANE_ID"))
            and bool(current_environ.get("HERDR_WORKSPACE_ID"))
            and herdr_path is not None
        )
        if executable is None or not profile_ok or not environment_ready:
            readiness: Mapping[str, object] = {
                "status": "unavailable",
                "error_code": (
                    "harness_unavailable"
                    if executable is None
                    else "profile_unavailable"
                    if not profile_ok
                    else "not_in_herdr"
                ),
                "error_summary": None,
            }
            duration_ms = 0
        else:
            probe_started = time.monotonic()
            try:
                readiness = probe(workflow, harness, probe_timeout_seconds)
            except Exception as exc:
                readiness = {
                    "status": "error",
                    "error_code": "readiness_probe_failed",
                    "error_summary": " ".join(str(exc).split())[:300] or None,
                }
            measured_ms = max(0, int((time.monotonic() - probe_started) * 1000))
            reported_ms = readiness.get("duration_ms")
            duration_ms = (
                int(reported_ms)
                if isinstance(reported_ms, int) and not isinstance(reported_ms, bool)
                else measured_ms
            )
        readiness_ms += duration_ms
        status = str(readiness.get("status", "error"))
        checks.append(
            {
                "check": f"readiness:{harness.value}",
                "ok": status == "ready",
                "status": status,
                "error_code": readiness.get("error_code"),
                "error_summary": readiness.get("error_summary"),
                "duration_ms": duration_ms,
                "phase_timings_ms": readiness.get("phase_timings_ms", {}),
            }
        )
    ok = all(bool(check["ok"]) for check in checks)
    passed = sum(bool(check["ok"]) for check in checks)
    print(
        json.dumps(
            {
                "checks": checks,
                "ok": ok,
                "summary": {
                    "checks": len(checks),
                    "failed": len(checks) - passed,
                    "harnesses": [harness.value for harness in harnesses],
                    "passed": passed,
                    "readiness_ms": readiness_ms,
                },
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if ok else 1


def probe_harness_readiness(
    workflow: WorkflowConfig,
    harness: Harness,
    timeout_seconds: int,
    *,
    transport: HerdrTransport | None = None,
) -> Mapping[str, object]:
    active_transport = transport or HerdrTransport(workflow.name, workflow.workspace)
    name = doctor_agent_name(workflow.name, harness)
    prefix = f"HERDR-DOCTOR-OK harness={harness.value}"
    started = time.monotonic()
    try:
        outcome = active_transport.dispatch(
            harness,
            (
                "Read-only readiness probe. Do not modify files or external state. "
                f"Reply with exactly this line: {prefix}"
            ),
            timeout_seconds=timeout_seconds,
            agent_name=name,
            context=DispatchContext(
                placement=PlacementTarget.TAB,
                title=f"doctor-{harness.value}",
                task_key=f"doctor-{harness.value}",
                receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, prefix),
            ),
        )
    finally:
        active_transport.close_created_agent(name)
    status_by_error = {
        "agent_auth_failed": "auth_required",
        "agent_auth_required": "auth_required",
        "agent_model_invalid": "model_invalid",
        "herdr_timeout": "timeout",
        "timeout": "timeout",
        "prompt_acceptance_timeout": "timeout",
        "agent_provider_failed": "error",
        "herdr_unavailable": "unavailable",
        "not_in_herdr": "unavailable",
    }
    if outcome.state in {AgentState.IDLE, AgentState.DONE} and outcome.task_verified is True:
        status = "ready"
    else:
        status = status_by_error.get(outcome.error_code or "", "error")
    return {
        "status": status,
        "error_code": outcome.error_code,
        "error_summary": outcome.error_summary,
        "duration_ms": max(0, int((time.monotonic() - started) * 1000)),
        "phase_timings_ms": outcome.phase_timings_ms or {},
    }


def smoke(
    workflow: WorkflowConfig,
    *,
    selected_harnesses: list[str] | None = None,
) -> int:
    transport = HerdrTransport(workflow.name, workflow.workspace)
    failures: list[dict[str, str]] = []
    results: list[dict[str, object]] = []
    created_names: list[str] = []
    selected = set(selected_harnesses or ())
    workers = [
        worker
        for worker in workflow.workers
        if not selected or worker.harness.value in selected
    ]
    try:
        for worker in workers:
            harness = worker.harness
            targets = _smoke_targets(workflow)
            prefix = f"HERDR-SMOKE-OK harness={harness.value}"
            prompt = (
                "This is a read-only connectivity check. Use local read-only tools to "
                f"inspect {', '.join(targets)}. Do not modify or create files, access the "
                "network, or perform external actions. Finish with one line that starts "
                f"exactly with: {prefix}"
            )
            name = smoke_agent_name(workflow.name, harness)
            outcome = transport.dispatch(
                harness,
                prompt,
                timeout_seconds=workflow.coordinator.agent_timeout_seconds,
                agent_name=name,
                context=DispatchContext(
                    placement=PlacementTarget.TAB,
                    title=f"smoke-{harness.value}",
                    task_key=f"smoke-{harness.value}",
                    receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, prefix),
                ),
            )
            if not outcome.member_reused:
                created_names.append(name)
            if outcome.error_code is not None or outcome.state not in {
                AgentState.IDLE,
                AgentState.DONE,
            } or outcome.task_verified is not True:
                failures.append(
                    {
                        "harness": harness.value,
                        "error": outcome.error_code or outcome.state.value,
                    }
                )
                continue
            results.append(
                {
                    "harness": harness.value,
                    "state": outcome.state.value,
                    "task_verified": True,
                }
            )
    finally:
        for name in reversed(created_names):
            try:
                transport.close_created_agent(name)
            except Exception as exc:
                failures.append({"harness": name, "error": f"cleanup:{type(exc).__name__}"})
    print(json.dumps({"failures": failures, "results": results}, indent=2, sort_keys=True))
    return 0 if not failures and len(results) == len(workers) else 1


def _smoke_targets(workflow: WorkflowConfig) -> tuple[str, ...]:
    candidates = (workflow.workspace / "README.md", workflow.path)
    targets: list[str] = []
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            rendered = candidate.relative_to(workflow.workspace).as_posix()
        except ValueError:
            rendered = str(candidate)
        targets.append(rendered)
    if not targets:
        raise ValueError("smoke_target_not_found")
    return tuple(targets)


def _catalog_text(profiles: tuple[HarnessProfile, ...]) -> str:
    lines = ["Available harnesses:"]
    for profile in profiles:
        lines.extend(
            [
                f"- {profile.harness.value} ({profile.display_name})",
                f"  summary: {profile.summary}",
                f"  strengths: {', '.join(profile.strengths)}",
                f"  best_for: {', '.join(profile.best_for)}",
                f"  avoid_for: {', '.join(profile.avoid_for)}",
                f"  traits: {', '.join(profile.traits)}",
                f"  profile_ref: harness:{profile.harness.value}",
            ]
        )
    return "\n".join(lines)


def _add_selection_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--controller-harness",
        choices=["auto", *(item.value for item in Harness)],
        default=None,
        help="Override the planner/router harness; auto uses local deterministic selection.",
    )
    parser.add_argument(
        "--worker-harness",
        action="append",
        choices=[item.value for item in Harness],
        help="Limit dispatch to this worker harness; repeat to form a candidate pool.",
    )


def _coordinator_from_args(
    config: WorkflowConfig,
    args: argparse.Namespace,
) -> Coordinator:
    return Coordinator(
        config,
        **_selection_kwargs(args),
    )


def _selection_kwargs(args: argparse.Namespace) -> dict[str, object]:
    controller_value = getattr(args, "controller_harness", None)
    controller = (
        None
        if controller_value in {None, "auto"}
        else Harness(controller_value)
    )
    worker_values = getattr(args, "worker_harness", None)
    workers = (
        None
        if worker_values is None
        else tuple(Harness(value) for value in worker_values)
    )
    return {
        "controller_harness": controller,
        "controller_auto": controller_value == "auto",
        "worker_harnesses": workers,
    }
