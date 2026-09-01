from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.catalog import (
    execution_prompt,
    profile_for_harness,
)
from herdr_orchestrator.delivery_journal import (
    DeliveryEffect,
    DeliveryEffectObservation,
    DeliveryEffectState,
    DeliveryJournal,
)
from herdr_orchestrator.delivery_legacy import (
    completed_legacy_migration,
    existing_ticket_receipts,
    legacy_migration_payload,
)
from herdr_orchestrator.delivery_prompts import (
    implementation_prompt,
)
from herdr_orchestrator.delivery_protocol import (
    DeliveryArtifactError,
    DeliveryPlan,
    DeliveryTicket,
    ReviewFinding,
    ReviewReport,
    TicketReceipt,
    load_delivery_plan,
    load_ticket_receipt,
    load_tracker_publication,
    validate_artifact_path,
    write_artifact_text,
)
from herdr_orchestrator.git_workspace import GitWorkspace, GitWorkspaceError, Worktree
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    WorkflowConfig,
)
from herdr_orchestrator.protocol import TransportError
from herdr_orchestrator.tracker import (
    DeliveryTracker,
    TrackerError,
    TrackerMarkers,
    TrackerTicket,
    contains_high_confidence_secret,
    tracker_markers,
    tracker_markers_from_payload,
)

MAX_WAYFINDER_DECISIONS = 100
MAX_PROXY_ROUNDS = 8
ARTIFACT_PROMPT_ATTEMPTS = 2
DELIVERY_LEASE_GRACE_SECONDS = 30.0
MINIMUM_DELIVERY_LEASE_SECONDS = 60.0
SENSITIVE_QUESTION = re.compile(
    r"(?i)\b(api[ _-]?key|credential|password|secret|token|production|prod)\b"
)


class DeliveryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DeliveryResult:
    run_id: str
    status: str
    artifact_root: Path
    tracker_references: dict[str, str]
    integration_branch: str
    integration_commit: str
    tickets_completed: int
    review_rounds: int


@dataclass(frozen=True, slots=True)
class _ImplementedTicket:
    ticket: DeliveryTicket
    worktree: Worktree
    receipt: TicketReceipt


class _Record(Protocol):
    def __call__(self, event: str, details: dict[str, object]) -> None: ...


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


class _InspectAgent(Protocol):
    def __call__(
        self,
        workspace: Path,
        agent_name: str,
        harness: Harness,
    ) -> tuple[bool, DispatchOutcome | None]: ...


class _RevalidateReview(Protocol):
    def __call__(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        round_number: int,
        integration_commit: str,
    ) -> ReviewReport: ...


