from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
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
from herdr_orchestrator.delivery import (
    DeliveryError,
    DeliveryEscalation,
    StandardizedDelivery,
)
from herdr_orchestrator.delivery_protocol import DeliveryArtifactError
from herdr_orchestrator.git_workspace import GitWorkspaceError
from herdr_orchestrator.herdr import HerdrTransport, smoke_agent_name
from herdr_orchestrator.model import (
    AgentState,
    Harness,
    HarnessProfile,
    TrackerBackend,
    WayfinderMode,
    WorkflowConfig,
)
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.store import Store
from herdr_orchestrator.tracker import TrackerError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Durable multi-harness orchestration over Herdr.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "seed", "status"):
        command = subparsers.add_parser(name)
        command.add_argument("--workflow", required=True)

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

    run = subparsers.add_parser("run")
    run.add_argument("--workflow", required=True)
    run.add_argument("--once", action="store_true")
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
                return doctor(config)
            case "seed":
                added, existing = Coordinator(config).seed()
                print(json.dumps({"added": added, "existing": existing}, sort_keys=True))
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
        TrackerError,
        ValueError,
    ) as exc:
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
    git_path = shutil.which("git")
    checks.append({"check": "git", "ok": git_path is not None, "value": git_path})
    if workflow.standardized_delivery.tracker_backend is TrackerBackend.GITHUB:
        github_path = shutil.which("gh")
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
    for harness in harnesses:
        executable = shutil.which(harness.value)
        checks.append(
            {
                "check": f"harness:{harness.value}",
                "ok": executable is not None,
                "value": executable,
            }
        )
        profile = profile_for_harness(workflow.profiles, harness)
        checks.append(
            {
                "check": f"profile:{harness.value}",
                "ok": profile.context_file.is_file(),
                "value": str(profile.context_file),
            }
        )
    ok = all(bool(check["ok"]) for check in checks)
    print(json.dumps({"checks": checks, "ok": ok}, indent=2, sort_keys=True))
    return 0 if ok else 1


def smoke(
    workflow: WorkflowConfig,
    *,
    selected_harnesses: list[str] | None = None,
) -> int:
    transport = HerdrTransport(workflow.name, workflow.workspace)
    failures: list[dict[str, str]] = []
    results: list[dict[str, str]] = []
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
            prompt = (
                "这是只读连通性测试。必须使用本地只读工具检查 pyproject.toml 和 "
                "workflows/multi-harness.toml；不得修改或创建文件，不得联网，不得执行任何"
                "外部动作。完成后简短回复 project.name、schema_version 和 workers 数量。"
            )
            name = smoke_agent_name(workflow.name, harness)
            outcome = transport.dispatch(
                harness,
                prompt,
                timeout_seconds=workflow.coordinator.agent_timeout_seconds,
                agent_name=name,
            )
            if not outcome.member_reused:
                created_names.append(name)
            if outcome.error_code is not None or outcome.state not in {
                AgentState.IDLE,
                AgentState.DONE,
            }:
                failures.append(
                    {
                        "harness": harness.value,
                        "error": outcome.error_code or outcome.state.value,
                    }
                )
                continue
            results.append({"harness": harness.value, "state": outcome.state.value})
    finally:
        for name in reversed(created_names):
            try:
                transport.close_created_agent(name)
            except Exception as exc:
                failures.append({"harness": name, "error": f"cleanup:{type(exc).__name__}"})
    print(json.dumps({"failures": failures, "results": results}, indent=2, sort_keys=True))
    return 0 if not failures and len(results) == len(workers) else 1


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
