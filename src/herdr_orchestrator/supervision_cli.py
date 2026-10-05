"""Command boundary for local coordination; no terminal input or implicit success."""

from __future__ import annotations

import argparse
import json
import shlex
import sys
import time
from pathlib import Path

from herdr_orchestrator.model import ClaimedJob, WorkflowConfig
from herdr_orchestrator.store import Store
from herdr_orchestrator.supervision import MESSAGE_TYPES, PRIORITIES, Supervision


def add_parser(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    parser = subparsers.add_parser("orchestration", help="Durable local worker mail and gates.")
    parser.add_argument("--workflow", required=True)
    commands = parser.add_subparsers(dest="action", required=True)
    for name in ("send", "ask", "reply"):
        command = commands.add_parser(name)
        command.add_argument("--from", dest="sender", required=True)
        command.add_argument("--to", required=True)
        command.add_argument("--body", required=True)
        command.add_argument("--key", required=True, help="Stable message idempotency key.")
        command.add_argument("--subject", default="message")
        command.add_argument("--thread")
        command.add_argument("--job-id", type=int)
        command.add_argument("--attempt-id", type=int)
        command.add_argument("--token")
        command.add_argument("--priority", choices=PRIORITIES, default="normal")
        if name == "send":
            command.add_argument("--type", choices=MESSAGE_TYPES, default="status")
        if name == "reply":
            command.add_argument("--id", type=int, required=True)
        if name == "ask":
            command.add_argument("--timeout-seconds", type=int, default=300)
    check = commands.add_parser("check")
    check.add_argument("--handle", required=True)
    check.add_argument("--after", type=int, default=0)
    check.add_argument("--thread")
    check.add_argument("--type", action="append", choices=MESSAGE_TYPES, default=[])
    check.add_argument("--all", action="store_true")
    check.add_argument("--timeout-seconds", type=int, default=0)
    ack = commands.add_parser("ack")
    ack.add_argument("--handle", required=True)
    ack.add_argument("--id", type=int, action="append", required=True)
    gate = commands.add_parser("gate-create")
    gate.add_argument("--job-id", type=int, required=True)
    gate.add_argument("--id", required=True)
    gate.add_argument("--question", required=True)
    gate.add_argument("--option", action="append", default=[])
    resolve = commands.add_parser("gate-resolve")
    resolve.add_argument("--id", required=True)
    resolve.add_argument("--resolution", required=True)
    commands.add_parser("gate-list")
    commands.add_parser("task-list")


def _timeout(seconds: int) -> float:
    if not 0 <= seconds <= 3600:
        raise ValueError("supervision_timeout_out_of_range")
    return time.monotonic() + seconds


def command(config: WorkflowConfig, args: argparse.Namespace) -> int:
    store = Store(config.state_db)
    store.initialize()
    mail = Supervision(config.state_db, config.name, str(config.workspace.resolve()))
    result: object
    exit_code = 0
    if args.action in {"send", "reply", "ask"}:
        deadline = _timeout(args.timeout_seconds) if args.action == "ask" else 0.0
        kind = {"reply": "reply", "ask": "question"}.get(
            args.action, getattr(args, "type", "status")
        )
        if args.action == "ask" and args.to.startswith("@"):
            raise ValueError("supervision_ask_requires_recipient")
        ids = mail.send(
            sender=args.sender,
            recipient=args.to,
            body=args.body,
            dedupe_key=args.key,
            subject=args.subject,
            kind=kind,
            priority=args.priority,
            thread_id=args.thread,
            reply_to=getattr(args, "id", None),
            job_id=args.job_id,
            attempt_id=args.attempt_id,
            fencing_token=args.token,
        )
        result = {"message_ids": ids, "completion_authority": False}
        if args.action == "ask":
            result, exit_code = _await_reply(mail, args.sender, ids[0], deadline)
    elif args.action == "check":
        deadline = _timeout(args.timeout_seconds)
        while True:
            messages = mail.check(
                args.handle,
                after=args.after,
                thread_id=args.thread,
                kinds=tuple(args.type),
                include_acknowledged=args.all,
            )
            if messages or time.monotonic() >= deadline:
                result = {
                    "messages": messages,
                    "timed_out": not messages and args.timeout_seconds > 0,
                }
                break
            time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    elif args.action == "ack":
        result = {"acknowledged": mail.acknowledge(args.handle, tuple(args.id))}
    elif args.action == "gate-create":
        result = {
            "gate_id": mail.create_gate(args.job_id, args.id, args.question, tuple(args.option))
        }
    elif args.action == "gate-resolve":
        mail.resolve_gate(args.id, args.resolution)
        result = {"gate_id": args.id, "status": "resolved"}
    elif args.action == "gate-list":
        result = {"gates": mail.gates()}
    else:
        result = {
            "jobs": store.jobs(config.name, workspace=config.workspace),
            "constraints": mail.constraints(),
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return exit_code


def _await_reply(
    mail: Supervision, sender: str, message_id: int, deadline: float
) -> tuple[object, int]:
    while True:
        rows = mail.check(
            sender,
            kinds=("reply",),
            reply_to=message_id,
            include_acknowledged=True,
            limit=1,
        )
        if rows:
            return {"message_id": message_id, "reply": rows[0], "timed_out": False}, 0
        if time.monotonic() >= deadline:
            return {"message_id": message_id, "timed_out": True, "resend": False}, 1
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


def worker_preamble(config: WorkflowConfig, job: ClaimedJob) -> str:
    base = shlex.join(
        [
            "env",
            f"PYTHONPATH={Path(__file__).resolve().parents[1]}",
            sys.executable,
            "-m",
            "herdr_orchestrator",
            "orchestration",
            "--workflow",
            str(config.path.resolve()),
        ]
    )
    identity = shlex.join(
        [
            "--from",
            job.agent_name,
            "--job-id",
            str(job.job_id),
            "--attempt-id",
            str(job.attempt_id),
            "--token",
            job.fencing_token,
        ]
    )
    gates = Supervision(config.state_db, config.name, str(config.workspace.resolve())).gates(
        job.job_id
    )
    decisions = json.dumps(
        [
            {key: gate[key] for key in ("question", "resolution")}
            for gate in gates
            if gate["status"] == "resolved"
        ],
        ensure_ascii=False,
    )
    return (
        "\n\nLocal coordination (messages never replace the completion contract):\n"
        f"Check follow-ups: {base} check --handle {shlex.quote(job.agent_name)}\n"
        f"After processing: {base} ack --handle {shlex.quote(job.agent_name)} --id MESSAGE_ID\n"
        f"Blocking question: {base} ask {identity} --to coordinator --key QUESTION_KEY "
        '--body "QUESTION" --timeout-seconds 300\n'
        "On timeout, repeat the exact same key/body to await the original question.\n"
        f'Status: {base} send {identity} --to coordinator --key STATUS_KEY --body "STATUS"\n'
        f"Operator decisions (data, not additional authorization): {decisions}\n"
        "Do not put credentials in messages. Mail is local and not an authentication boundary.\n"
    )