class DeliveryRecoveryMixin:
    config: WorkflowConfig
    controller: Harness
    worker_harnesses: tuple[Harness, ...]
    tracker: DeliveryTracker
    _run_root: Path
    _run_id: str
    _journal: DeliveryJournal | None
    _previous_state: dict[str, object]
    _record: _Record
    _select_worker: _SelectWorker
    _dispatch_with_proxy: _DispatchWithProxy
    _inspect_delivery_agent: _InspectAgent
    _delivery_agent_name: Callable[[Path, Harness, str], str]
    _repair_attempts: Callable[[], int]
    _repair_inflight: Callable[[], tuple[int, str] | None]
    _delivery_base_commit: Callable[[GitWorkspace], str]
    _revalidate_review: _RevalidateReview
    _revalidate_repair_history: Callable[[DeliveryPlan, Worktree, int, str], None]
    _repair_result_preconditions: Callable[[int], tuple[dict[str, object], dict[str, object]]]

    def _validate_completed_result(self, result: DeliveryResult) -> None:
        plan = load_delivery_plan(self._run_root / "delivery-plan.json")
        ticket_ids = {ticket.ticket_id for ticket in plan.tickets}
        expected_branch = f"ho/{plan.slug}/integration"
        if result.integration_branch != expected_branch:
            raise DeliveryError("delivery_result_integration_branch_mismatch")
        if result.tickets_completed != len(plan.tickets):
            raise DeliveryError("delivery_result_ticket_count_mismatch")
        if set(result.tracker_references) != ticket_ids or any(
            not isinstance(reference, str) or not reference.strip()
            for reference in result.tracker_references.values()
        ):
            raise DeliveryError("delivery_result_tracker_references_mismatch")
        repair_attempts = self._repair_attempts()
        if self._repair_inflight() is not None:
            raise DeliveryError("delivery_result_repair_inflight")
        if result.review_rounds != repair_attempts + 1:
            raise DeliveryError("delivery_result_review_rounds_invalid")
        git = GitWorkspace(
            self.config.workspace,
            self._run_root,
            plan.slug,
        )
        base_commit = self._delivery_base_commit(git)
        integration = Worktree(
            self._run_root / "worktrees" / "integration",
            expected_branch,
            base_commit,
        )
        _validate_worktree_ownership(
            git,
            self._run_root / "worktrees" / "integration",
            integration,
        )
        _validate_worktree_clean(git, integration.path)
        commit = git.head(integration)
        if commit != result.integration_commit:
            raise DeliveryError("delivery_result_integration_commit_mismatch")
        self._revalidate_result_prerequisites(result)

    def _publish_result(self, result: DeliveryResult) -> None:
        path = self._run_root / "result.json"
        self._revalidate_result_prerequisites(result)
        preconditions = self._result_preconditions(result)
        result_payload = {
            "run_id": result.run_id,
            "status": result.status,
            "tracker_references": result.tracker_references,
            "integration_branch": result.integration_branch,
            "integration_commit": result.integration_commit,
            "tickets_completed": result.tickets_completed,
            "review_rounds": result.review_rounds,
            "preconditions": preconditions,
        }
        file_payload = {
            "run_id": result.run_id,
            "status": result.status,
            "artifact_root": str(result.artifact_root),
            "tracker_references": result.tracker_references,
            "integration_branch": result.integration_branch,
            "integration_commit": result.integration_commit,
            "tickets_completed": result.tickets_completed,
            "review_rounds": result.review_rounds,
        }

        def publish() -> dict[str, object]:
            existing = _load_completed_result(path, result.run_id)
            if existing is None:
                _write_json(path, file_payload)
                existing = _load_completed_result(path, result.run_id)
            if existing != result:
                raise DeliveryError("delivery_recovery_conflict:result.publish")
            return {
                **result_payload,
                "result_sha256": _file_sha256(path),
            }

        payload = self._require_journal().reconcile(
            DeliveryEffect(
                key="result:publish",
                kind="result.publish",
                intent=result_payload,
                observe=partial(
                    self._observe_result,
                    result,
                    result_payload,
                    path,
                ),
                apply=publish,
            )
        )
        if payload != {**result_payload, "result_sha256": _file_sha256(path)}:
            raise DeliveryError("delivery_recovery_conflict:result.publish")

    def _revalidate_result_prerequisites(self, result: DeliveryResult) -> None:
        plan = load_delivery_plan(self._run_root / "delivery-plan.json")
        publication = {
            key: reference.reference for key, reference in self._publish_tracker(plan).items()
        }
        if publication != result.tracker_references:
            raise DeliveryError("delivery_result_tracker_publication_mismatch")
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        base_commit = self._delivery_base_commit(git)
        integration = Worktree(
            self._run_root / "worktrees/integration",
            result.integration_branch,
            base_commit,
        )
        _validate_worktree_ownership(git, integration.path, integration)
        _validate_worktree_clean(git, integration.path)
        if git.head(integration) != result.integration_commit:
            raise DeliveryError("delivery_recovery_conflict:git.integration")
        journal = self._require_journal()
        for ticket in plan.tickets:
            worktree_record = journal.require_confirmed(f"git:worktree:ticket:{ticket.ticket_id}")
            ticket_base = worktree_record.get("base_commit")
            ticket_branch = worktree_record.get("branch")
            acceptance_intent = journal._intent_details(f"ticket:accept:{ticket.ticket_id}")
            if (
                not isinstance(ticket_base, str)
                or not isinstance(ticket_branch, str)
                or acceptance_intent is None
            ):
                raise DeliveryError("delivery_recovery_conflict:receipt.ticket.accept")
            ticket_worktree = Worktree(
                self._run_root / "worktrees" / f"ticket-{ticket.ticket_id}",
                ticket_branch,
                ticket_base,
            )
            implemented = self._reconcile_ticket_acceptance(
                plan,
                ticket,
                None,
                ticket_worktree,
                intent=acceptance_intent,
            )
            self._merge_ticket(git, integration, implemented)
            self._close_ticket(plan, ticket, implemented.receipt)
        self._revalidate_repair_history(
            plan,
            integration,
            result.review_rounds,
            result.integration_commit,
        )

    def _result_preconditions(self, result: DeliveryResult) -> dict[str, object]:
        journal = self._require_journal()
        review = journal.require_confirmed(f"review:accept:{result.review_rounds}")
        if review.get("integration_commit") != result.integration_commit:
            raise DeliveryError("delivery_recovery_conflict:result.publish")
        plan = load_delivery_plan(self._run_root / "delivery-plan.json")
        tickets: dict[str, object] = {}
        for ticket in plan.tickets:
            acceptance = journal.require_confirmed(f"ticket:accept:{ticket.ticket_id}")
            merge = journal.require_confirmed(f"git:merge:{ticket.ticket_id}")
            close = journal.require_confirmed(f"tracker:close:{ticket.ticket_id}")
            receipt_commit = acceptance.get("commit")
            if (
                not isinstance(receipt_commit, str)
                or merge.get("ticket_commit") != receipt_commit
                or close.get("receipt_commit") != receipt_commit
                or close.get("integration_commit") != merge.get("integration_commit")
                or close.get("status") != "closed"
            ):
                raise DeliveryError("delivery_recovery_conflict:result.publish")
            tickets[ticket.ticket_id] = {
                "receipt_commit": receipt_commit,
                "merge_commit": merge.get("integration_commit"),
                "close_status": close.get("status"),
            }
        repairs, adjudications = self._repair_result_preconditions(result.review_rounds)
        return {
            "review_round": result.review_rounds,
            "review_commit": result.integration_commit,
            "tickets": tickets,
            "repairs": repairs,
            "adjudications": adjudications,
        }

    def _observe_result(
        self,
        result: DeliveryResult,
        result_payload: dict[str, object],
        path: Path,
        expected: dict[str, object] | None,
        started: bool,
    ) -> DeliveryEffectObservation:
        try:
            self._revalidate_result_prerequisites(result)
        except (DeliveryArtifactError, DeliveryError):
            return _effect_conflict()
        existing = _load_completed_result(path, result.run_id)
        if existing is None:
            return _effect_absent() if expected is None else _effect_conflict()
        if existing != result:
            return _effect_conflict()
        details = {**result_payload, "result_sha256": _file_sha256(path)}
        return _effect_matched(details)

    def _publish_tracker(self, plan: DeliveryPlan) -> dict[str, TrackerTicket]:
        if (
            self.config.standardized_delivery.tracker_backend.value == "github"
            and contains_high_confidence_secret(plan)
        ):
            raise DeliveryError(
                "delivery_secret_material_rejected: remove secret material before retry"
            )
        path = self._run_root / "tracker-publication.json"
        journal = self._journal
        try:
            observation_receipts = existing_ticket_receipts(
                self._run_root,
                plan,
                confirmed=None if journal is None else journal.has_confirmation,
            )
        except DeliveryArtifactError as exc:
            raise DeliveryError("delivery_recovery_conflict:receipt.ticket.accept") from exc
        if path.is_file():
            references = self._restore_tracker_publication(path, plan)
            if journal is not None:
                recorded = journal._intent_details("tracker:publish")
                spec_url = getattr(self.tracker, "spec_url", None)
                migration_receipts: dict[str, TicketReceipt] | None = None
                if recorded is None:
                    markers = tracker_markers(self._run_id, plan)
                    migration_receipts = self._preflight_legacy_delivery(
                        plan,
                        references,
                        spec_url,
                        markers,
                    )
                    observation_receipts = migration_receipts
                    recorded = {
                        "markers": markers.payload(),
                        "migration": legacy_migration_payload(
                            self._run_root,
                            references,
                            spec_url,
                            migration_receipts,
                            file_sha256=_file_sha256,
                        ),
                    }
                else:
                    markers = tracker_markers_from_payload(recorded.get("markers"), plan)
                migration = recorded.get("migration")
                migration_completed = completed_legacy_migration(migration)
                if migration is not None:
                    migration_receipts = self._preflight_legacy_delivery(
                        plan,
                        references,
                        spec_url,
                        markers,
                        require_closed=migration_completed,
                    )
                    observation_receipts = migration_receipts
                    if migration != legacy_migration_payload(
                        self._run_root,
                        references,
                        spec_url,
                        migration_receipts,
                        file_sha256=_file_sha256,
                        completed=migration_completed,
                    ):
                        raise DeliveryError("delivery_recovery_conflict:tracker.publish")
                apply = (
                    partial(
                        self._adopt_tracker_effect,
                        plan,
                        markers,
                        references,
                        spec_url,
                        migration_receipts,
                        migration_completed,
                    )
                    if migration is not None
                    else partial(self._publish_tracker_effect, plan, markers)
                )
                payload = journal.reconcile(
                    DeliveryEffect(
                        key="tracker:publish",
                        kind="tracker.publish",
                        intent=recorded,
                        observe=partial(
                            self._observe_tracker_publication,
                            plan,
                            markers,
                            observation_receipts,
                        ),
                        apply=apply,
                    )
                )
                observed, observed_spec = self._tracker_references_from_effect(payload, plan)
                if observed != references or observed_spec != spec_url:
                    raise DeliveryError("delivery_tracker_publication_mismatch")
                references = observed
            self._record("tracker_recovered", {"tickets": sorted(references)})
            return references
        journal = self._journal
        if journal is None:
            payload = self._publish_tracker_effect(plan, None)
        else:
            if self._tracker_publish_was_interrupted() and not journal.has_intent(
                "tracker:publish"
            ):
                raise DeliveryError("delivery_tracker_publish_interrupted")
            recorded = journal._intent_details("tracker:publish")
            markers = (
                tracker_markers(self._run_id, plan)
                if recorded is None
                else tracker_markers_from_payload(recorded.get("markers"), plan)
            )
            payload = journal.reconcile(
                DeliveryEffect(
                    key="tracker:publish",
                    kind="tracker.publish",
                    intent={"markers": markers.payload()},
                    observe=partial(
                        self._observe_tracker_publication,
                        plan,
                        markers,
                        observation_receipts,
                    ),
                    apply=partial(self._publish_tracker_effect, plan, markers),
                )
            )
        references, spec_url = self._tracker_references_from_effect(payload, plan)
        _write_json(
            path,
            {
                "backend": self.config.standardized_delivery.tracker_backend.value,
                "spec_url": spec_url,
                "tickets": {key: reference.reference for key, reference in references.items()},
            },
        )
        return references

    def _observe_tracker_publication(
        self,
        plan: DeliveryPlan,
        markers: TrackerMarkers,
        receipts: dict[str, TicketReceipt],
        expected: dict[str, object] | None,
        started: bool,
    ) -> DeliveryEffectObservation:
        try:
            observed = self.tracker.observe_publication(
                plan,
                markers=markers,
                receipts=receipts,
            )
        except TrackerError:
            return _effect_conflict()
        if observed is None:
            return _effect_absent()
        references, spec_url = observed
        details: dict[str, object] = {
            "backend": self.config.standardized_delivery.tracker_backend.value,
            "spec_url": spec_url,
            "tickets": {key: value.reference for key, value in references.items()},
        }
        return _effect_matched(details)

    def _publish_tracker_effect(
        self,
        plan: DeliveryPlan,
        markers: TrackerMarkers | None,
    ) -> dict[str, object]:
        references = self.tracker.publish(plan, markers=markers)
        expected = {ticket.ticket_id for ticket in plan.tickets}
        if set(references) != expected or any(
            not isinstance(reference.reference, str) or not reference.reference.strip()
            for reference in references.values()
        ):
            raise DeliveryError("delivery_tracker_references_invalid")
        spec_url = getattr(self.tracker, "spec_url", None)
        if spec_url is not None and (not isinstance(spec_url, str) or not spec_url.strip()):
            raise DeliveryError("delivery_tracker_publication_invalid")
        return {
            "backend": self.config.standardized_delivery.tracker_backend.value,
            "spec_url": spec_url,
            "tickets": {key: reference.reference for key, reference in references.items()},
        }

    def _adopt_tracker_effect(
        self,
        plan: DeliveryPlan,
        markers: TrackerMarkers,
        references: dict[str, TrackerTicket],
        spec_url: str | None,
        receipts: dict[str, TicketReceipt] | None,
        require_closed: bool,
    ) -> dict[str, object]:
        adopted = self.tracker.adopt(
            plan,
            references=references,
            spec_url=spec_url,
            markers=markers,
            receipts=receipts,
            require_closed=require_closed,
        )
        if adopted != references:
            raise DeliveryError("delivery_tracker_adoption_conflict")
        return {
            "backend": self.config.standardized_delivery.tracker_backend.value,
            "spec_url": spec_url,
            "tickets": {key: value.reference for key, value in adopted.items()},
        }

    def _preflight_legacy_delivery(
        self,
        plan: DeliveryPlan,
        references: dict[str, TrackerTicket],
        spec_url: str | None,
        markers: TrackerMarkers,
        *,
        require_closed: bool = False,
    ) -> dict[str, TicketReceipt]:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        base_commit = self._delivery_base_commit(git)
        integration_path = self._run_root / "worktrees/integration"
        integration = Worktree(
            integration_path,
            f"ho/{plan.slug}/integration",
            base_commit,
        )
        integration_head: str | None = None
        if integration_path.exists():
            _validate_worktree_ownership(git, integration_path, integration)
            _validate_worktree_clean(git, integration_path)
            integration_head = git.head(integration)
            if not git.is_ancestor(integration_path, base_commit, integration_head):
                raise DeliveryError("delivery_recovery_conflict:git.worktree.create")
        receipts: dict[str, TicketReceipt] = {}
        for ticket in plan.tickets:
            receipt_path = self._run_root / "receipts" / f"ticket-{ticket.ticket_id}.json"
            worktree_path = self._run_root / "worktrees" / f"ticket-{ticket.ticket_id}"
            if not receipt_path.is_file() and not worktree_path.exists():
                continue
            ticket_worktree = Worktree(
                worktree_path,
                f"ho/{plan.slug}/ticket-{ticket.ticket_id}",
                base_commit,
            )
            if not worktree_path.exists():
                raise DeliveryError("delivery_recovery_conflict:receipt.ticket.accept")
            _validate_worktree_ownership(git, worktree_path, ticket_worktree)
            _validate_worktree_clean(git, worktree_path)
            for harness in self.worker_harnesses:
                self._preflight_legacy_agent(
                    worktree_path,
                    harness,
                    f"impl-{ticket.ticket_id}",
                )
            if not receipt_path.is_file():
                if git.head(ticket_worktree) != base_commit:
                    raise DeliveryError("delivery_recovery_conflict:receipt.ticket.accept")
                continue
            try:
                receipt = load_ticket_receipt(receipt_path, ticket)
            except DeliveryArtifactError as exc:
                raise DeliveryError("delivery_recovery_conflict:receipt.ticket.accept") from exc
            if (
                git.head(ticket_worktree) != receipt.commit
                or not git.is_ancestor(worktree_path, base_commit, receipt.commit)
                or integration_head is None
            ):
                raise DeliveryError("delivery_recovery_conflict:receipt.ticket.accept")
            if git.is_ancestor(integration_path, receipt.commit, integration_head):
                merged = git.find_merge(integration, receipt.commit)
                if merged is None:
                    raise DeliveryError("delivery_recovery_conflict:git.integration.merge")
            receipts[ticket.ticket_id] = receipt
        self._preflight_legacy_agent(
            self.config.workspace,
            self.controller,
            "plan",
        )
        inspect = getattr(self.tracker, "inspect_adoption", None)
        if not callable(inspect):
            raise DeliveryError("delivery_tracker_adoption_uninspectable")
        inspect(
            plan,
            references=references,
            spec_url=spec_url,
            markers=markers,
            receipts=receipts,
            require_closed=require_closed,
        )
        return receipts

    def _preflight_legacy_agent(
        self,
        workspace: Path,
        harness: Harness,
        role: str,
    ) -> None:
        agent_name = self._delivery_agent_name(workspace, harness, role)
        try:
            supported, inspected = self._inspect_delivery_agent(
                workspace,
                agent_name,
                harness,
            )
        except TransportError as exc:
            raise DeliveryError("delivery_recovery_conflict:agent.dispatch") from exc
        if supported and _agent_is_active(inspected):
            raise DeliveryError("delivery_recovery_conflict:agent.dispatch")

    def _tracker_references_from_effect(
        self,
        payload: dict[str, object],
        plan: DeliveryPlan,
    ) -> tuple[dict[str, TrackerTicket], str | None]:
        tickets = payload.get("tickets")
        spec_url = payload.get("spec_url")
        expected = {ticket.ticket_id for ticket in plan.tickets}
        if (
            set(payload) != {"backend", "spec_url", "tickets"}
            or payload.get("backend") != self.config.standardized_delivery.tracker_backend.value
            or not isinstance(tickets, dict)
            or set(tickets) != expected
            or any(
                not isinstance(reference, str) or not reference.strip()
                for reference in tickets.values()
            )
            or (spec_url is not None and (not isinstance(spec_url, str) or not spec_url.strip()))
        ):
            raise DeliveryError("delivery_tracker_publication_invalid")
        references = {
            ticket_id: TrackerTicket(ticket_id, reference)
            for ticket_id, reference in tickets.items()
            if isinstance(ticket_id, str) and isinstance(reference, str)
        }
        if len(references) != len(tickets):
            raise DeliveryError("delivery_tracker_publication_invalid")
        self.tracker.references = references
        if hasattr(self.tracker, "spec_url"):
            self.tracker.spec_url = spec_url
        return references, spec_url

    def _restore_tracker_publication(
        self,
        path: Path,
        plan: DeliveryPlan,
    ) -> dict[str, TrackerTicket]:
        _safe_delivery_path(path, root=self._run_root)
        try:
            spec_url, tickets = load_tracker_publication(
                path,
                backend=self.config.standardized_delivery.tracker_backend.value,
                ticket_ids={ticket.ticket_id for ticket in plan.tickets},
            )
        except DeliveryArtifactError as exc:
            raise DeliveryError("delivery_tracker_publication_invalid") from exc
        if not hasattr(self.tracker, "references"):
            raise DeliveryError("delivery_tracker_publication_unrestorable")
        references = {key: TrackerTicket(key, value) for key, value in tickets.items()}
        self.tracker.references = references
        if hasattr(self.tracker, "spec_url"):
            if spec_url is None:
                raise DeliveryError("delivery_tracker_publication_invalid")
            self.tracker.spec_url = spec_url
        return references

    def _tracker_publish_was_interrupted(self) -> bool:
        if self.config.standardized_delivery.tracker_backend.value != "github":
            return False
        return any(
            self._previous_state.get(key) == "tracker-publish" for key in ("stage", "failed_stage")
        )

    def _implement_plan(self, plan: DeliveryPlan) -> tuple[Worktree, int]:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        base_commit = self._delivery_base_commit(git)
        integration = self._worktree_effect(
            git,
            operation_key="git:worktree:integration",
            kind="integration",
            base_commit=base_commit,
        )
        _validate_worktree_ownership(
            git,
            self._run_root / "worktrees" / "integration",
            integration,
        )
        _validate_worktree_clean(git, integration.path)
        if not _git_succeeds(
            git,
            integration.path,
            "merge-base",
            "--is-ancestor",
            base_commit,
            git.head(integration),
        ):
            raise DeliveryError("delivery_integration_base_mismatch")
        completed = self._recover_integrated_tickets(git, plan, integration)
        tickets = {ticket.ticket_id: ticket for ticket in plan.tickets}
        completed = {
            ticket_id
            for ticket_id in completed
            if set(tickets[ticket_id].blocked_by).issubset(completed)
        }
        for ticket_id in sorted(completed):
            receipt = load_ticket_receipt(
                self._run_root / "receipts" / f"ticket-{ticket_id}.json",
                tickets[ticket_id],
            )
            self._close_ticket(plan, tickets[ticket_id], receipt)
            self._record(
                "ticket_recovered",
                {"ticket_id": ticket_id, "commit": receipt.commit},
            )
        while len(completed) < len(tickets):
            frontier = [
                ticket
                for ticket in plan.tickets
                if ticket.ticket_id not in completed and set(ticket.blocked_by).issubset(completed)
            ]
            if not frontier:
                raise DeliveryError("ticket_dag_stalled")
            selected = frontier[: self.config.standardized_delivery.max_parallel]
            integration_head = git.head(integration)
            routed: list[tuple[DeliveryTicket, Harness]] = [
                (
                    ticket,
                    self._select_worker(
                        f"Implement ticket {ticket.ticket_id}: {ticket.title}",
                        ticket.what_to_build,
                        f"{plan.slug}:ticket:{ticket.ticket_id}",
                    ),
                )
                for ticket in selected
            ]
            prepared = [
                (
                    ticket,
                    harness,
                    self._worktree_effect(
                        git,
                        operation_key=f"git:worktree:ticket:{ticket.ticket_id}",
                        kind="ticket",
                        base_commit=integration_head,
                        ticket_id=ticket.ticket_id,
                    ),
                )
                for ticket, harness in routed
            ]
            for ticket, _, worktree in prepared:
                _validate_worktree_ownership(
                    git,
                    self._run_root / "worktrees" / f"ticket-{ticket.ticket_id}",
                    worktree,
                )
                _validate_worktree_clean(git, worktree.path)
            implemented: list[_ImplementedTicket] = []
            with ThreadPoolExecutor(
                max_workers=len(routed),
                thread_name_prefix="delivery-ticket",
            ) as executor:
                futures = {
                    executor.submit(
                        self._implement_ticket,
                        plan,
                        ticket,
                        harness,
                        worktree,
                    ): ticket
                    for ticket, harness, worktree in prepared
                }
                for future in as_completed(futures):
                    implemented.append(future.result())
            implemented.sort(key=lambda item: item.ticket.ticket_id)
            for result in implemented:
                _validate_worktree_ownership(
                    git,
                    self._run_root / "worktrees" / f"ticket-{result.ticket.ticket_id}",
                    result.worktree,
                )
                self._merge_ticket(git, integration, result)
                self._close_ticket(plan, result.ticket, result.receipt)
                completed.add(result.ticket.ticket_id)
                self._record(
                    "ticket_completed",
                    {
                        "ticket_id": result.ticket.ticket_id,
                        "title": result.ticket.title,
                        "commit": result.receipt.commit,
                    },
                )
        return integration, len(completed)

    def _worktree_effect(
        self,
        git: GitWorkspace,
        *,
        operation_key: str,
        kind: str,
        base_commit: str,
        ticket_id: str | None = None,
    ) -> Worktree:
        journal = self._require_journal()
        recorded = journal._intent_details(operation_key)
        if recorded is not None:
            recorded_base = recorded.get("base_commit")
            if not isinstance(recorded_base, str):
                raise DeliveryError("delivery_journal_invalid")
            base_commit = recorded_base
        branch = (
            f"ho/{git.slug}/integration"
            if kind == "integration"
            else f"ho/{git.slug}/ticket-{ticket_id}"
        )

        def create() -> dict[str, object]:
            worktree = (
                git.create_integration(base_commit)
                if kind == "integration"
                else git.create_ticket(str(ticket_id), base_commit=base_commit)
            )
            return {"branch": worktree.branch, "base_commit": worktree.base_commit}

        def observe(
            expected: dict[str, object] | None,
            started: bool,
        ) -> DeliveryEffectObservation:
            try:
                worktree = git.observe_worktree(
                    (
                        self._run_root / "worktrees" / "integration"
                        if kind == "integration"
                        else self._run_root / "worktrees" / f"ticket-{ticket_id}"
                    ),
                    branch,
                    base_commit,
                    require_base_head=(expected is None and not self._is_legacy_migration()),
                )
            except GitWorkspaceError:
                return _effect_conflict()
            if worktree is None:
                return _effect_absent()
            return _effect_matched({"branch": worktree.branch, "base_commit": worktree.base_commit})

        payload = journal.reconcile(
            DeliveryEffect(
                key=operation_key,
                kind="git.worktree.create",
                intent={"kind": kind, "branch": branch, "base_commit": base_commit},
                observe=observe,
                apply=create,
            )
        )
        if payload != {"branch": branch, "base_commit": base_commit}:
            raise DeliveryError("delivery_worktree_confirmation_invalid")
        path = (
            self._run_root / "worktrees" / "integration"
            if kind == "integration"
            else self._run_root / "worktrees" / f"ticket-{ticket_id}"
        )
        return Worktree(path, branch, base_commit)

    def _merge_ticket(
        self,
        git: GitWorkspace,
        integration: Worktree,
        result: _ImplementedTicket,
    ) -> str:
        journal = self._require_journal()
        operation_key = f"git:merge:{result.ticket.ticket_id}"
        recorded = journal._intent_details(operation_key)
        if recorded is None and not self._is_legacy_migration():
            self._assert_integration_frontier(git, integration)
        migrated_merge = (
            git.find_merge(integration, result.receipt.commit)
            if recorded is None and self._is_legacy_migration()
            else None
        )
        parent = (
            recorded.get("integration_parent")
            if recorded is not None
            else (migrated_merge[0] if migrated_merge is not None else git.head(integration))
        )
        if not isinstance(parent, str):
            raise DeliveryError("delivery_journal_invalid")

        def merge() -> dict[str, object]:
            current = git.head(integration)
            if current == parent:
                current = git.merge(integration, result.worktree)
            parents = git.parents(integration.path, current)
            if set(parents) != {parent, result.receipt.commit} or len(parents) != 2:
                raise DeliveryError("delivery_recovery_conflict:git.integration.merge")
            return {
                "ticket_commit": result.receipt.commit,
                "integration_parent": parent,
                "integration_commit": current,
            }

        def observe(
            expected: dict[str, object] | None,
            started: bool,
        ) -> DeliveryEffectObservation:
            if expected is not None:
                confirmed_commit = expected.get("integration_commit")
                if not isinstance(confirmed_commit, str):
                    return _effect_conflict()
                parents = git.parents(integration.path, confirmed_commit)
                if (
                    set(parents) != {parent, result.receipt.commit}
                    or len(parents) != 2
                    or not git.is_ancestor(
                        integration.path,
                        confirmed_commit,
                        git.head(integration),
                    )
                ):
                    return _effect_conflict()
                return _effect_matched(expected)
            if migrated_merge is not None:
                migrated_parent, migrated_commit = migrated_merge
                if migrated_parent != parent or not git.is_ancestor(
                    integration.path,
                    migrated_commit,
                    git.head(integration),
                ):
                    return _effect_conflict()
                return _effect_matched(
                    {
                        "ticket_commit": result.receipt.commit,
                        "integration_parent": migrated_parent,
                        "integration_commit": migrated_commit,
                    }
                )
            try:
                observed = git.observe_merge(
                    integration,
                    parent=parent,
                    ticket_commit=result.receipt.commit,
                )
            except GitWorkspaceError:
                return _effect_conflict()
            if observed is None:
                return _effect_absent()
            return _effect_matched(
                {
                    "ticket_commit": result.receipt.commit,
                    "integration_parent": parent,
                    "integration_commit": observed,
                }
            )

        payload = journal.reconcile(
            DeliveryEffect(
                key=operation_key,
                kind="git.integration.merge",
                intent={
                    "ticket_id": result.ticket.ticket_id,
                    "ticket_commit": result.receipt.commit,
                    "integration_parent": parent,
                },
                observe=observe,
                apply=merge,
            )
        )
        commit = payload.get("integration_commit")
        if not isinstance(commit, str):
            raise DeliveryError("delivery_merge_confirmation_invalid")
        return commit

    def _close_ticket(
        self,
        plan: DeliveryPlan,
        ticket: DeliveryTicket,
        receipt: TicketReceipt,
    ) -> None:
        journal = self._require_journal()
        journal.require_confirmed(f"ticket:accept:{ticket.ticket_id}")
        merge = journal.require_confirmed(f"git:merge:{ticket.ticket_id}")
        integration_commit = merge.get("integration_commit")
        if not isinstance(integration_commit, str):
            raise DeliveryError("delivery_merge_confirmation_invalid")
        marker = self._tracker_markers(plan).ticket(ticket.ticket_id)
        close_result: dict[str, object] = {
            "ticket_id": ticket.ticket_id,
            "receipt_commit": receipt.commit,
            "integration_commit": integration_commit,
            "status": "closed",
        }

        def close() -> dict[str, object]:
            self.tracker.close(ticket, receipt, marker=marker)
            return close_result

        def observe(
            expected: dict[str, object] | None,
            started: bool,
        ) -> DeliveryEffectObservation:
            try:
                closed = self.tracker.observe_close(ticket, receipt, marker=marker)
            except TrackerError:
                return _effect_conflict()
            return _effect_matched(close_result) if closed else _effect_absent()

        journal.reconcile(
            DeliveryEffect(
                key=f"tracker:close:{ticket.ticket_id}",
                kind="tracker.close",
                intent={
                    "ticket_id": ticket.ticket_id,
                    "receipt_commit": receipt.commit,
                    "integration_commit": integration_commit,
                    "marker": marker,
                },
                observe=observe,
                apply=close,
            )
        )

    def _tracker_markers(self, plan: DeliveryPlan) -> TrackerMarkers:
        intent = self._require_journal()._intent_details("tracker:publish")
        if intent is None:
            raise DeliveryError("delivery_tracker_marker_missing")
        return tracker_markers_from_payload(intent.get("markers"), plan)

    def _is_legacy_migration(self) -> bool:
        intent = self._require_journal()._intent_details("tracker:publish")
        return intent is not None and isinstance(intent.get("migration"), dict)

    def _assert_integration_frontier(
        self,
        git: GitWorkspace,
        integration: Worktree,
    ) -> str:
        journal = self._require_journal()
        latest = journal.latest_confirmation(
            kinds=frozenset({"git.integration.merge", "repair.commit"})
        )
        if latest is None:
            creation = journal.require_confirmed("git:worktree:integration")
            expected = creation.get("base_commit")
        else:
            expected = latest.details.get(
                "integration_commit" if latest.kind == "git.integration.merge" else "commit"
            )
        if not isinstance(expected, str) or git.head(integration) != expected:
            raise DeliveryError("delivery_recovery_conflict:git.integration")
        return expected

    def _recover_integrated_tickets(
        self,
        git: GitWorkspace,
        plan: DeliveryPlan,
        integration: Worktree,
    ) -> set[str]:
        integration_head = git.head(integration)
        completed: set[str] = set()
        for ticket in plan.tickets:
            receipt_file = self._run_root / "receipts" / f"ticket-{ticket.ticket_id}.json"
            if not receipt_file.is_file():
                continue
            receipt = load_ticket_receipt(receipt_file, ticket)
            journal = self._require_journal()
            worktree_key = f"git:worktree:ticket:{ticket.ticket_id}"
            if not journal.has_intent(worktree_key) and self._is_legacy_migration():
                self._worktree_effect(
                    git,
                    operation_key=worktree_key,
                    kind="ticket",
                    base_commit=integration.base_commit,
                    ticket_id=ticket.ticket_id,
                )
            worktree_record = journal.require_confirmed(worktree_key)
            ticket_base = worktree_record.get("base_commit")
            ticket_branch = worktree_record.get("branch")
            if not isinstance(ticket_base, str) or not isinstance(ticket_branch, str):
                raise DeliveryError("delivery_worktree_confirmation_invalid")
            worktree = Worktree(
                self._run_root / "worktrees" / f"ticket-{ticket.ticket_id}",
                ticket_branch,
                ticket_base,
            )
            _validate_worktree_ownership(
                git,
                worktree.path,
                worktree,
            )
            _validate_worktree_clean(git, worktree.path)
            if git.head(worktree) != receipt.commit:
                raise DeliveryError(f"ticket_receipt_commit_mismatch: {ticket.ticket_id}")
            if not git.is_ancestor(worktree.path, ticket_base, receipt.commit):
                raise DeliveryError(f"ticket_commit_diverged: {ticket.ticket_id}")
            acceptance_key = f"ticket:accept:{ticket.ticket_id}"
            acceptance_intent = journal._intent_details(acceptance_key) or {
                "ticket_id": ticket.ticket_id,
                "harness": "legacy",
                "branch": ticket_branch,
                "base_commit": ticket_base,
            }
            self._reconcile_ticket_acceptance(
                plan,
                ticket,
                None,
                worktree,
                intent=acceptance_intent,
            )
            if _git_succeeds(
                git,
                integration.path,
                "merge-base",
                "--is-ancestor",
                receipt.commit,
                integration_head,
            ):
                self._merge_ticket(
                    git,
                    integration,
                    _ImplementedTicket(ticket, worktree, receipt),
                )
                completed.add(ticket.ticket_id)
        return completed

    def _implement_ticket(
        self,
        plan: DeliveryPlan,
        ticket: DeliveryTicket,
        harness: Harness,
        worktree: Worktree,
    ) -> _ImplementedTicket:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        _validate_worktree_ownership(
            git,
            self._run_root / "worktrees" / f"ticket-{ticket.ticket_id}",
            worktree,
        )
        _validate_worktree_clean(git, worktree.path)
        intent: dict[str, object] = {
            "ticket_id": ticket.ticket_id,
            "harness": harness.value,
            "branch": worktree.branch,
            "base_commit": worktree.base_commit,
            "agent_name": self._delivery_agent_name(
                worktree.path,
                harness,
                f"impl-{ticket.ticket_id}",
            ),
        }
        return self._reconcile_ticket_acceptance(
            plan,
            ticket,
            harness,
            worktree,
            intent=intent,
        )

    def _reconcile_ticket_acceptance(
        self,
        plan: DeliveryPlan,
        ticket: DeliveryTicket,
        harness: Harness | None,
        worktree: Worktree,
        *,
        intent: dict[str, object],
    ) -> _ImplementedTicket:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        receipt_file = self._run_root / "receipts" / f"ticket-{ticket.ticket_id}.json"
        _safe_delivery_path(receipt_file, root=self._run_root)
        receipt_file.parent.mkdir(parents=True, exist_ok=True)

        def accept() -> dict[str, object]:
            if not receipt_file.is_file():
                if harness is None:
                    raise DeliveryError(
                        f"delivery_recovery_conflict:receipt.ticket.accept:{ticket.ticket_id}"
                    )
                profile = profile_for_harness(self.config.profiles, harness)
                prompt = execution_prompt(
                    profile,
                    implementation_prompt(plan, ticket, receipt_file),
                )
                for attempt in range(ARTIFACT_PROMPT_ATTEMPTS):
                    outcome = self._dispatch_with_proxy(
                        worktree.path,
                        harness,
                        prompt,
                        role=f"impl-{ticket.ticket_id}",
                    )
                    _require_success(outcome, f"ticket_{ticket.ticket_id}")
                    _validate_worktree_ownership(
                        git,
                        self._run_root / "worktrees" / f"ticket-{ticket.ticket_id}",
                        worktree,
                    )
                    if receipt_file.is_file():
                        break
                    self._record(
                        "artifact_prompt_retried",
                        {
                            "role": f"impl-{ticket.ticket_id}",
                            "attempt": attempt + 1,
                        },
                    )
            receipt = load_ticket_receipt(receipt_file, ticket)
            commit = git.validate_commit(worktree)
            if receipt.commit != commit:
                raise DeliveryError(f"ticket_receipt_commit_mismatch: {ticket.ticket_id}")
            return {
                "ticket_id": ticket.ticket_id,
                "commit": commit,
                "receipt_sha256": _file_sha256(receipt_file),
            }

        def observe(
            expected: dict[str, object] | None,
            started: bool,
        ) -> DeliveryEffectObservation:
            if not receipt_file.is_file():
                if git.head(worktree) != worktree.base_commit:
                    return _effect_conflict()
                if started and harness is not None:
                    agent_name = intent.get("agent_name")
                    if not isinstance(agent_name, str):
                        return _effect_conflict()
                    try:
                        _, inspected = self._inspect_delivery_agent(
                            worktree.path,
                            agent_name,
                            harness,
                        )
                    except TransportError:
                        return _effect_conflict()
                    if _agent_is_active(inspected):
                        return _effect_conflict()
                return _effect_absent()
            try:
                observed_receipt = load_ticket_receipt(receipt_file, ticket)
                observed_commit = git.validate_commit(worktree)
            except (DeliveryArtifactError, GitWorkspaceError):
                return _effect_conflict()
            if observed_receipt.commit != observed_commit:
                return _effect_conflict()
            return _effect_matched(
                {
                    "ticket_id": ticket.ticket_id,
                    "commit": observed_commit,
                    "receipt_sha256": _file_sha256(receipt_file),
                }
            )

        payload = self._require_journal().reconcile(
            DeliveryEffect(
                key=f"ticket:accept:{ticket.ticket_id}",
                kind="receipt.ticket.accept",
                intent=intent,
                observe=observe,
                apply=accept,
            )
        )
        receipt = load_ticket_receipt(receipt_file, ticket)
        commit = git.validate_commit(worktree)
        if receipt.commit != commit or payload != {
            "ticket_id": ticket.ticket_id,
            "commit": commit,
            "receipt_sha256": _file_sha256(receipt_file),
        }:
            raise DeliveryError(f"ticket_receipt_commit_mismatch: {ticket.ticket_id}")
        return _ImplementedTicket(ticket, worktree, receipt)

    def _require_journal(self) -> DeliveryJournal:
        if self._journal is None:
            raise DeliveryError("delivery_journal_unavailable")
        return self._journal


