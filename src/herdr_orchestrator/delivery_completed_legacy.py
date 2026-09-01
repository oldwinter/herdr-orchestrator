from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.delivery_journal import (
    DeliveryEffect,
    DeliveryEffectObservation,
    DeliveryJournal,
)
from herdr_orchestrator.delivery_legacy import legacy_migration_payload
from herdr_orchestrator.delivery_protocol import (
    DeliveryPlan,
    TicketReceipt,
    WayfinderMap,
    load_delivery_plan,
)
from herdr_orchestrator.delivery_recovery import (
    DeliveryError,
    DeliveryResult,
    _effect_conflict,
    _effect_matched,
    _file_sha256,
    _load_completed_result,
)
from herdr_orchestrator.git_workspace import GitWorkspace, GitWorkspaceError, Worktree
from herdr_orchestrator.model import Harness, WorkflowConfig
from herdr_orchestrator.tracker import (
    DeliveryTracker,
    TrackerMarkers,
    TrackerTicket,
    tracker_markers,
    tracker_markers_from_payload,
)


class _CreatePlan(Protocol):
    def __call__(self, wayfinder: WayfinderMap | None) -> DeliveryPlan: ...


class _PreflightLegacyDelivery(Protocol):
    def __call__(
        self,
        plan: DeliveryPlan,
        references: dict[str, TrackerTicket],
        spec_url: str | None,
        markers: TrackerMarkers,
        *,
        require_closed: bool = False,
    ) -> dict[str, TicketReceipt]: ...


class _WorktreeEffect(Protocol):
    def __call__(
        self,
        git: GitWorkspace,
        *,
        operation_key: str,
        kind: str,
        base_commit: str,
        ticket_id: str | None = None,
    ) -> Worktree: ...


class _RecoverIntegratedTickets(Protocol):
    def __call__(
        self,
        git: GitWorkspace,
        plan: DeliveryPlan,
        integration: Worktree,
    ) -> set[str]: ...


class _InspectLegacyReviewHistory(Protocol):
    def __call__(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        review_rounds: int,
        integration_commit: str,
    ) -> tuple[str, ...]: ...


class _ReconstructLegacyReviewHistory(Protocol):
    def __call__(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        review_commits: tuple[str, ...],
    ) -> None: ...


