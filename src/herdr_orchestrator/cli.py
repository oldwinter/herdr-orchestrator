from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from herdr_orchestrator.config import ConfigError, load_workflow
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

    enqueue = subparsers.add_parser("enqueue")
    enqueue.add_argument("--workflow", required=True)
    enqueue.add_argument("--harness", required=True, choices=[item.value for item in Harness])
    enqueue.add_argument("--title", required=True)
    enqueue.add_argument("--prompt-file", required=True)
    enqueue.add_argument("--dedupe-key", required=True)
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
                job_id, created = Coordinator(config).enqueue_prompt_file(
                    harness=Harness(args.harness),
                    title=args.title,
                    prompt_file=prompt_file,
                    dedupe_key=args.dedupe_key,
                )
                print(json.dumps({"created": created, "job_id": job_id}, sort_keys=True))
                return 0
            case "run":
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
    except (ConfigError, ValueError) as exc:
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
