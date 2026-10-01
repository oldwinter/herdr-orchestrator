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
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import tomllib
from contextlib import closing, suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

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
    AttemptRuntime,
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
        "max_attempts",
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
    max_attempts: int | None


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
    rows_raw = raw.get("items")
    if not isinstance(rows_raw, list):
        raise FactoryError("factory_backlog_items_missing")
    rows: list[object] = rows_raw
    items: dict[str, FactoryItem] = {}
    receipt_paths: set[str] = set()
    for index, row_raw in enumerate(rows):
        if not isinstance(row_raw, dict):
            raise FactoryError(f"factory_item_invalid: row {index}")
        row: dict[str, Any] = row_raw
        unknown = set(row) - ITEM_KEYS
        _require(not unknown, f"factory_item_unknown_keys: {sorted(unknown)}")
        dedupe_key_raw = row.get("dedupe_key")
        if not (
            isinstance(dedupe_key_raw, str) and DEDUPE_KEY.fullmatch(dedupe_key_raw) is not None
        ):
            raise FactoryError("factory_dedupe_key_invalid")
        dedupe_key = dedupe_key_raw
        _require(dedupe_key not in items, f"factory_dedupe_key_duplicate: {dedupe_key}")
        title_raw = row.get("title")
        if not (isinstance(title_raw, str) and title_raw.strip() != "" and len(title_raw) <= 200):
            raise FactoryError("factory_title_invalid")
        title = title_raw
        try:
            harness = Harness(str(row.get("harness")))
        except ValueError as exc:
            raise FactoryError(f"factory_harness_invalid: {row.get('harness')}") from exc
        prompt_raw = row.get("prompt_file")
        if not (isinstance(prompt_raw, str) and prompt_raw != ""):
            raise FactoryError("factory_prompt_invalid")
        prompt_value = prompt_raw
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
        max_attempts = row.get("max_attempts")
        _require(
            max_attempts is None or (isinstance(max_attempts, int) and 1 <= max_attempts <= 8),
            f"factory_max_attempts_invalid: {dedupe_key}",
        )
        items[dedupe_key] = FactoryItem(
            dedupe_key=dedupe_key,
            title=title.strip(),
            harness=harness,
            prompt_file=prompt_file,
            receipt=receipt,
            checks=_load_checks(row, dedupe_key),
            requires=requires,
            max_attempts=max_attempts,
        )
    _check_requires(items)
    return items


