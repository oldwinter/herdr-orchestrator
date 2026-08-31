from __future__ import annotations

import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.catalog import (
    execution_prompt,
    profile_for_harness,
)
from herdr_orchestrator.delivery_journal import (
    DeliveryEffect,
    DeliveryEffectObservation,
    DeliveryJournal,
)
from herdr_orchestrator.delivery_prompts import (
    repair_prompt,
    review_verdict_prompt,
)
from herdr_orchestrator.delivery_protocol import (
    DeliveryArtifactError,
    DeliveryPlan,
    FindingSeverity,
    ReviewReport,
    load_repair_receipt,
    load_review_verdict,
)
from herdr_orchestrator.delivery_recovery import (
    DeliveryError,
    _effect_absent,
    _effect_conflict,
    _effect_matched,
    _file_sha256,
    _finding_map,
    _git_output,
    _require_success,
    _safe_delivery_path,
    _validate_worktree_ownership,
    _write_json,
)
from herdr_orchestrator.git_workspace import GitWorkspace, GitWorkspaceError, Worktree
from herdr_orchestrator.model import (
    DispatchOutcome,
    Harness,
    HarnessProfile,
    WorkflowConfig,
)


class _Record(Protocol):
    def __call__(self, event: str, details: dict[str, object]) -> None: ...


class _Review(Protocol):
    def __call__(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        round_number: int,
    ) -> ReviewReport: ...


class _DispatchArtifact(Protocol):
    def __call__(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        output_file: Path,
        *,
        role: str,
        journal_context: dict[str, object] | None = None,
        use_principal_proxy: bool = True,
        agent_name_override: str | None = None,
    ) -> None: ...


class _SelectWorker(Protocol):
    def __call__(self, title: str, prompt: str, dedupe_key: str) -> Harness: ...


class _DispatchWithProxy(Protocol):
    def __call__(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        role: str,
        agent_name_override: str | None = None,
    ) -> DispatchOutcome: ...