def _finite_number(value: object) -> bool:
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _finding_map(report: ReviewReport) -> dict[str, ReviewFinding]:
    findings: dict[str, ReviewFinding] = {}
    for axis, rows in (("standards", report.standards), ("spec", report.spec)):
        for index, finding in enumerate(rows, 1):
            findings[f"{axis}:{index}"] = finding
    return findings


def _journal_payload(value: dict[str, object]) -> dict[str, object]:
    if contains_high_confidence_secret(value):
        raise DeliveryError("delivery_journal_sensitive_value")
    try:
        serialized = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            allow_nan=False,
        )
        if len(serialized.encode("utf-8")) > 64 * 1024:
            raise DeliveryError("delivery_journal_payload_too_large")
        payload = json.loads(serialized)
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DeliveryError("delivery_journal_payload_invalid") from exc
    if not isinstance(payload, dict):
        raise DeliveryError("delivery_journal_payload_invalid")
    return payload


def _file_sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise DeliveryError("delivery_artifact_unreadable") from exc


def _effect_absent() -> DeliveryEffectObservation:
    return DeliveryEffectObservation(DeliveryEffectState.ABSENT)


def _effect_matched(details: dict[str, object]) -> DeliveryEffectObservation:
    return DeliveryEffectObservation(DeliveryEffectState.MATCHED, details)