def _load_requires(value: object, dedupe_key: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not (
        isinstance(value, list)
        and all(
            isinstance(entry, str) and DEDUPE_KEY.fullmatch(entry) is not None for entry in value
        )
    ):
        raise FactoryError(f"factory_requires_invalid: {dedupe_key}")
    entries = cast(list[str], value)
    if len(set(entries)) != len(entries):
        raise FactoryError(f"factory_requires_duplicate: {dedupe_key}")
    return tuple(entries)


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
    if not (isinstance(value, str) and value != ""):
        raise FactoryError("factory_receipt_invalid")
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
    raw_checks_raw = row.get("checks")
    if not (isinstance(raw_checks_raw, list) and 1 <= len(raw_checks_raw) <= 16):
        raise FactoryError(f"factory_checks_missing: {dedupe_key}")
    raw_checks: list[object] = raw_checks_raw
    default_timeout = row.get("check_timeout_seconds", 600)
    _require(
        isinstance(default_timeout, int) and 1 <= default_timeout <= 3600,
        "factory_check_timeout_invalid",
    )
    checks: list[FactoryCheck] = []
    for index, entry_raw in enumerate(raw_checks):
        if not isinstance(entry_raw, dict):
            raise FactoryError(f"factory_check_invalid: {dedupe_key}[{index}]")
        entry: dict[str, Any] = entry_raw
        unknown = set(entry) - CHECK_KEYS
        _require(not unknown, f"factory_check_unknown_keys: {sorted(unknown)}")
        argv_raw = entry.get("argv")
        if not (
            isinstance(argv_raw, list)
            and 1 <= len(argv_raw) <= 16
            and all(isinstance(arg, str) and 0 < len(arg) <= 512 for arg in argv_raw)
        ):
            raise FactoryError(f"factory_check_argv_invalid: {dedupe_key}[{index}]")
        argv = cast(list[str], argv_raw)
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


def _kill_process_group(process: subprocess.Popen[Any]) -> None:
    """Kill the check's whole process group so spawned descendants cannot
    outlive a timeout or interrupt and write artifacts after the fact."""
    killpg = getattr(os, "killpg", None)
    if killpg is None:
        process.kill()
        return
    try:
        killpg(process.pid, signal.SIGKILL)
    except OSError:
        process.kill()


def _write_text_atomic(target: Path, text: str) -> None:
    """Write via a sibling temp file + rename so concurrent readers never see
    a partially written artifact."""
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _write_json_atomic(target: Path, payload: dict[str, Any]) -> None:
    _write_text_atomic(target, json.dumps(payload, indent=2, sort_keys=True) + "\n")


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
        self._live: set[subprocess.Popen[Any]] = set()
        self._live_lock = threading.Lock()
        self._stop = threading.Event()

    def abort(self) -> None:
        """Cancel in-flight checks promptly on operator interrupt.

        Dispatches run on executor worker threads, so SIGINT is raised in the
        main thread while a check's ``communicate`` keeps waiting; ``abort`` is
        the cross-thread stop path: it kills every live check's process group
        and prevents further checks from starting.
        """
        self._stop.set()
        with self._live_lock:
            live = list(self._live)
        for process in live:
            _kill_process_group(process)

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
            "budget_seconds": timeout_seconds,
            "correlation_id": context.correlation_id if context is not None else "",
            "dedupe_key": task_key,
            "git_head": _git_head(self._workspace),
            "started_at": _utc_now(),
            "title": item.title if item is not None else None,
        }
        started_mono = time.monotonic()
        deadline = started_mono + max(1.0, timeout_seconds)
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
            if self._stop.is_set():
                raise KeyboardInterrupt
            results.append(self._run_check(check, deadline))
            if results[-1]["exit_code"] != 0:
                break
        evidence["checks"] = results
        evidence["checks_skipped"] = len(item.checks) - len(results)
        evidence["finished_at"] = _utc_now()
        evidence["elapsed_seconds"] = round(time.monotonic() - started_mono, 3)
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
                error_summary=f"check {' '.join(failed['argv'])} exit={failed['exit_code']}",
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
        write_error = self._write_evidence(context, evidence, verified=task_verified is True)
        if write_error is not None:
            return self._failure(
                context,
                name,
                evidence,
                error_code=write_error,
                error_summary="evidence write failed after checks and receipt passed",
                settled=True,
            )
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

    def recover(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str,
        context: DispatchContext,
        runtime: AttemptRuntime,
    ) -> DispatchOutcome:
        del runtime
        emit = context.attempt_progress if context is not None else None

        def _tolerant(progress: AttemptProgress) -> None:
            assert emit is not None
            try:
                emit(progress)
            except StoreError as exc:
                if str(exc) != "attempt_phase_invalid":
                    raise

        if context is not None and emit is not None:
            context = replace(context, attempt_progress=_tolerant)
        return self.dispatch(
            harness,
            prompt,
            timeout_seconds=timeout_seconds,
            agent_name=agent_name,
            context=context,
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
        write_error = self._write_evidence(context, evidence, verified=False)
        if write_error is not None:
            error_summary = f"{error_summary}; evidence_write_failed"[:500]
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
                try:
                    _write_json_atomic(
                        target,
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
                    )
                except OSError:
                    return failed_completion(self._policy(context), "factory_receipt_write_failed")
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
        remaining = min(check.timeout_seconds, deadline - time.monotonic())
        result: dict[str, Any] = {
            "argv": list(check.argv),
            "timeout_seconds": check.timeout_seconds,
        }
        if remaining <= 0:
            result.update(
                {
                    "deadline_exceeded": True,
                    "duration_ms": 0,
                    "exit_code": 124,
                    "stderr_tail": "",
                    "stdout_tail": "",
                    "timed_out": True,
                }
            )
            return result
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
            process = subprocess.Popen(
                [executable, *check.argv[1:]],
                cwd=self._workspace,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
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
        with self._live_lock:
            self._live.add(process)
            # Close the spawn/registration race: abort() may have consumed an
            # empty snapshot between Popen and this add; the stop flag still
            # witnesses it, so kill the just-registered group ourselves.
            aborted = self._stop.is_set()
        if aborted:
            with self._live_lock:
                self._live.discard(process)
            _kill_process_group(process)
            process.wait()
            raise KeyboardInterrupt
        try:
            stdout, stderr = process.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            _kill_process_group(process)
            process.communicate()
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
        except KeyboardInterrupt:
            _kill_process_group(process)
            process.wait()
            raise
        finally:
            with self._live_lock:
                self._live.discard(process)
        result.update(
            {
                "duration_ms": int((time.monotonic() - started) * 1000),
                "exit_code": process.returncode,
                "stderr_tail": _bounded_tail(stderr),
                "stdout_tail": _bounded_tail(stdout),
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
    ) -> str | None:
        evidence["verified"] = verified
        try:
            directory = self._evidence_root / str(evidence.get("dedupe_key") or "unknown")
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S.%fZ")
            correlation = str(evidence.get("correlation_id") or "no-correlation")[:8]
            target = directory / f"{stamp}-{correlation}.json"
            _write_json_atomic(target, evidence)
            stale = sorted(directory.glob("*.json"))[:-EVIDENCE_KEEP_PER_ITEM]
            for old in stale:
                old.unlink(missing_ok=True)
            for orphan in directory.glob(".*.tmp"):
                orphan.unlink(missing_ok=True)
        except OSError:
            return "factory_evidence_write_failed"
        return None


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
    queued_rows: dict[str, dict[str, object]] = {}
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
                connection.row_factory = sqlite3.Row
                for row in connection.execute(
                    """
                    SELECT dedupe_key, title, harness, prompt, receipt_value
                    FROM jobs WHERE workflow = ?
                    """,
                    (config.name,),
                ):
                    key = str(row["dedupe_key"])
                    queued.append(key)
                    queued_rows[key] = dict(row)
        except sqlite3.DatabaseError as exc:
            state_db_error = str(exc)[:200]
    unsupported = sorted(
        item.dedupe_key for item in items.values() if item.harness not in worker_harnesses
    )
    agent_budget = int(config.coordinator.agent_timeout_seconds)
    warnings = [
        f"{item.dedupe_key}: check[{index}] timeout_seconds={check.timeout_seconds}"
        f" exceeds agent_timeout_seconds={agent_budget}; the dispatch deadline will truncate it"
        for item in items.values()
        for index, check in enumerate(item.checks)
        if check.timeout_seconds > agent_budget
    ]
    warnings += [
        f"{item.dedupe_key}: check[{index}] argv[0]={check.argv[0]!r} does not"
        " resolve on PATH; the check will fail with exit_code=127"
        for item in items.values()
        for index, check in enumerate(item.checks)
        if not Path(check.argv[0]).is_absolute() and shutil.which(check.argv[0]) is None
    ]
    for item in items.values():
        row = queued_rows.get(item.dedupe_key)
        if row is None:
            continue
        try:
            prompt: str | None = item.prompt_file.read_text(encoding="utf-8").strip()
        except OSError:
            prompt = None
        fields = [
            ("title", item.title, row["title"]),
            ("harness", item.harness.value, row["harness"]),
            ("receipt", item.receipt, row["receipt_value"]),
        ]
        if prompt is not None:
            fields.append(("prompt", prompt, row["prompt"]))
        drifted = [field for field, current, durable in fields if str(current) != str(durable)]
        if drifted:
            warnings.append(
                f"{item.dedupe_key}: {', '.join(drifted)} changed after the job was"
                " queued; the next intake will fail with dedupe_contract_conflict"
            )
    print(
        json.dumps(
            {
                "items": [
                    {
                        "checks": [list(check.argv) for check in item.checks],
                        "dedupe_key": item.dedupe_key,
                        "harness": item.harness.value,
                        "harness_supported": item.harness in worker_harnesses,
                        "max_attempts": item.max_attempts,
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
                "unsupported_harnesses": unsupported,
                "valid": not unsupported,
                "warnings": warnings,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if not unsupported else 2


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
            {
                "dedupe_key": required,
                "state": state_by_key.get(required, "unqueued"),
            }
            for required in item.requires
            if state_by_key.get(required) != JobState.SUCCEEDED.value
        ]
        if blockers:
            waiting.append({"dedupe_key": item.dedupe_key, "waiting_on": blockers})
            continue
        job_id, created, selected = coordinator.enqueue_prompt_file(
            harness=item.harness,
            title=item.title,
            prompt_file=item.prompt_file,
            dedupe_key=item.dedupe_key,
            placement=PlacementTarget.PANE,
            receipt=TaskReceipt(ReceiptKind.FILE, item.receipt),
            max_attempts=item.max_attempts,
        )
        added += int(created)
        if created:
            state_by_key[item.dedupe_key] = JobState.PENDING.value
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


def _run_jobs_summary(coordinator: Coordinator) -> list[dict[str, object]]:
    workspace = str(coordinator.config.workspace.resolve())
    return [
        {
            "dedupe_key": job["dedupe_key"],
            "error_code": job["error_code"],
            "harness": job["harness"],
            "job_id": job["id"],
            "state": job["state"],
            "task_verified": job["task_verified"],
        }
        for job in coordinator.store.jobs(
            coordinator.config.name, workspace=workspace, include_legacy=True
        )
    ]


def _install_signal_abort(dispatcher: object) -> dict[int, Any]:
    """Point SIGINT and SIGTERM at ``dispatcher.abort()`` so either signal
    kills in-flight check process groups immediately instead of waiting out
    the check's communicate timeout. SIGINT exits 130, SIGTERM exits the
    conventional 143; the claimed attempt's lease lapses and is reclaimed
    later either way. Returns the previous handlers (empty when signals are
    unavailable, e.g. off the main thread)."""

    def _handler(_signum: int, _frame: Any) -> None:
        abort = getattr(dispatcher, "abort", None)
        if callable(abort):
            abort()
        if _signum == signal.SIGINT:
            raise KeyboardInterrupt
        print("terminated", file=sys.stderr)
        raise SystemExit(128 + _signum)

    previous: dict[int, Any] = {}
    try:
        for signum in (signal.SIGINT, signal.SIGTERM):
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, _handler)
    except ValueError:
        for signum, handler in previous.items():
            signal.signal(signum, handler)
        return {}
    return previous


def _restore_signal_abort(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        with suppress(ValueError):
            signal.signal(signum, handler)


def _command_run(args: argparse.Namespace) -> int:
    coordinator, _ = _build_coordinator(args.workflow, args.backlog)
    previous_handlers = _install_signal_abort(coordinator.dispatcher)
    try:
        return _command_run_inner(args, coordinator)
    finally:
        _restore_signal_abort(previous_handlers)


def _command_run_inner(args: argparse.Namespace, coordinator: Coordinator) -> int:
    if args.once:
        report = coordinator.run_once()
        report["jobs"] = _run_jobs_summary(coordinator)
        print(json.dumps(report, sort_keys=True))
        return 1 if report.get("failed") or report.get("blocked") else 0
    result = coordinator.run_until_idle(timeout_seconds=args.drain_timeout_seconds)
    result["jobs"] = _run_jobs_summary(coordinator)
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
        state_by_key = {str(job["dedupe_key"]): str(job["state"]) for job in jobs}
        backlog: dict[str, object] = {
            "items": len(items),
            "requires": {
                item.dedupe_key: list(item.requires) for item in items.values() if item.requires
            },
            "unqueued": sorted(key for key in items if key not in queued),
            "waiting": {
                item.dedupe_key: unmet
                for item in items.values()
                if item.dedupe_key not in queued
                and item.requires
                and (
                    unmet := [
                        {
                            "dedupe_key": required,
                            "state": state_by_key.get(required, "unqueued"),
                        }
                        for required in item.requires
                        if state_by_key.get(required) != JobState.SUCCEEDED.value
                    ]
                )
            },
        }
    except FactoryError as exc:
        backlog = {"error": str(exc), "items": 0, "unqueued": [], "waiting": {}}
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
                        "harness": job["harness"],
                        "id": job["id"],
                        "lease_expired": (
                            job["lease_until"] is not None
                            and float(job["lease_until"]) <= time.time()
                        ),
                        "max_attempts": job["max_attempts"],
                        "receipt_kind": job["receipt_kind"],
                        "receipt_value": job["receipt_value"],
                        "retry_backoff_seconds": (
                            max(0, round(float(job["available_at"]) - time.time()))
                            if job["state"] == "pending"
                            else None
                        ),
                        "state": job["state"],
                        "task_verified": job["task_verified"],
                        "title": job["title"],
                        "updated_at_utc": datetime.fromtimestamp(
                            float(job["updated_at"]), UTC
                        ).isoformat(),
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
    queued = {str(job["dedupe_key"]) for job in jobs}
    state_by_key = {str(job["dedupe_key"]): str(job["state"]) for job in jobs}
    try:
        backlog_items = load_backlog(args.backlog)
        unqueued = sorted(key for key in backlog_items if key not in queued)
        waiting = [
            f"{item.dedupe_key} (waiting on: "
            + ", ".join(
                f"{required}={state_by_key.get(required, 'unqueued')}"
                for required in item.requires
                if state_by_key.get(required) != JobState.SUCCEEDED.value
            )
            + ")"
            for item in backlog_items.values()
            if item.dedupe_key in unqueued and item.requires
        ]
        lines += [
            "",
            "## Backlog coverage",
            "",
            f"- items: {len(backlog_items)}",
            f"- queued: {len(backlog_items) - len(unqueued)}",
            f"- unqueued: {', '.join(unqueued) if unqueued else 'none'}",
            f"- waiting on requires: {', '.join(waiting) if waiting else 'none'}",
        ]
    except FactoryError as exc:
        lines += ["", "## Backlog coverage", "", f"- error: `{exc}`"]
    report_root = config.workspace / ".orchestrator" / "factory"
    try:
        report_root.mkdir(parents=True, exist_ok=True)
        target = report_root / "report.md"
        _write_text_atomic(target, "\n".join(lines) + "\n")
    except OSError as exc:
        raise FactoryError(f"factory_report_write_failed: {exc}") from exc
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
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130
    except sqlite3.DatabaseError as exc:
        print(f"factory_state_db_unreadable: {exc}", file=sys.stderr)
        return 2
    except (ConfigError, FactoryError, StoreError, TransportError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
