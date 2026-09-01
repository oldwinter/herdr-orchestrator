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
    RepairReceipt,
    ReviewReport,
    load_repair_receipt,
    load_review_axis,
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


class _RevalidateReview(Protocol):
    def __call__(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        round_number: int,
        integration_commit: str,
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


class _ReconstructReviewArtifacts(Protocol):
    def __call__(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        round_number: int,
        integration_commit: str,
    ) -> None: ...


class DeliveryRepairMixin:
    config: WorkflowConfig
    controller: Harness
    worker_harnesses: tuple[Harness, ...]
    _run_root: Path
    _journal: DeliveryJournal | None
    _record: _Record
    _review: _Review
    _revalidate_review: _RevalidateReview
    _dispatch_artifact: _DispatchArtifact
    _select_worker: _SelectWorker
    _dispatch_with_proxy: _DispatchWithProxy
    _reconstruct_review_artifacts: _ReconstructReviewArtifacts
    _preflight_legacy_agent: Callable[[Path, Harness, str], None]
    _require_journal: Callable[[], DeliveryJournal]
    _is_legacy_migration: Callable[[], bool]

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

    def _revalidate_repair_history(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        review_rounds: int,
        integration_commit: str,
    ) -> None:
        journal = self._require_journal()
        attempts = self._repair_attempts()
        if review_rounds != attempts + 1:
            raise DeliveryError("delivery_result_review_rounds_invalid")
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        expected_review_commit: str | None = None
        for round_number in range(1, review_rounds + 1):
            review = journal.require_confirmed(f"review:accept:{round_number}")
            review_commit = review.get("integration_commit")
            if (
                not isinstance(review_commit, str)
                or (expected_review_commit is not None and review_commit != expected_review_commit)
                or (round_number == review_rounds and review_commit != integration_commit)
            ):
                raise DeliveryError("delivery_recovery_conflict:review.accept")
            report = self._revalidate_review(
                plan,
                integration,
                round_number,
                review_commit,
            )
            must_fix = self._revalidate_adjudication(
                plan,
                report,
                round_number,
            )
            if round_number <= attempts:
                if not must_fix:
                    raise DeliveryError("delivery_recovery_conflict:repair.commit")
                expected_review_commit = self._revalidate_repair_round(
                    git,
                    integration,
                    round_number,
                    review_commit,
                )
            elif must_fix:
                raise DeliveryError("delivery_result_review_gate_failed")

    def _revalidate_adjudication(
        self,
        plan: DeliveryPlan,
        report: ReviewReport,
        round_number: int,
    ) -> dict[str, object]:
        findings = _finding_map(report)
        if not findings:
            verdict_file = self._run_root / "reviews" / f"round-{round_number}" / "verdict.json"
            if verdict_file.exists():
                raise DeliveryError("delivery_recovery_conflict:review.adjudicate")
            return {}
        verdict_file = self._run_root / "reviews" / f"round-{round_number}" / "verdict.json"
        try:
            journal = self._require_journal()
            key = f"agent:artifact:judge-{round_number}"
            if not journal.has_intent(key) and not self._is_legacy_migration():
                journal.require_confirmed(key)
            self._dispatch_artifact(
                self.config.workspace,
                self.controller,
                review_verdict_prompt(plan, findings, verdict_file),
                verdict_file,
                role=f"judge-{round_number}",
            )
            journal.require_confirmed(key)
            verdict = load_review_verdict(
                verdict_file,
                candidates=tuple(findings),
            )
        except (DeliveryArtifactError, DeliveryError) as exc:
            raise DeliveryError("delivery_recovery_conflict:review.adjudicate") from exc
        return {
            finding_id: findings[finding_id]
            for finding_id in verdict.accepted
            if findings[finding_id].severity is FindingSeverity.MUST_FIX
        }

    def _revalidate_repair_round(
        self,
        git: GitWorkspace,
        integration: Worktree,
        round_number: int,
        before: str,
    ) -> str:
        journal = self._require_journal()
        operation_key = f"repair:commit:{round_number}"
        intent = journal._intent_details(operation_key)
        confirmation = journal.require_confirmed(operation_key)
        receipt_file = self._repair_receipt_path(round_number)
        expected_receipt = str(receipt_file.relative_to(self._run_root))
        if intent != {
            "round": round_number,
            "before_commit": before,
            "receipt": expected_receipt,
        }:
            raise DeliveryError("delivery_recovery_conflict:repair.commit")
        try:
            receipt = load_repair_receipt(
                receipt_file,
                round_number=round_number,
                before_commit=before,
            )
            parents = git.parents(integration.path, receipt.commit)
            current = git.head(integration)
        except (DeliveryArtifactError, GitWorkspaceError) as exc:
            raise DeliveryError("delivery_recovery_conflict:repair.commit") from exc
        expected = {
            "round": round_number,
            "before_commit": before,
            "commit": receipt.commit,
            "receipt_sha256": _file_sha256(receipt_file),
        }
        if (
            confirmation != expected
            or parents != (before,)
            or not git.is_ancestor(integration.path, receipt.commit, current)
        ):
            raise DeliveryError("delivery_recovery_conflict:repair.commit")
        return receipt.commit

    def _inspect_completed_legacy_review_history(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        review_rounds: int,
        integration_commit: str,
    ) -> tuple[str, ...]:
        attempts = self._repair_attempts()
        if self._repair_inflight() is not None or review_rounds != attempts + 1:
            raise DeliveryError("delivery_result_review_rounds_invalid")
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        review_commits = self._legacy_review_commits(
            git,
            integration,
            review_rounds,
            integration_commit,
        )
        for round_number, review_commit in enumerate(review_commits, 1):
            self._inspect_completed_legacy_review_round(
                plan,
                integration,
                git,
                round_number,
                review_commit,
                integration_commit,
                needs_repair=round_number <= attempts,
            )
        return review_commits

    def _legacy_review_commits(
        self,
        git: GitWorkspace,
        integration: Worktree,
        review_rounds: int,
        integration_commit: str,
    ) -> tuple[str, ...]:
        review_commits: list[str] = []
        prior_repair: str | None = None
        for round_number in range(1, review_rounds):
            receipt = self._legacy_repair_receipt(round_number)
            if prior_repair is not None and receipt.before_commit != prior_repair:
                raise DeliveryError("delivery_recovery_conflict:repair.commit")
            try:
                parents = git.parents(integration.path, receipt.commit)
            except GitWorkspaceError as exc:
                raise DeliveryError("delivery_recovery_conflict:repair.commit") from exc
            if parents != (receipt.before_commit,):
                raise DeliveryError("delivery_recovery_conflict:repair.commit")
            review_commits.append(receipt.before_commit)
            prior_repair = receipt.commit
        if prior_repair is not None and prior_repair != integration_commit:
            raise DeliveryError("delivery_recovery_conflict:repair.commit")
        review_commits.append(integration_commit)
        return tuple(review_commits)

    def _inspect_completed_legacy_review_round(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        git: GitWorkspace,
        round_number: int,
        review_commit: str,
        integration_commit: str,
        *,
        needs_repair: bool,
    ) -> None:
        try:
            if not git.is_ancestor(
                integration.path,
                review_commit,
                integration_commit,
            ):
                raise DeliveryError("delivery_recovery_conflict:review.accept")
            report = self._load_legacy_review_report(round_number)
        except (DeliveryArtifactError, GitWorkspaceError) as exc:
            raise DeliveryError("delivery_recovery_conflict:review.accept") from exc
        for harness in self.worker_harnesses:
            for axis in ("standards", "spec"):
                self._preflight_legacy_agent(
                    integration.path,
                    harness,
                    f"review-{axis}-{round_number}",
                )
        must_fix = self._inspect_legacy_adjudication(plan, report, round_number)
        if needs_repair:
            if not must_fix:
                raise DeliveryError("delivery_recovery_conflict:repair.commit")
            for harness in self.worker_harnesses:
                self._preflight_legacy_agent(
                    integration.path,
                    harness,
                    f"repair-{round_number}",
                )
        elif must_fix:
            raise DeliveryError("delivery_result_review_gate_failed")

    def _load_legacy_review_report(self, round_number: int) -> ReviewReport:
        review_root = self._run_root / "reviews" / f"round-{round_number}"
        return ReviewReport(
            standards=load_review_axis(review_root / "standards.json", "standards"),
            spec=load_review_axis(review_root / "spec.json", "spec"),
        )

    def _inspect_legacy_adjudication(
        self,
        plan: DeliveryPlan,
        report: ReviewReport,
        round_number: int,
    ) -> dict[str, object]:
        findings = _finding_map(report)
        if not findings:
            verdict_file = self._run_root / "reviews" / f"round-{round_number}" / "verdict.json"
            if verdict_file.exists():
                raise DeliveryError("delivery_recovery_conflict:review.adjudicate")
            return {}
        self._preflight_legacy_agent(
            self.config.workspace,
            self.controller,
            f"judge-{round_number}",
        )
        try:
            verdict = load_review_verdict(
                self._run_root / "reviews" / f"round-{round_number}" / "verdict.json",
                candidates=tuple(findings),
            )
        except DeliveryArtifactError as exc:
            raise DeliveryError("delivery_recovery_conflict:review.adjudicate") from exc
        return {
            finding_id: findings[finding_id]
            for finding_id in verdict.accepted
            if findings[finding_id].severity is FindingSeverity.MUST_FIX
        }

    def _reconstruct_completed_legacy_review_history(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        review_commits: tuple[str, ...],
    ) -> None:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        for round_number, review_commit in enumerate(review_commits, 1):
            self._reconstruct_review_artifacts(
                plan,
                integration,
                round_number,
                review_commit,
            )
            report = self._revalidate_review(
                plan,
                integration,
                round_number,
                review_commit,
            )
            self._revalidate_adjudication(plan, report, round_number)
            if round_number < len(review_commits):
                self._confirm_legacy_repair_round(
                    git,
                    integration,
                    round_number,
                    review_commit,
                )
        self._revalidate_repair_history(
            plan,
            integration,
            len(review_commits),
            review_commits[-1],
        )

    def _confirm_legacy_repair_round(
        self,
        git: GitWorkspace,
        integration: Worktree,
        round_number: int,
        before: str,
    ) -> None:
        receipt = self._legacy_repair_receipt(round_number)
        if receipt.before_commit != before:
            raise DeliveryError("delivery_recovery_conflict:repair.commit")
        receipt_file = self._run_root / "repairs" / f"round-{round_number}.json"
        details: dict[str, object] = {
            "round": round_number,
            "before_commit": before,
            "commit": receipt.commit,
            "receipt_sha256": _file_sha256(receipt_file),
        }
        self._require_journal().reconcile(
            DeliveryEffect(
                key=f"repair:commit:{round_number}",
                kind="repair.commit",
                intent={
                    "round": round_number,
                    "before_commit": before,
                    "receipt": str(receipt_file.relative_to(self._run_root)),
                },
                observe=lambda expected, started: _effect_matched(details),
                apply=lambda: details,
            )
        )
        self._revalidate_repair_round(
            git,
            integration,
            round_number,
            before,
        )

    def _legacy_repair_receipt(self, round_number: int) -> RepairReceipt:
        receipt_file = self._run_root / "repairs" / f"round-{round_number}.json"
        _safe_delivery_path(receipt_file, root=self._run_root)
        try:
            payload = json.loads(receipt_file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeliveryError("delivery_recovery_conflict:repair.commit") from exc
        before = payload.get("before_commit") if isinstance(payload, dict) else None
        if not isinstance(before, str):
            raise DeliveryError("delivery_recovery_conflict:repair.commit")
        try:
            return load_repair_receipt(
                receipt_file,
                round_number=round_number,
                before_commit=before,
            )
        except DeliveryArtifactError as exc:
            raise DeliveryError("delivery_recovery_conflict:repair.commit") from exc

    def _repair_result_preconditions(
        self,
        review_rounds: int,
    ) -> tuple[dict[str, object], dict[str, object]]:
        journal = self._require_journal()
        repairs: dict[str, object] = {}
        for round_number in range(1, review_rounds):
            repair = journal.require_confirmed(f"repair:commit:{round_number}")
            repairs[str(round_number)] = {
                "commit": repair.get("commit"),
                "receipt_sha256": repair.get("receipt_sha256"),
            }
        adjudications: dict[str, object] = {}
        for round_number in range(1, review_rounds + 1):
            key = f"agent:artifact:judge-{round_number}"
            if journal.has_intent(key):
                adjudication = journal.require_confirmed(key)
                adjudications[str(round_number)] = {
                    "artifact_sha256": adjudication.get("artifact_sha256"),
                    "prompt_sha256": adjudication.get("prompt_sha256"),
                }
        return repairs, adjudications

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