def _effect_conflict() -> DeliveryEffectObservation:
    return DeliveryEffectObservation(DeliveryEffectState.CONFLICT)


def _agent_is_active(outcome: DispatchOutcome | None) -> bool:
    return outcome is not None and outcome.state in {
        AgentState.WORKING,
        AgentState.BLOCKED,
        AgentState.UNKNOWN,
    }


def _require_success(outcome: DispatchOutcome, role: str) -> None:
    if outcome.error_code is not None or outcome.state not in {
        AgentState.IDLE,
        AgentState.DONE,
    }:
        raise DeliveryError(
            f"delivery_dispatch_failed:{role}:" f"{outcome.error_code or outcome.state.value}"
        )


def _safe_delivery_path(path: Path, *, root: Path | None = None) -> Path:
    try:
        return validate_artifact_path(path, root=root)
    except DeliveryArtifactError as exc:
        raise DeliveryError("delivery_artifact_path_invalid") from exc


def _validate_worktree_ownership(
    git: GitWorkspace,
    expected_path: Path,
    worktree: Worktree,
) -> None:
    try:
        git.validate_ownership(expected_path, worktree)
    except GitWorkspaceError as exc:
        raise DeliveryError(str(exc)) from exc


def _validate_worktree_clean(git: GitWorkspace, path: Path) -> None:
    try:
        git.validate_clean(path)
    except GitWorkspaceError as exc:
        raise DeliveryError(str(exc)) from exc