class DeliveryRepairMixin:
    config: WorkflowConfig
    controller: Harness
    _run_root: Path
    _journal: DeliveryJournal | None
    _record: _Record
    _review: _Review
    _dispatch_artifact: _DispatchArtifact
    _select_worker: _SelectWorker
    _dispatch_with_proxy: _DispatchWithProxy
    _require_journal: Callable[[], DeliveryJournal]

    def _review_and_repair(self, plan: DeliveryPlan, integration: Worktree) -> int:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        repair_attempts = self._reconcile_repair_attempt(git, integration)
        while True:
            round_number = repair_attempts + 1
            report = self._review(plan, integration, round_number)
            findings = _finding_map(report)
            if not findings:
                self._record(
                    "review_completed",
                    {"round": round_number, "findings": 0, "accepted": []},
                )
                return round_number
            verdict_file = self._run_root / "reviews" / f"round-{round_number}" / "verdict.json"
            self._dispatch_artifact(
                self.config.workspace,
                self.controller,
                review_verdict_prompt(plan, findings, verdict_file),
                verdict_file,
                role=f"judge-{round_number}",
            )
            verdict = load_review_verdict(
                verdict_file,
                candidates=tuple(findings),
            )
            accepted = {finding_id: findings[finding_id] for finding_id in verdict.accepted}
            must_fix = {
                finding_id: finding
                for finding_id, finding in accepted.items()
                if finding.severity is FindingSeverity.MUST_FIX
            }
            self._record(
                "review_adjudicated",
                {
                    "round": round_number,
                    "findings": len(findings),
                    "accepted": list(verdict.accepted),
                    "dismissed": list(verdict.dismissed),
                    "must_fix": list(must_fix),
                },
            )
            if not must_fix:
                return round_number
            _validate_worktree_ownership(
                git,
                self._run_root / "worktrees" / "integration",
                integration,
            )
            repair_number, before = self._begin_repair_attempt(git, integration)
            harness = self._select_worker(
                f"Repair accepted review findings, round {repair_number}",
                json.dumps(
                    {key: finding.summary for key, finding in must_fix.items()},
                    ensure_ascii=False,
                ),
                f"{plan.slug}:repair:{repair_number}",
            )
            profile = profile_for_harness(self.config.profiles, harness)
            receipt_file = self._repair_receipt_path(repair_number)
            repair_instructions = (
                f"{repair_prompt(plan, must_fix, repair_number)}\n\n"
                "After committing, write only this additional UTF-8 JSON artifact:\n"
                f"{receipt_file}\n\n"
                "Exact schema:\n"
                f'{{"round":{repair_number},"before_commit":"{before}",'
                '"commit":"<full HEAD commit SHA>"}'
            )

            def dispatch_repair(
                selected_harness: Harness = harness,
                selected_profile: HarnessProfile = profile,
                instructions: str = repair_instructions,
                selected_round: int = repair_number,
            ) -> None:
                outcome = self._dispatch_with_proxy(
                    integration.path,
                    selected_harness,
                    execution_prompt(selected_profile, instructions),
                    role=f"repair-{selected_round}",
                )
                _require_success(outcome, f"repair_{selected_round}")

            if self._journal is None:
                outcome = self._dispatch_with_proxy(
                    integration.path,
                    harness,
                    execution_prompt(
                        profile,
                        repair_prompt(plan, must_fix, repair_number),
                    ),
                    role=f"repair-{repair_number}",
                )
                _require_success(outcome, f"repair_{repair_number}")
                _validate_worktree_ownership(
                    git,
                    self._run_root / "worktrees" / "integration",
                    integration,
                )
                after = git.validate_commit(Worktree(integration.path, integration.branch, before))
            else:
                after = self._reconcile_repair_commit(
                    git,
                    integration,
                    repair_number,
                    before,
                    dispatch=dispatch_repair,
                )
            self._complete_repair_attempt(repair_number, after)
            repair_attempts = repair_number

    def _begin_repair_attempt(
        self,
        git: GitWorkspace,
        integration: Worktree,
    ) -> tuple[int, str]:
        path = self._run_root / "repair-state.json"
        attempts = self._reconcile_repair_attempt(git, integration)
        current = git.head(integration)
        if attempts >= self.config.standardized_delivery.review_repair_rounds:
            raise DeliveryError("review_repair_rounds_exhausted")
        round_number = attempts + 1
        _write_json(
            path,
            {
                "attempts": attempts,
                "in_flight": {"round": round_number, "before": current},
            },
        )
        self._record("review_repair_claimed", {"round": round_number})
        return round_number, current

    def _reconcile_repair_attempt(self, git: GitWorkspace, integration: Worktree) -> int:
        attempts = self._repair_attempts()
        inflight = self._repair_inflight()
        if inflight is None:
            return attempts
        round_number, before = inflight
        if round_number != attempts + 1:
            raise DeliveryError("delivery_repair_state_invalid")
        current = git.head(integration)
        if current != before:
            if self._journal is not None:
                current = self._reconcile_repair_commit(
                    git,
                    integration,
                    round_number,
                    before,
                    dispatch=None,
                )
            attempts = round_number
            self._record(
                "review_repair_recovered",
                {"round": round_number, "commit": current},
            )
        _write_json(
            self._run_root / "repair-state.json",
            {"attempts": attempts, "in_flight": None},
        )
        return attempts

    def _reconcile_repair_commit(
        self,
        git: GitWorkspace,
        integration: Worktree,
        round_number: int,
        before: str,
        *,
        dispatch: Callable[[], None] | None,
    ) -> str:
        journal = self._require_journal()
        receipt_file = self._repair_receipt_path(round_number)
        operation_key = f"repair:commit:{round_number}"
        pending = journal.has_intent(operation_key)

        def repair_details() -> dict[str, object]:
            current = git.head(integration)
            _validate_worktree_ownership(
                git,
                self._run_root / "worktrees" / "integration",
                integration,
            )
            try:
                receipt = load_repair_receipt(
                    receipt_file,
                    round_number=round_number,
                    before_commit=before,
                )
            except DeliveryArtifactError as exc:
                raise DeliveryError("delivery_recovery_conflict:repair.commit") from exc
            current = git.validate_commit(Worktree(integration.path, integration.branch, before))
            parents = git.parents(integration.path, current)
            if receipt.commit != current or parents != (before,):
                raise DeliveryError("delivery_recovery_conflict:repair.commit")
            return {
                "round": round_number,
                "before_commit": before,
                "commit": current,
                "receipt_sha256": _file_sha256(receipt_file),
            }

        def observe(
            expected: dict[str, object] | None,
            started: bool,
        ) -> DeliveryEffectObservation:
            current = git.head(integration)
            if expected is not None:
                confirmed_commit = expected.get("commit")
                if not isinstance(confirmed_commit, str) or not git.is_ancestor(
                    integration.path,
                    confirmed_commit,
                    current,
                ):
                    return _effect_conflict()
                try:
                    details = repair_details()
                except (DeliveryError, DeliveryArtifactError, GitWorkspaceError):
                    return _effect_conflict()
                return _effect_matched(expected if details == expected else details)
            if current == before:
                if receipt_file.exists() or started:
                    return _effect_conflict()
                return _effect_absent()
            try:
                return _effect_matched(repair_details())
            except (DeliveryError, DeliveryArtifactError, GitWorkspaceError):
                return _effect_conflict()

        def repair() -> dict[str, object]:
            if git.head(integration) == before:
                if dispatch is None:
                    raise DeliveryError("delivery_recovery_conflict:repair.commit")
                if not pending:
                    receipt_file.unlink(missing_ok=True)
                dispatch()
            return repair_details()

        payload = journal.reconcile(
            DeliveryEffect(
                key=operation_key,
                kind="repair.commit",
                intent={
                    "round": round_number,
                    "before_commit": before,
                    "receipt": str(receipt_file.relative_to(self._run_root)),
                },
                observe=observe,
                apply=repair,
            )
        )
        commit = payload.get("commit")
        if not isinstance(commit, str):
            raise DeliveryError("delivery_repair_confirmation_invalid")
        return commit

    def _repair_receipt_path(self, round_number: int) -> Path:
        path = self._run_root / "repairs" / f"round-{round_number}.json"
        _safe_delivery_path(path, root=self._run_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def _complete_repair_attempt(self, round_number: int, commit: str) -> None:
        inflight = self._repair_inflight()
        if inflight is None or inflight[0] != round_number:
            raise DeliveryError("delivery_repair_state_invalid")
        _write_json(
            self._run_root / "repair-state.json",
            {"attempts": round_number, "in_flight": None},
        )
        self._record("review_repaired", {"round": round_number, "commit": commit})

    def _repair_attempts(self) -> int:
        path = self._run_root / "repair-state.json"
        _safe_delivery_path(path, root=self._run_root)
        if not path.is_file():
            return 0
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeliveryError("delivery_repair_state_invalid") from exc
        attempts = payload.get("attempts") if isinstance(payload, dict) else None
        if (
            not isinstance(payload, dict)
            or set(payload) not in ({"attempts"}, {"attempts", "in_flight"})
            or not isinstance(attempts, int)
            or isinstance(attempts, bool)
            or not 0 <= attempts <= self.config.standardized_delivery.review_repair_rounds
        ):
            raise DeliveryError("delivery_repair_state_invalid")
        return attempts

    def _repair_inflight(self) -> tuple[int, str] | None:
        path = self._run_root / "repair-state.json"
        _safe_delivery_path(path, root=self._run_root)
        if not path.is_file():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeliveryError("delivery_repair_state_invalid") from exc
        inflight = payload.get("in_flight") if isinstance(payload, dict) else None
        if isinstance(payload, dict) and "in_flight" not in payload:
            return None
        if inflight is None:
            return None
        if (
            not isinstance(inflight, dict)
            or set(inflight) != {"round", "before"}
            or not isinstance(inflight["round"], int)
            or isinstance(inflight["round"], bool)
            or not 1
            <= inflight["round"]
            <= (self.config.standardized_delivery.review_repair_rounds)
            or not isinstance(inflight["before"], str)
            or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", inflight["before"])
        ):
            raise DeliveryError("delivery_repair_state_invalid")
        return inflight["round"], inflight["before"]

    def _delivery_base_commit(self, git: GitWorkspace) -> str:
        path = self._run_root / "git-base.json"
        _safe_delivery_path(path, root=self._run_root)
        repository = str(self.config.workspace.resolve())
        if path.is_file():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise DeliveryError("delivery_git_base_invalid") from exc
            if (
                not isinstance(payload, dict)
                or set(payload) != {"commit", "repository"}
                or payload["repository"] != repository
                or not isinstance(payload["commit"], str)
                or not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", payload["commit"])
            ):
                raise DeliveryError("delivery_git_base_invalid")
            commit = payload["commit"]
        else:
            if (self._run_root / "worktrees" / "integration").exists():
                raise DeliveryError("delivery_git_base_missing")
            commit = git.base_commit()
            _write_json(path, {"commit": commit, "repository": repository})
        if _git_output(git, self.config.workspace, "cat-file", "-t", commit) != "commit":
            raise DeliveryError("delivery_git_base_invalid")
        return commit
