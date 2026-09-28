#!/usr/bin/env python3
"""Local software-factory lane over the durable herdr-orchestrator queue.

The factory reuses the canonical durable queue (jobs, attempts, fencing,
dedupe contracts, retries, receipts) and the existing Coordinator claim and
outcome-commit path. The only difference from a Herdr dispatch is the
transport: ``LocalDispatcher`` executes the declared acceptance checks of a
backlog item locally in the workflow workspace instead of prompting an agent.

Backlog items live in ``factory/backlog.toml`` (committed, operator authored)
and their task contracts in ``factory/prompts/``. Runtime state stays under
``.orchestrator/factory/`` and never enters Git.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
import tomllib
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from herdr_orchestrator.completion import (
    CompletionPolicy,
    CompletionResult,
    FileReceiptSnapshot,
    ReceiptKind,
    TaskReceipt,
    failed_completion,
    receipt_file_path,
    snapshot_file_receipt,
    verify_completion,
)
from herdr_orchestrator.config import ConfigError, load_workflow
from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    DispatchContext,
    DispatchOutcome,
    Harness,
    JobState,
    PlacementTarget,
)
from herdr_orchestrator.protocol import TransportError
from herdr_orchestrator.runner import Coordinator
from herdr_orchestrator.store import Store, StoreError

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WORKFLOW = REPO_ROOT / "workflows" / "devin-factory.toml"
DEFAULT_BACKLOG = REPO_ROOT / "factory" / "backlog.toml"

DEDUPE_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
ITEM_KEYS = frozenset(
    {
        "dedupe_key",
        "title",
        "harness",
        "prompt_file",
        "receipt",
        "checks",
        "check_timeout_seconds",
        "requires",
    }
)
CHECK_KEYS = frozenset({"argv", "timeout_seconds"})
MAX_CHECK_OUTPUT_CHARS = 4000
EVIDENCE_KEEP_PER_ITEM = 25


class FactoryError(ValueError):
    """Stable machine-readable failure for factory intake and execution."""


@dataclass(frozen=True, slots=True)
class FactoryCheck:
    argv: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True, slots=True)
class FactoryItem:
    dedupe_key: str
    title: str
    harness: Harness
    prompt_file: Path
    receipt: str
    checks: tuple[FactoryCheck, ...]
    requires: tuple[str, ...]


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise FactoryError(code)


def load_backlog(path: Path) -> dict[str, FactoryItem]:
    """Parse and validate the operator-authored work item index."""
    path = path.expanduser().resolve()
    if not path.is_file():
        raise FactoryError(f"factory_backlog_not_found: {path}")
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise FactoryError(f"factory_backlog_invalid: {exc}") from exc
    _require(isinstance(raw, dict), "factory_backlog_invalid")
    _require(raw.get("schema_version") == 1, "factory_backlog_schema_version")
    rows = raw.get("items")
    _require(isinstance(rows, list), "factory_backlog_items_missing")
    items: dict[str, FactoryItem] = {}
    receipt_paths: set[str] = set()
    for index, row in enumerate(rows):
        _require(isinstance(row, dict), f"factory_item_invalid: row {index}")
        unknown = set(row) - ITEM_KEYS
        _require(not unknown, f"factory_item_unknown_keys: {sorted(unknown)}")
        dedupe_key = row.get("dedupe_key")
        _require(
            isinstance(dedupe_key, str) and DEDUPE_KEY.fullmatch(dedupe_key) is not None,
            "factory_dedupe_key_invalid",
        )
        _require(dedupe_key not in items, f"factory_dedupe_key_duplicate: {dedupe_key}")
        title = row.get("title")
        _require(
            isinstance(title, str) and title.strip() != "" and len(title) <= 200,
            "factory_title_invalid",
        )
        try:
            harness = Harness(str(row.get("harness")))
        except ValueError as exc:
            raise FactoryError(f"factory_harness_invalid: {row.get('harness')}") from exc
        prompt_value = row.get("prompt_file")
        _require(
            isinstance(prompt_value, str) and prompt_value != "",
            "factory_prompt_invalid",
        )
        prompt_file = (path.parent / prompt_value).resolve()
        _require(
            prompt_file.is_file() and prompt_file.is_relative_to(path.parent),
            f"factory_prompt_missing: {prompt_value}",
        )
        _require(
            prompt_file.read_text(encoding="utf-8").strip() != "",
            f"factory_prompt_empty: {prompt_value}",
        )
        receipt = _load_receipt(row.get("receipt"), dedupe_key)
        _require(receipt not in receipt_paths, f"factory_receipt_duplicate: {receipt}")
        receipt_paths.add(receipt)
        requires = _load_requires(row.get("requires"), dedupe_key)
        items[dedupe_key] = FactoryItem(
            dedupe_key=dedupe_key,
            title=title.strip(),
            harness=harness,
            prompt_file=prompt_file,
            receipt=receipt,
            checks=_load_checks(row, dedupe_key),
            requires=requires,
        )
    _check_requires(items)
    return items


def _load_requires(value: object, dedupe_key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    _require(
        isinstance(value, list)
        and all(
            isinstance(entry, str) and DEDUPE_KEY.fullmatch(entry) is not None for entry in value
        ),
        f"factory_requires_invalid: {dedupe_key}",
    )
    _require(
        len(set(value)) == len(value),
        f"factory_requires_duplicate: {dedupe_key}",
    )
    return tuple(value)


def _check_requires(items: dict[str, FactoryItem]) -> None:
    for item in items.values():
        for required in item.requires:
            _require(
                required in items,
                f"factory_requires_unknown: {item.dedupe_key} -> {required}",
            )
            _require(
                required != item.dedupe_key,
                f"factory_requires_cycle: {item.dedupe_key}",
            )
    visiting: set[str] = set()
    done: set[str] = set()

    def walk(key: str, trail: tuple[str, ...]) -> None:
        if key in done:
            return
        _require(
            key not in visiting,
            f"factory_requires_cycle: {' -> '.join((*trail, key))}",
        )
        visiting.add(key)
        for required in items[key].requires:
            walk(required, (*trail, key))
        visiting.discard(key)
        done.add(key)

    for key in items:
        walk(key, ())


def _load_receipt(value: object, dedupe_key: str) -> str:
    if value is None:
        value = f".orchestrator/factory/receipts/{dedupe_key}.json"
    _require(isinstance(value, str) and value != "", "factory_receipt_invalid")
    relative = Path(value)
    _require(
        not relative.is_absolute()
        and bool(relative.parts)
        and ".." not in relative.parts
        and ".orchestrator" in relative.parts,
        "factory_receipt_invalid",
    )
    return relative.as_posix()


def _load_checks(row: dict[str, Any], dedupe_key: str) -> tuple[FactoryCheck, ...]:
    raw_checks = row.get("checks")
    _require(
        isinstance(raw_checks, list) and 1 <= len(raw_checks) <= 16,
        f"factory_checks_missing: {dedupe_key}",
    )
    default_timeout = row.get("check_timeout_seconds", 600)
    _require(
        isinstance(default_timeout, int) and 1 <= default_timeout <= 3600,
        "factory_check_timeout_invalid",
    )
    checks: list[FactoryCheck] = []
    for index, entry in enumerate(raw_checks):
        _require(isinstance(entry, dict), f"factory_check_invalid: {dedupe_key}[{index}]")
        unknown = set(entry) - CHECK_KEYS
        _require(not unknown, f"factory_check_unknown_keys: {sorted(unknown)}")
        argv = entry.get("argv")
        _require(
            isinstance(argv, list)
            and 1 <= len(argv) <= 16
            and all(isinstance(arg, str) and 0 < len(arg) <= 512 for arg in argv),
            f"factory_check_argv_invalid: {dedupe_key}[{index}]",
        )
        executable = Path(argv[0])
        _require(
            ".." not in executable.parts and (not executable.is_absolute() or executable.is_file()),
            f"factory_check_executable_invalid: {argv[0]}",
        )
        timeout = entry.get("timeout_seconds", default_timeout)
        _require(
            isinstance(timeout, int) and 1 <= timeout <= 3600,
            "factory_check_timeout_invalid",
        )
        checks.append(FactoryCheck(tuple(argv), timeout))
    return tuple(checks)


def _bounded_tail(text: str | None) -> str:
    return text[-MAX_CHECK_OUTPUT_CHARS:] if text else ""


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _git_head(workspace: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


class LocalDispatcher:
    """Dispatcher protocol implementation that runs declared checks locally.

    It mirrors the Herdr transport contract: attempt phases are persisted in
    order, the file receipt is snapshotted before execution and verified with
    the shared ``verify_completion`` after checks pass, and failures return a
    ``verification-failed`` completion so the queue records no success.
    """

    def __init__(
        self,
        *,
        workspace: Path,
        items: dict[str, FactoryItem],
        evidence_root: Path,
        environment: dict[str, str] | None = None,
    ) -> None:
        self._workspace = workspace
        self._items = items
        self._evidence_root = evidence_root
        self._environment = environment if environment is not None else os.environ

    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome:
        del harness
        name = agent_name or "local-executor"
        task_key = context.task_key if context is not None else ""
        item = self._items.get(task_key)
        evidence: dict[str, Any] = {
            "correlation_id": context.correlation_id if context is not None else "",
            "dedupe_key": task_key,
            "git_head": _git_head(self._workspace),
            "started_at": _utc_now(),
            "title": item.title if item is not None else None,
        }
        deadline = time.monotonic() + max(1.0, timeout_seconds)
        self._progress(context, AttemptPhase.RUNTIME_ACQUIRED, AgentState.WORKING)
        file_before = (
            snapshot_file_receipt(context.receipt, self._workspace) if context is not None else None
        )
        if item is None:
            return self._failure(
                context,
                name,
                evidence,
                error_code="factory_item_unknown",
                error_summary=f"no backlog item for dedupe_key {task_key}",
                settled=False,
            )
        self._progress(context, AttemptPhase.PROMPT_ACCEPTED, AgentState.WORKING)
        results = []
        for check in item.checks:
            results.append(self._run_check(check, deadline))
            if results[-1]["exit_code"] != 0:
                break
        evidence["checks"] = results
        evidence["checks_skipped"] = len(item.checks) - len(results)
        evidence["finished_at"] = _utc_now()
        self._progress(context, AttemptPhase.SETTLED, AgentState.DONE)
        failed = next((result for result in results if result["exit_code"] != 0), None)
        if failed is not None:
            return self._failure(
                context,
                name,
                evidence,
                error_code=(
                    "factory_check_timeout" if failed.get("timed_out") else "factory_check_failed"
                ),
                error_summary=f"check {' '.join(failed['argv'])} " f"exit={failed['exit_code']}",
                settled=True,
            )
        completion = self._write_receipt_and_verify(
            context,
            prompt,
            file_before,
            evidence,
            item,
        )
        task_verified = completion.task_verified
        self._progress(
            context,
            AttemptPhase.RECEIPT_OBSERVED,
            AgentState.DONE,
            task_verified=task_verified,
            completion=completion,
        )
        self._write_evidence(context, evidence, verified=task_verified is True)
        return DispatchOutcome(
            agent_name=name,
            state=AgentState.DONE,
            member_reused=False,
            pane_id=None,
            placement=context.placement if context is not None else None,
            execution_path=str(self._workspace),
            agent_settled=True,
            task_verified=task_verified,
            completion=completion,
            correlation_id=context.correlation_id if context is not None else "",
        )

    def _failure(
        self,
        context: DispatchContext | None,
        agent_name: str,
        evidence: dict[str, Any],
        *,
        error_code: str,
        error_summary: str,
        settled: bool,
    ) -> DispatchOutcome:
        evidence["error_code"] = error_code
        evidence["error_summary"] = error_summary[:500]
        evidence.setdefault("finished_at", _utc_now())
        policy = self._policy(context)
        completion = failed_completion(policy, error_code)
        if settled:
            self._progress(
                context,
                AttemptPhase.RECEIPT_OBSERVED,
                AgentState.DONE,
                task_verified=False,
                completion=completion,
            )
        self._write_evidence(context, evidence, verified=False)
        return DispatchOutcome(
            agent_name=agent_name,
            state=AgentState.DONE,
            member_reused=False,
            pane_id=None,
            placement=context.placement if context is not None else None,
            execution_path=str(self._workspace),
            error_code=error_code,
            error_summary=error_summary,
            agent_settled=True,
            task_verified=False,
            completion=completion,
            correlation_id=context.correlation_id if context is not None else "",
        )

    @staticmethod
    def _policy(context: DispatchContext | None) -> CompletionPolicy:
        if context is not None and context.receipt is not None:
            return CompletionPolicy.RECEIPT_V1
        return CompletionPolicy.LEGACY_UNVERIFIED

    def _write_receipt_and_verify(
        self,
        context: DispatchContext | None,
        prompt: str,
        file_before: FileReceiptSnapshot | None,
        evidence: dict[str, Any],
        item: FactoryItem,
    ) -> CompletionResult:
        receipt = context.receipt if context is not None else None
        try:
            if receipt is not None and receipt.kind is ReceiptKind.FILE:
                target = receipt_file_path(receipt, self._workspace)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(
                    json.dumps(
                        {
                            "checks": [
                                {
                                    "argv": result["argv"],
                                    "exit_code": result["exit_code"],
                                }
                                for result in evidence.get("checks", [])
                            ],
                            "correlation_id": evidence["correlation_id"],
                            "dedupe_key": item.dedupe_key,
                            "git_head": evidence["git_head"],
                            "schema_version": 1,
                            "task": item.title,
                            "verified_at": _utc_now(),
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
            return verify_completion(
                receipt,
                None,
                self._workspace,
                prompt=prompt,
                output_before=None,
                file_before=file_before,
                read_output=lambda: "",
            )
        except TransportError as exc:
            return failed_completion(self._policy(context), exc.code)

    def _run_check(self, check: FactoryCheck, deadline: float) -> dict[str, Any]:
        remaining = max(1.0, min(check.timeout_seconds, deadline - time.monotonic()))
        result: dict[str, Any] = {
            "argv": list(check.argv),
            "timeout_seconds": check.timeout_seconds,
        }
        executable = shutil.which(check.argv[0])
        if executable is None:
            result.update(
                {
                    "exit_code": 127,
                    "error": "executable_not_found",
                    "stderr_tail": "",
                    "stdout_tail": "",
                }
            )
            return result
        env = dict(self._environment)
        source = str(REPO_ROOT / "src")
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = source if not existing else source + os.pathsep + existing
        started = time.monotonic()
        try:
            completed = subprocess.run(
                [executable, *check.argv[1:]],
                cwd=self._workspace,
                env=env,
                capture_output=True,
                text=True,
                timeout=remaining,
                check=False,
            )
        except subprocess.TimeoutExpired:
            result.update(
                {
                    "duration_ms": int((time.monotonic() - started) * 1000),
                    "exit_code": 124,
                    "stderr_tail": "",
                    "stdout_tail": "",
                    "timed_out": True,
                }
            )
            return result
        except OSError as exc:
            result.update(
                {
                    "error": str(exc)[:300],
                    "exit_code": 126,
                    "stderr_tail": "",
                    "stdout_tail": "",
                }
            )
            return result
        result.update(
            {
                "duration_ms": int((time.monotonic() - started) * 1000),
                "exit_code": completed.returncode,
                "stderr_tail": _bounded_tail(completed.stderr),
                "stdout_tail": _bounded_tail(completed.stdout),
            }
        )
        return result

    def _progress(
        self,
        context: DispatchContext | None,
        phase: AttemptPhase,
        agent_state: AgentState,
        *,
        task_verified: bool | None = None,
        completion: CompletionResult | None = None,
    ) -> None:
        if context is None or context.attempt_progress is None:
            return
        context.attempt_progress(
            AttemptProgress(
                phase,
                "local-executor",
                execution_path=str(self._workspace),
                agent_state=agent_state,
                member_reused=False,
                agent_settled=agent_state in {AgentState.IDLE, AgentState.DONE},
                task_verified=task_verified,
                completion=completion,
            )
        )

    def _write_evidence(
        self,
        context: DispatchContext | None,
        evidence: dict[str, Any],
        *,
        verified: bool,
    ) -> None:
        evidence["verified"] = verified
        directory = self._evidence_root / str(evidence.get("dedupe_key") or "unknown")
        directory.mkdir(parents=True, exist_ok=True)
        stamp = _utc_now().replace(":", "-").replace("+", "Z")
        correlation = str(evidence.get("correlation_id") or "no-correlation")[:8]
        target = directory / f"{stamp}-{correlation}.json"
        target.write_text(
            json.dumps(evidence, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        stale = sorted(directory.glob("*.json"))[:-EVIDENCE_KEEP_PER_ITEM]
        for old in stale:
            old.unlink(missing_ok=True)


def _build_coordinator(
    workflow: Path,
    backlog: Path,
) -> tuple[Coordinator, dict[str, FactoryItem]]:
    config = load_workflow(workflow)
    items = load_backlog(backlog)
    worker_harnesses = {worker.harness for worker in config.workers}
    for item in items.values():
        if item.harness not in worker_harnesses:
            raise FactoryError(
                f"factory_harness_has_no_worker: {item.harness.value} ({item.dedupe_key})"
            )
    dispatcher = LocalDispatcher(
        workspace=config.workspace,
        items=items,
        evidence_root=config.workspace / ".orchestrator" / "factory" / "evidence",
    )
    return Coordinator(config, dispatcher=dispatcher), items


def _command_validate(args: argparse.Namespace) -> int:
    """Dry-run the backlog: validate and summarize without queue writes."""
    config = load_workflow(args.workflow)
    items = load_backlog(args.backlog)
    worker_harnesses = {worker.harness for worker in config.workers}
    queued: list[str] = []
    state_db_error: str | None = None
    state_db = Path(config.state_db)
    if state_db.is_file():
        try:
            with closing(
                sqlite3.connect(
                    f"file:{state_db}?mode=ro&immutable=1",
                    uri=True,
                )
            ) as connection:
                queued = [
                    str(row[0])
                    for row in connection.execute(
                        "SELECT dedupe_key FROM jobs WHERE workflow = ?",
                        (config.name,),
                    )
                ]
        except sqlite3.DatabaseError as exc:
            state_db_error = str(exc)[:200]
    print(
        json.dumps(
            {
                "items": [
                    {
                        "checks": [list(check.argv) for check in item.checks],
                        "dedupe_key": item.dedupe_key,
                        "harness": item.harness.value,
                        "harness_supported": item.harness in worker_harnesses,
                        "prompt_file": item.prompt_file.name,
                        "queued": item.dedupe_key in queued,
                        "receipt": item.receipt,
                        "requires": list(item.requires),
                        "title": item.title,
                    }
                    for item in items.values()
                ],
                "state_db": state_db.is_file(),
                "state_db_error": state_db_error,
                "valid": True,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _command_intake(args: argparse.Namespace) -> int:
    coordinator, items = _build_coordinator(args.workflow, args.backlog)
    coordinator.initialize()
    workspace = str(coordinator.config.workspace.resolve())
    state_by_key = {
        str(job["dedupe_key"]): str(job["state"])
        for job in coordinator.store.jobs(
            coordinator.config.name, workspace=workspace, include_legacy=True
        )
    }
    added = 0
    jobs: list[dict[str, object]] = []
    waiting: list[dict[str, object]] = []
    for item in items.values():
        blockers = [
            required
            for required in item.requires
            if state_by_key.get(required) != JobState.SUCCEEDED.value
        ]
        if blockers:
            waiting.append({"dedupe_key": item.dedupe_key, "requires": blockers})
            continue
        job_id, created, selected = coordinator.enqueue_prompt_file(
            harness=item.harness,
            title=item.title,
            prompt_file=item.prompt_file,
            dedupe_key=item.dedupe_key,
            placement=PlacementTarget.PANE,
            receipt=TaskReceipt(ReceiptKind.FILE, item.receipt),
        )
        added += int(created)
        jobs.append(
            {
                "created": created,
                "dedupe_key": item.dedupe_key,
                "harness": selected.value,
                "job_id": job_id,
            }
        )
    print(
        json.dumps(
            {
                "added": added,
                "existing": len(jobs) - added,
                "jobs": jobs,
                "waiting": waiting,
            },
            sort_keys=True,
        )
    )
    return 0


def _command_run(args: argparse.Namespace) -> int:
    coordinator, _ = _build_coordinator(args.workflow, args.backlog)
    if args.once:
        report = coordinator.run_once()
        print(json.dumps(report, sort_keys=True))
        return 1 if report.get("failed") or report.get("blocked") else 0
    result = coordinator.run_until_idle(timeout_seconds=args.drain_timeout_seconds)
    print(json.dumps(result, sort_keys=True))
    queue = result.get("queue", {})
    terminal = int(queue.get("failed", 0)) + int(queue.get("blocked", 0))
    return 0 if result.get("idle") and terminal == 0 else 1


def _command_status(args: argparse.Namespace) -> int:
    config = load_workflow(args.workflow)
    store = Store(config.state_db)
    store.initialize()
    workspace = str(config.workspace.resolve())
    jobs = store.jobs(config.name, workspace=workspace, include_legacy=True)
    try:
        items = load_backlog(args.backlog)
        queued = {str(job["dedupe_key"]) for job in jobs}
        backlog: dict[str, object] = {
            "items": len(items),
            "requires": {
                item.dedupe_key: list(item.requires) for item in items.values() if item.requires
            },
            "unqueued": sorted(key for key in items if key not in queued),
        }
    except FactoryError as exc:
        backlog = {"error": str(exc), "items": 0, "unqueued": []}
    print(
        json.dumps(
            {
                "backlog": backlog,
                "counts": store.status_counts(
                    config.name,
                    workspace=workspace,
                    include_legacy=True,
                ),
                "jobs": [
                    {
                        "attempt_phase": job["attempt_phase"],
                        "attempts": job["attempts"],
                        "dedupe_key": job["dedupe_key"],
                        "error_code": job["error_code"],
                        "error_summary": job["error_summary"],
                        "id": job["id"],
                        "max_attempts": job["max_attempts"],
                        "receipt_kind": job["receipt_kind"],
                        "receipt_value": job["receipt_value"],
                        "state": job["state"],
                        "task_verified": job["task_verified"],
                        "title": job["title"],
                        "verification_class": job["verification_class"],
                    }
                    for job in jobs
                ],
                "workflow": config.name,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def _command_report(args: argparse.Namespace) -> int:
    config = load_workflow(args.workflow)
    store = Store(config.state_db)
    store.initialize()
    workspace = str(config.workspace.resolve())
    jobs = store.jobs(config.name, workspace=workspace, include_legacy=True)
    counts = store.status_counts(config.name, workspace=workspace, include_legacy=True)
    head = _git_head(config.workspace) or "unknown"
    lines = [
        "# Devin factory report",
        "",
        f"- workflow: `{config.name}`",
        f"- generated_at: {_utc_now()}",
        f"- git_head: `{head}`",
        f"- counts: {json.dumps(counts, sort_keys=True)}",
        "",
        "| job | work item | state | attempts | verified | error | evidence |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    evidence_root = config.workspace / ".orchestrator" / "factory" / "evidence"
    for job in jobs:
        dedupe = str(job["dedupe_key"] or "")
        evidence_dir = evidence_root / dedupe
        evidence_note = "-"
        if evidence_dir.is_dir():
            latest = sorted(evidence_dir.glob("*.json"))
            if latest:
                evidence_note = latest[-1].relative_to(config.workspace).as_posix()
        lines.append(
            "| {id} | {key} | {state} | {attempts}/{max_attempts} | {verified} "
            "| {error} | {evidence} |".format(
                id=job["id"],
                key=dedupe or "-",
                state=job["state"],
                attempts=job["attempts"],
                max_attempts=job["max_attempts"],
                verified=job["task_verified"],
                error=job["error_code"] or "-",
                evidence=evidence_note,
            )
        )
    report_root = config.workspace / ".orchestrator" / "factory"
    report_root.mkdir(parents=True, exist_ok=True)
    target = report_root / "report.md"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "jobs": len(jobs),
                "report": target.relative_to(config.workspace).as_posix(),
            },
            sort_keys=True,
        )
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Local software-factory lane over the durable queue."
    )
    parser.add_argument(
        "--workflow",
        type=Path,
        default=DEFAULT_WORKFLOW,
        help="Factory workflow TOML (default: workflows/devin-factory.toml).",
    )
    parser.add_argument(
        "--backlog",
        type=Path,
        default=DEFAULT_BACKLOG,
        help="Work item index TOML (default: factory/backlog.toml).",
    )
    subparsers = parser.add_subparsers(dest="factory_command", required=True)
    subparsers.add_parser("intake", help="Enqueue backlog items (idempotent).")
    run = subparsers.add_parser("run", help="Claim items and run declared checks.")
    run.add_argument("--once", action="store_true", help="Run a single wave.")
    run.add_argument(
        "--drain-timeout-seconds",
        type=int,
        default=3600,
        help="Drain deadline for the default until-idle run.",
    )
    subparsers.add_parser("status", help="Show queue counts, jobs and backlog.")
    subparsers.add_parser("report", help="Write .orchestrator/factory/report.md.")
    subparsers.add_parser(
        "validate",
        help="Dry-run: parse and summarize the backlog without queue writes.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "intake": _command_intake,
        "run": _command_run,
        "status": _command_status,
        "report": _command_report,
        "validate": _command_validate,
    }
    try:
        return handlers[args.factory_command](args)
    except (ConfigError, FactoryError, StoreError, TransportError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