def _git_output(git: GitWorkspace, cwd: Path, *args: str) -> str:
    try:
        return git.output(cwd, *args)
    except GitWorkspaceError as exc:
        raise DeliveryError(str(exc)) from exc


def _git_succeeds(git: GitWorkspace, cwd: Path, *args: str) -> bool:
    try:
        return git.succeeds(cwd, *args)
    except GitWorkspaceError as exc:
        raise DeliveryError("delivery_git_query_failed") from exc


def _write_json(path: Path, payload: dict[str, object]) -> None:
    write_artifact_text(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        error_type=DeliveryError,
    )


def _load_completed_result(path: Path, run_id: str) -> DeliveryResult | None:
    _safe_delivery_path(path)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DeliveryError("delivery_result_invalid_json") from exc
    expected = {
        "run_id",
        "status",
        "artifact_root",
        "tracker_references",
        "integration_branch",
        "integration_commit",
        "tickets_completed",
        "review_rounds",
    }
    if not isinstance(payload, dict) or set(payload) != expected:
        raise DeliveryError("delivery_result_invalid_shape")
    references = payload["tracker_references"]
    artifact_root = payload["artifact_root"]
    if (
        payload["run_id"] != run_id
        or payload["status"] != "succeeded"
        or not isinstance(references, dict)
        or not all(
            isinstance(key, str) and isinstance(value, str) for key, value in references.items()
        )
        or not isinstance(artifact_root, str)
        or Path(artifact_root).resolve() != path.parent.resolve()
        or not isinstance(payload["integration_branch"], str)
        or not isinstance(payload["integration_commit"], str)
        or not isinstance(payload["tickets_completed"], int)
        or isinstance(payload["tickets_completed"], bool)
        or not isinstance(payload["review_rounds"], int)
        or isinstance(payload["review_rounds"], bool)
    ):
        raise DeliveryError("delivery_result_invalid")
    return DeliveryResult(
        run_id=run_id,
        status="succeeded",
        artifact_root=Path(artifact_root),
        tracker_references=dict(references),
        integration_branch=payload["integration_branch"],
        integration_commit=payload["integration_commit"],
        tickets_completed=payload["tickets_completed"],
        review_rounds=payload["review_rounds"],
    )
