"""Explicit bridge to Orca-owned Run/Dispatch state, never a second scheduler."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from herdr_orchestrator.model import WorkflowConfig
from herdr_orchestrator.protocol import CommandRunner, subprocess_runner

READ_COMMANDS = frozenset(
    {
        "run-current",
        "run-list",
        "run-show",
        "task-list",
        "worker-show",
        "worker-read",
        "worker-list",
        "dispatch-show",
        "request-show",
        "gate-list",
        "inbox",
    }
)
WRITE_COMMANDS = frozenset(
    {
        "run-create",
        "run-use",
        "task-create",
        "task-update",
        "worker-start",
        "worker-retain",
        "worker-release",
        "send",
        "reply",
        "ask",
        "gate-create",
        "gate-resolve",
    }
)


def executable(environment: Mapping[str, str]) -> str:
    configured = environment.get("ORCA_CLI_COMMAND")
    if configured:
        return configured
    if environment.get("ORCA_DEV_REPO_ROOT"):
        return "orca-dev"
    # Never invoke the unrelated GNOME screen reader outside an Orca terminal.
    if sys.platform.startswith("linux") and not environment.get("ORCA_TERMINAL_ID"):
        return "orca-ide"
    return "orca"


class OrcaBridge:
    def __init__(
        self,
        workspace: Path,
        *,
        runner: CommandRunner = subprocess_runner,
        environment: Mapping[str, str] | None = None,
    ) -> None:
        self.workspace = workspace
        self.runner = runner
        self.executable = executable(os.environ if environment is None else environment)

    def invoke(
        self,
        arguments: list[str],
        *,
        apply: bool = False,
        timeout_seconds: int = 60,
    ) -> tuple[int, object]:
        if not arguments or arguments[0] not in READ_COMMANDS | WRITE_COMMANDS | {"check"}:
            raise ValueError("orca_command_not_supported")
        if not 1 <= timeout_seconds <= 3600:
            raise ValueError("orca_timeout_out_of_range")
        operation = arguments[0]
        mutates = (
            operation in WRITE_COMMANDS
            or (operation == "check" and not ({"--peek", "--all"} & set(arguments)))
            or any(arg == "--ack" or arg.startswith("--ack=") for arg in arguments)
        )
        if mutates and not apply:
            raise ValueError("orca_apply_required: native mutation requires explicit --apply")
        if any(arg.split("=", 1)[0] in {"--pairing-code", "--environment"} for arg in arguments):
            raise ValueError("orca_authority_override_forbidden")
        argv = [self.executable, "orchestration", *arguments]
        argv.append("--json")
        try:
            process = self.runner(argv, cwd=str(self.workspace), timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            return 2, {
                "error": "orca_outcome_unknown" if mutates else "orca_timeout",
                "retry_automatically": False,
                "instruction": "Inspect native request/dispatch state; do not relaunch.",
            }
        except OSError:
            return 2, {"error": "orca_unavailable", "retry_automatically": False}
        return _receipt(process, mutates=mutates)


def _receipt(process: subprocess.CompletedProcess[str], *, mutates: bool) -> tuple[int, object]:
    output = process.stdout or process.stderr
    if len(output) > 1024 * 1024:
        return 2, {"error": "orca_response_too_large", "outcome_unknown": mutates}
    try:
        payload = json.loads(output)
    except (ValueError, RecursionError):
        return 2, {
            "error": "orca_invalid_response",
            "outcome_unknown": mutates,
            "exit_code": process.returncode,
        }
    if not isinstance(payload, (dict, list)):
        return 2, {"error": "orca_invalid_response", "outcome_unknown": mutates}
    # Keep native failedStage, residualResources, Delivery and recovery commands intact.
    return process.returncode, payload


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser(
        "orca", help="Forward explicitly to Orca's native control plane."
    )
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=60)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)


def command(config: WorkflowConfig, args: argparse.Namespace) -> int:
    status, payload = OrcaBridge(config.workspace).invoke(
        args.arguments,
        apply=args.apply,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return status