class CompletedLegacyRecoveryMixin:
    config: WorkflowConfig
    controller: Harness
    worker_harnesses: tuple[Harness, ...]
    tracker: DeliveryTracker
    _run_id: str
    _run_root: Path
    _journal: DeliveryJournal | None
    _legacy_reconstruction_read_only: bool
    _create_plan: _CreatePlan
    _preflight_legacy_delivery: _PreflightLegacyDelivery
    _preflight_legacy_agent: Callable[[Path, Harness, str], None]
    _worktree_effect: _WorktreeEffect
    _recover_integrated_tickets: _RecoverIntegratedTickets
    _inspect_completed_legacy_review_history: _InspectLegacyReviewHistory
    _reconstruct_completed_legacy_review_history: _ReconstructLegacyReviewHistory
    _delivery_base_commit: Callable[[GitWorkspace], str]
    _restore_tracker_publication: Callable[[Path, DeliveryPlan], dict[str, TrackerTicket]]
    _publish_tracker: Callable[[DeliveryPlan], dict[str, TrackerTicket]]
    _result_preconditions: Callable[[DeliveryResult], dict[str, object]]
    _tracker_markers: Callable[[DeliveryPlan], TrackerMarkers]
    _require_journal: Callable[[], DeliveryJournal]

    def _recover_completed_legacy_result(self, result: DeliveryResult) -> None:
        plan = load_delivery_plan(self._run_root / "delivery-plan.json")
        references = self._restore_tracker_publication(
            self._run_root / "tracker-publication.json",
            plan,
        )
        if (
            result.tickets_completed != len(plan.tickets)
            or set(result.tracker_references) != {ticket.ticket_id for ticket in plan.tickets}
            or {ticket_id: reference.reference for ticket_id, reference in references.items()}
            != result.tracker_references
        ):
            raise DeliveryError("delivery_result_invalid")
        spec_url = getattr(self.tracker, "spec_url", None)
        tracker_intent = self._require_journal()._intent_details("tracker:publish")
        markers = (
            tracker_markers(self._run_id, plan)
            if tracker_intent is None
            else tracker_markers_from_payload(tracker_intent.get("markers"), plan)
        )
        receipts = self._preflight_legacy_delivery(
            plan,
            references,
            spec_url,
            markers,
            require_closed=True,
        )
        if set(receipts) != {ticket.ticket_id for ticket in plan.tickets}:
            raise DeliveryError("delivery_recovery_conflict:receipt.ticket.accept")
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        base_commit = self._delivery_base_commit(git)
        integration = self._completed_legacy_integration(git, result, base_commit)
        for receipt in receipts.values():
            if git.find_merge(integration, receipt.commit) is None:
                raise DeliveryError("delivery_recovery_conflict:git.integration.merge")
        review_commits = self._inspect_completed_legacy_review_history(
            plan,
            integration,
            result.review_rounds,
            result.integration_commit,
        )
        self._preflight_completed_legacy_routes()

        self._legacy_reconstruction_read_only = True
        try:
            if self._create_plan(None) != plan:
                raise DeliveryError("delivery_recovery_conflict:agent.dispatch")
            self._record_legacy_tracker_intent(
                plan,
                references,
                spec_url,
                markers,
                receipts,
            )
            integration = self._worktree_effect(
                git,
                operation_key="git:worktree:integration",
                kind="integration",
                base_commit=base_commit,
            )
            completed = self._recover_integrated_tickets(git, plan, integration)
            if completed != {ticket.ticket_id for ticket in plan.tickets}:
                raise DeliveryError("delivery_recovery_conflict:git.integration.merge")
            for ticket in plan.tickets:
                self._confirm_legacy_ticket_close(
                    plan,
                    ticket.ticket_id,
                    receipts[ticket.ticket_id],
                )
            self._reconstruct_completed_legacy_review_history(
                plan,
                integration,
                review_commits,
            )
            self._confirm_legacy_result(result)
            observed = self._publish_tracker(plan)
            if observed != references:
                raise DeliveryError("delivery_recovery_conflict:tracker.publish")
        finally:
            self._legacy_reconstruction_read_only = False

    def _completed_legacy_integration(
        self,
        git: GitWorkspace,
        result: DeliveryResult,
        base_commit: str,
    ) -> Worktree:
        path = self._run_root / "worktrees/integration"
        integration = Worktree(path, result.integration_branch, base_commit)
        try:
            git.validate_ownership(path, integration)
            git.validate_clean(path)
            head = git.head(integration)
        except GitWorkspaceError as exc:
            raise DeliveryError(str(exc)) from exc
        if head != result.integration_commit:
            raise DeliveryError("delivery_result_integration_commit_mismatch")
        return integration

    def _preflight_completed_legacy_routes(self) -> None:
        routes = self._run_root / "routes"
        if not routes.is_dir():
            return
        for artifact in routes.glob("*.json"):
            self._preflight_legacy_agent(
                self.config.workspace,
                self.controller,
                f"route-{artifact.stem[:5]}",
            )

    def _record_legacy_tracker_intent(
        self,
        plan: DeliveryPlan,
        references: dict[str, TrackerTicket],
        spec_url: str | None,
        markers: TrackerMarkers,
        receipts: dict[str, TicketReceipt],
    ) -> None:
        intent: dict[str, object] = {
            "markers": markers.payload(),
            "migration": legacy_migration_payload(
                self._run_root,
                references,
                spec_url,
                receipts,
                file_sha256=_file_sha256,
                completed=True,
            ),
        }
        self._require_journal().record_intent(
            DeliveryEffect(
                key="tracker:publish",
                kind="tracker.publish",
                intent=intent,
                observe=lambda expected, started: _effect_matched({}),
                apply=lambda: {},
            )
        )

    def _confirm_legacy_ticket_close(
        self,
        plan: DeliveryPlan,
        ticket_id: str,
        receipt: TicketReceipt,
    ) -> None:
        journal = self._require_journal()
        merge = journal.require_confirmed(f"git:merge:{ticket_id}")
        integration_commit = merge.get("integration_commit")
        if not isinstance(integration_commit, str):
            raise DeliveryError("delivery_recovery_conflict:git.integration.merge")
        marker = self._tracker_markers(plan).ticket(ticket_id)
        details: dict[str, object] = {
            "ticket_id": ticket_id,
            "receipt_commit": receipt.commit,
            "integration_commit": integration_commit,
            "status": "closed",
        }
        journal.reconcile(
            DeliveryEffect(
                key=f"tracker:close:{ticket_id}",
                kind="tracker.close",
                intent={
                    "ticket_id": ticket_id,
                    "receipt_commit": receipt.commit,
                    "integration_commit": integration_commit,
                    "marker": marker,
                },
                observe=lambda expected, started: _effect_matched(details),
                apply=lambda: details,
            )
        )

    def _confirm_legacy_result(self, result: DeliveryResult) -> None:
        path = self._run_root / "result.json"
        if _load_completed_result(path, result.run_id) != result:
            raise DeliveryError("delivery_recovery_conflict:result.publish")
        result_payload = {
            "run_id": result.run_id,
            "status": result.status,
            "tracker_references": result.tracker_references,
            "integration_branch": result.integration_branch,
            "integration_commit": result.integration_commit,
            "tickets_completed": result.tickets_completed,
            "review_rounds": result.review_rounds,
            "preconditions": self._result_preconditions(result),
        }
        details = {**result_payload, "result_sha256": _file_sha256(path)}
        self._require_journal().reconcile(
            DeliveryEffect(
                key="result:publish",
                kind="result.publish",
                intent=result_payload,
                observe=lambda expected, started: self._observe_completed_legacy_result(
                    result,
                    result_payload,
                    path,
                    expected,
                ),
                apply=lambda: details,
            )
        )

    def _observe_completed_legacy_result(
        self,
        result: DeliveryResult,
        result_payload: dict[str, object],
        path: Path,
        expected: dict[str, object] | None,
    ) -> DeliveryEffectObservation:
        if _load_completed_result(path, result.run_id) != result:
            return _effect_conflict()
        details = {**result_payload, "result_sha256": _file_sha256(path)}
        if expected is not None and details != expected:
            return _effect_conflict()
        return _effect_matched(details)
