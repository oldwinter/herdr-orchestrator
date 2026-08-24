from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.catalog import (
    execution_prompt,
    profile_for_harness,
    render_compact_catalog,
)
from herdr_orchestrator.delivery_prompts import (
    implementation_prompt,
    plan_prompt,
    principal_proxy_prompt,
    repair_prompt,
    review_verdict_prompt,
    spec_review_prompt,
    standards_review_prompt,
    wayfinder_chart_prompt,
    wayfinder_resolve_prompt,
    wayfinder_route_prompt,
)
from herdr_orchestrator.delivery_protocol import (
    AuthorityCategory,
    DecisionTicket,
    DeliveryPlan,
    DeliveryTicket,
    FindingSeverity,
    ProxyAction,
    ReviewFinding,
    ReviewReport,
    TicketReceipt,
    WayfinderMap,
    load_delivery_plan,
    load_proxy_decision,
    load_review_axis,
    load_review_verdict,
    load_ticket_receipt,
    load_wayfinder_map,
    load_wayfinder_resolution,
    load_wayfinder_route,
)
from herdr_orchestrator.git_workspace import GitWorkspace, Worktree
from herdr_orchestrator.herdr import HerdrTransport
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    HarnessProfile,
    WayfinderMode,
    WorkflowConfig,
)
from herdr_orchestrator.planner import (
    PlannerOutputError,
    load_worker_selection,
    worker_selection_prompt,
)
from herdr_orchestrator.protocol import TransportError
from herdr_orchestrator.selection import (
    effective_worker_harnesses,
    select_controller_harness,
)
from herdr_orchestrator.tracker import DeliveryTracker, tracker_from_config

MAX_WAYFINDER_DECISIONS = 100
MAX_PROXY_ROUNDS = 8
ARTIFACT_PROMPT_ATTEMPTS = 2
SENSITIVE_QUESTION = re.compile(
    r"(?i)\b(api[ _-]?key|credential|password|secret|token|production|prod)\b"
)


class DeliveryError(RuntimeError):
    pass


class DeliveryEscalation(DeliveryError):
    pass


class DeliveryDispatcher(Protocol):
    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome: ...

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str: ...

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome: ...


class HerdrDeliveryDispatcher:
    def __init__(self, workflow: WorkflowConfig) -> None:
        self.workflow = workflow
        self._transports: dict[Path, HerdrTransport] = {}
        self._lock = threading.Lock()

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        return self._transport(workspace).dispatch(
            harness,
            prompt,
            timeout_seconds=timeout_seconds,
            agent_name=agent_name,
        )

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        return self._transport(workspace).read_agent(name, lines=lines)

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        return self._transport(workspace).respond(
            name,
            harness,
            response,
            timeout_seconds=timeout_seconds,
        )

    def _transport(self, workspace: Path) -> HerdrTransport:
        resolved = workspace.resolve()
        with self._lock:
            transport = self._transports.get(resolved)
            if transport is None:
                transport = HerdrTransport(self.workflow.name, resolved)
                self._transports[resolved] = transport
            return transport


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


class StandardizedDelivery:
    def __init__(
        self,
        config: WorkflowConfig,
        *,
        dispatcher: DeliveryDispatcher | None = None,
        tracker: DeliveryTracker | None = None,
        controller_harness: Harness | None = None,
        controller_auto: bool = False,
        worker_harnesses: Iterable[Harness] | None = None,
    ) -> None:
        self.config = config
        self.dispatcher = dispatcher or HerdrDeliveryDispatcher(config)
        self.tracker = tracker or tracker_from_config(config.standardized_delivery)
        self.controller_override = controller_harness
        self.controller_auto = controller_auto
        self.worker_harnesses = effective_worker_harnesses(config, worker_harnesses)
        self.controller = select_controller_harness(
            config,
            worker_harnesses=self.worker_harnesses,
            override=controller_harness,
            force_auto=controller_auto,
        )
        self._goal = ""
        self._run_root = Path()
        self._ledger_lock = threading.Lock()

    def run(self, goal_file: Path) -> DeliveryResult:
        goal_path = goal_file.expanduser().resolve()
        if not goal_path.is_file():
            raise DeliveryError(f"delivery_goal_not_found: {goal_path}")
        goal = goal_path.read_text(encoding="utf-8").strip()
        if not goal:
            raise DeliveryError("delivery_goal_empty")
        self._goal = goal
        delivery_config = self.config.standardized_delivery
        run_id = hashlib.sha256(
            (
                f"{self.config.name}\0{self.config.workspace.resolve()}\0{goal}\0"
                f"{delivery_config.tracker_backend.value}\0"
                f"{delivery_config.tracker_root}\0"
                f"{delivery_config.github_repository}\0"
                f"{delivery_config.wayfinder.value}\0"
                f"{delivery_config.max_parallel}\0"
                f"{delivery_config.review_repair_rounds}\0"
                f"{self.controller.value}\0"
                f"{','.join(harness.value for harness in self.worker_harnesses)}"
            ).encode()
        ).hexdigest()[:12]
        self._run_root = self.config.standardized_delivery.artifact_root / run_id
        completed = _load_completed_result(self._run_root / "result.json", run_id)
        if completed is not None:
            return completed
        self._run_root.mkdir(parents=True, exist_ok=True)
        self._write_state("running", stage="wayfinder")
        try:
            wayfinder = self._run_wayfinder()
            self._write_state("running", stage="spec-and-tickets")
            plan = self._create_plan(wayfinder)
            references = self.tracker.publish(plan)
            self._record(
                "tracker_published",
                {
                    "backend": self.config.standardized_delivery.tracker_backend.value,
                    "tickets": {
                        key: value.reference for key, value in references.items()
                    },
                },
            )
            self._write_state("running", stage="implementation")
            integration, completed = self._implement_plan(plan)
            self._write_state("running", stage="final-review")
            review_rounds = self._review_and_repair(plan, integration)
            commit = GitWorkspace(
                self.config.workspace,
                self._run_root,
                plan.slug,
            ).head(integration)
            result = DeliveryResult(
                run_id=run_id,
                status="succeeded",
                artifact_root=self._run_root,
                tracker_references={
                    key: value.reference for key, value in references.items()
                },
                integration_branch=integration.branch,
                integration_commit=commit,
                tickets_completed=completed,
                review_rounds=review_rounds,
            )
            _write_json(
                self._run_root / "result.json",
                {
                    "run_id": result.run_id,
                    "status": result.status,
                    "artifact_root": str(result.artifact_root),
                    "tracker_references": result.tracker_references,
                    "integration_branch": result.integration_branch,
                    "integration_commit": result.integration_commit,
                    "tickets_completed": result.tickets_completed,
                    "review_rounds": result.review_rounds,
                },
            )
            self._write_state(
                "succeeded",
                stage="complete",
                integration_branch=integration.branch,
                integration_commit=commit,
            )
            return result
        except Exception as exc:
            self._write_state(
                "blocked" if isinstance(exc, DeliveryEscalation) else "failed",
                stage="stopped",
                error=type(exc).__name__,
            )
            raise

    def _run_wayfinder(self) -> WayfinderMap | None:
        mode = self.config.standardized_delivery.wayfinder
        if mode is WayfinderMode.ALWAYS:
            use_wayfinder = True
            reason = "configured_always"
        elif mode is WayfinderMode.NEVER:
            use_wayfinder = False
            reason = "configured_never"
        else:
            output = self._run_root / "wayfinder-route.json"
            if not output.is_file():
                self._dispatch_artifact(
                    self.config.workspace,
                    self.controller,
                    wayfinder_route_prompt(self._goal, output),
                    output,
                    role="way-route",
                )
            route = load_wayfinder_route(output)
            use_wayfinder = route.use_wayfinder
            reason = route.reason
        self._record(
            "wayfinder_routed",
            {"use_wayfinder": use_wayfinder, "reason": reason},
        )
        if not use_wayfinder:
            return None
        map_path = self._run_root / "wayfinder-map.json"
        if not map_path.is_file():
            self._dispatch_artifact(
                self.config.workspace,
                self.controller,
                wayfinder_chart_prompt(self._goal, map_path),
                map_path,
                role="way-chart",
            )
        map_ = load_wayfinder_map(map_path)
        iterations = 0
        while any(not ticket.resolution for ticket in map_.decisions):
            if iterations >= MAX_WAYFINDER_DECISIONS:
                raise DeliveryError("wayfinder_decision_limit")
            selected = _first_decision_frontier(map_)
            resolution_path = (
                self._run_root
                / "wayfinder"
                / f"resolution-{selected.ticket_id}.json"
            )
            resolution_path.parent.mkdir(parents=True, exist_ok=True)
            self._dispatch_artifact(
                self.config.workspace,
                self.controller,
                wayfinder_resolve_prompt(
                    self._goal,
                    json.dumps(_map_payload(map_), ensure_ascii=False, indent=2),
                    selected,
                    resolution_path,
                ),
                resolution_path,
                role=f"way-{selected.ticket_id}",
            )
            resolution = load_wayfinder_resolution(
                resolution_path,
                selected=selected,
                known_ids=tuple(ticket.ticket_id for ticket in map_.decisions),
            )
            decisions = tuple(
                replace(ticket, resolution=resolution.resolution)
                if ticket.ticket_id == selected.ticket_id
                else ticket
                for ticket in map_.decisions
            ) + resolution.new_decisions
            map_ = WayfinderMap(
                destination=map_.destination,
                notes=map_.notes,
                decisions=decisions,
                not_yet_specified=resolution.not_yet_specified,
                out_of_scope=resolution.out_of_scope,
            )
            _write_json(map_path, _map_payload(map_))
            self._record(
                "wayfinder_decision_resolved",
                {
                    "ticket_id": selected.ticket_id,
                    "title": selected.title,
                    "new_decisions": [
                        ticket.ticket_id for ticket in resolution.new_decisions
                    ],
                },
            )
            iterations += 1
        if map_.not_yet_specified:
            raise DeliveryError("wayfinder_fog_remaining")
        return map_

    def _create_plan(self, wayfinder: WayfinderMap | None) -> DeliveryPlan:
        output = self._run_root / "delivery-plan.json"
        if not output.is_file():
            self._dispatch_artifact(
                self.config.workspace,
                self.controller,
                plan_prompt(self._goal, output, wayfinder=wayfinder),
                output,
                role="plan",
            )
        plan = load_delivery_plan(output)
        self._record(
            "delivery_plan_accepted",
            {
                "slug": plan.slug,
                "seams": list(plan.seams),
                "tickets": [ticket.ticket_id for ticket in plan.tickets],
            },
        )
        return plan

    def _implement_plan(self, plan: DeliveryPlan) -> tuple[Worktree, int]:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        base_commit = git.base_commit()
        integration = git.create_integration(base_commit)
        completed: set[str] = set()
        tickets = {ticket.ticket_id: ticket for ticket in plan.tickets}
        while len(completed) < len(tickets):
            frontier = [
                ticket
                for ticket in plan.tickets
                if ticket.ticket_id not in completed
                and set(ticket.blocked_by).issubset(completed)
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
                    git.create_ticket(ticket.ticket_id, base_commit=integration_head),
                )
                for ticket, harness in routed
            ]
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
                git.merge(integration, result.worktree)
                self.tracker.close(result.ticket, result.receipt)
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

    def _implement_ticket(
        self,
        plan: DeliveryPlan,
        ticket: DeliveryTicket,
        harness: Harness,
        worktree: Worktree,
    ) -> _ImplementedTicket:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        receipt_file = self._run_root / "receipts" / f"ticket-{ticket.ticket_id}.json"
        receipt_file.parent.mkdir(parents=True, exist_ok=True)
        if receipt_file.is_file():
            receipt = load_ticket_receipt(receipt_file, ticket)
            commit = git.validate_commit(worktree)
            if receipt.commit != commit:
                raise DeliveryError(
                    f"ticket_receipt_commit_mismatch: {ticket.ticket_id}"
                )
            return _ImplementedTicket(ticket, worktree, receipt)
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
        return _ImplementedTicket(ticket, worktree, receipt)

    def _review_and_repair(self, plan: DeliveryPlan, integration: Worktree) -> int:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        review_rounds = 0
        for round_number in range(
            self.config.standardized_delivery.review_repair_rounds + 1
        ):
            report = self._review(plan, integration, round_number)
            review_rounds += 1
            findings = _finding_map(report)
            if not findings:
                self._record(
                    "review_completed",
                    {"round": round_number, "findings": 0, "accepted": []},
                )
                return review_rounds
            verdict_file = (
                self._run_root / "reviews" / f"round-{round_number}" / "verdict.json"
            )
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
            accepted = {
                finding_id: findings[finding_id]
                for finding_id in verdict.accepted
            }
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
                return review_rounds
            if round_number >= self.config.standardized_delivery.review_repair_rounds:
                raise DeliveryError("review_repair_rounds_exhausted")
            before = git.head(integration)
            harness = self._select_worker(
                f"Repair accepted review findings, round {round_number + 1}",
                json.dumps(
                    {key: finding.summary for key, finding in must_fix.items()},
                    ensure_ascii=False,
                ),
                f"{plan.slug}:repair:{round_number + 1}",
            )
            profile = profile_for_harness(self.config.profiles, harness)
            outcome = self._dispatch_with_proxy(
                integration.path,
                harness,
                execution_prompt(
                    profile,
                    repair_prompt(plan, must_fix, round_number + 1),
                ),
                role=f"repair-{round_number + 1}",
            )
            _require_success(outcome, f"repair_{round_number + 1}")
            after = git.validate_commit(
                Worktree(integration.path, integration.branch, before)
            )
            self._record(
                "review_repaired",
                {"round": round_number + 1, "commit": after},
            )
        raise DeliveryError("review_loop_invalid")

    def _review(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        round_number: int,
    ) -> ReviewReport:
        review_root = self._run_root / "reviews" / f"round-{round_number}"
        review_root.mkdir(parents=True, exist_ok=True)
        standards_file = review_root / "standards.json"
        spec_file = review_root / "spec.json"
        base_commit = integration.base_commit
        assignments = [
            (
                "standards",
                standards_file,
                standards_review_prompt(base_commit, standards_file),
            ),
            (
                "spec",
                spec_file,
                spec_review_prompt(base_commit, plan, spec_file),
            ),
        ]
        routed = [
            (
                axis,
                output,
                prompt,
                self._select_worker(
                    f"Final {axis} review, round {round_number}",
                    prompt,
                    f"{plan.slug}:review:{round_number}:{axis}",
                ),
            )
            for axis, output, prompt in assignments
        ]
        outcomes: dict[str, DispatchOutcome] = {}
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="delivery-review") as executor:
            futures = {
                executor.submit(
                    self._dispatch_with_proxy,
                    integration.path,
                    harness,
                    prompt,
                    role=f"review-{axis}-{round_number}",
                ): axis
                for axis, _, prompt, harness in routed
            }
            for future in as_completed(futures):
                axis = futures[future]
                outcomes[axis] = future.result()
        for axis, output, _, _ in routed:
            _require_success(outcomes[axis], f"review_{axis}")
            load_review_axis(output, axis)
        return ReviewReport(
            standards=load_review_axis(standards_file, "standards"),
            spec=load_review_axis(spec_file, "spec"),
        )

    def _select_worker(self, title: str, prompt: str, dedupe_key: str) -> Harness:
        profiles = self._worker_profiles()
        digest = hashlib.sha256(
            f"{self.config.name}\0delivery\0{dedupe_key}".encode()
        ).hexdigest()[:12]
        output = self._run_root / "routes" / f"{digest}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.is_file():
            self._dispatch_artifact(
                self.config.workspace,
                self.controller,
                worker_selection_prompt(
                    f"Title: {title}\n\nPrompt:\n{prompt}",
                    output,
                    render_compact_catalog(profiles),
                    self.worker_harnesses,
                ),
                output,
                role=f"route-{digest[:5]}",
            )
        try:
            selected = load_worker_selection(
                output,
                allowed_harnesses=self.worker_harnesses,
            )
        except PlannerOutputError as exc:
            raise DeliveryError(str(exc)) from exc
        self._record(
            "worker_routed",
            {"title": title, "harness": selected.value},
        )
        return selected

    def _dispatch_artifact(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        output_file: Path,
        *,
        role: str,
    ) -> None:
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.unlink(missing_ok=True)
        for attempt in range(ARTIFACT_PROMPT_ATTEMPTS):
            outcome = self._dispatch_with_proxy(
                workspace,
                harness,
                prompt,
                role=role,
            )
            _require_success(outcome, role)
            if output_file.is_file():
                return
            self._record(
                "artifact_prompt_retried",
                {"role": role, "attempt": attempt + 1},
            )
        raise DeliveryError(f"delivery_artifact_missing: {role}")

    def _dispatch_with_proxy(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        role: str,
    ) -> DispatchOutcome:
        if (
            workspace.resolve() == self.config.workspace.resolve()
            and harness is self.controller
        ):
            agent_name = _controller_agent_name(
                self.config.name,
                workspace,
                harness,
            )
        else:
            agent_name = _agent_name(role, workspace, harness)
        outcome = self.dispatcher.dispatch(
            workspace,
            harness,
            prompt,
            timeout_seconds=self.config.coordinator.agent_timeout_seconds,
            agent_name=agent_name,
        )
        for proxy_round in range(MAX_PROXY_ROUNDS):
            if outcome.state is not AgentState.BLOCKED:
                return outcome
            try:
                question = self.dispatcher.read_agent(
                    workspace,
                    agent_name,
                    lines=120,
                )
            except TransportError as exc:
                raise DeliveryError(
                    f"principal_proxy_read_failed:{exc.code}"
                ) from exc
            question_hash = hashlib.sha256(question.encode()).hexdigest()[:12]
            if SENSITIVE_QUESTION.search(question):
                self._record(
                    "principal_proxy_escalated",
                    {
                        "worker": agent_name,
                        "question_hash": question_hash,
                        "category": "secret-or-production",
                    },
                )
                raise DeliveryEscalation("principal_proxy_sensitive_escalation")
            decision_file = (
                self._run_root
                / "proxy"
                / f"{agent_name}-{proxy_round + 1}.json"
            )
            self._run_proxy_decision(
                question,
                decision_file,
                proxy_round=proxy_round + 1,
            )
            decision = load_proxy_decision(decision_file)
            self._record(
                "principal_proxy_decided",
                {
                    "worker": agent_name,
                    "question_hash": question_hash,
                    "action": decision.action.value,
                    "category": decision.category.value,
                    "rationale": decision.rationale,
                },
            )
            if decision.action is ProxyAction.ESCALATE or decision.category in {
                AuthorityCategory.SECRET,
                AuthorityCategory.PRODUCTION,
            }:
                raise DeliveryEscalation("principal_proxy_escalated")
            outcome = self.dispatcher.respond(
                workspace,
                agent_name,
                harness,
                decision.response,
                timeout_seconds=self.config.coordinator.agent_timeout_seconds,
            )
        raise DeliveryError("principal_proxy_rounds_exhausted")

    def _run_proxy_decision(
        self,
        question: str,
        decision_file: Path,
        *,
        proxy_round: int,
    ) -> None:
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        decision_file.unlink(missing_ok=True)
        name = _agent_name(
            f"principal-proxy-{proxy_round}",
            self.config.workspace,
            self.controller,
        )
        prompt = principal_proxy_prompt(self._goal, question, decision_file)
        for attempt in range(ARTIFACT_PROMPT_ATTEMPTS):
            outcome = self.dispatcher.dispatch(
                self.config.workspace,
                self.controller,
                prompt,
                timeout_seconds=self.config.coordinator.agent_timeout_seconds,
                agent_name=name,
            )
            if outcome.state is AgentState.BLOCKED:
                raise DeliveryEscalation("principal_proxy_controller_blocked")
            _require_success(outcome, "principal_proxy")
            if decision_file.is_file():
                return
            self._record(
                "artifact_prompt_retried",
                {"role": "principal_proxy", "attempt": attempt + 1},
            )
        raise DeliveryError("delivery_artifact_missing: principal_proxy")

    def _worker_profiles(self) -> tuple[HarnessProfile, ...]:
        return tuple(
            profile_for_harness(self.config.profiles, harness)
            for harness in self.worker_harnesses
        )

    def _record(self, event: str, details: dict[str, object]) -> None:
        row = {
            "event": event,
            "observed_at": time.time(),
            "details": details,
        }
        with self._ledger_lock:
            path = self._run_root / "decision-ledger.jsonl"
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, sort_keys=True))
                handle.write("\n")

    def _write_state(self, status: str, *, stage: str, **details: str) -> None:
        _write_json(
            self._run_root / "state.json",
            {
                "status": status,
                "stage": stage,
                **details,
            },
        )


def _first_decision_frontier(map_: WayfinderMap) -> DecisionTicket:
    resolved = {
        ticket.ticket_id for ticket in map_.decisions if ticket.resolution
    }
    for ticket in map_.decisions:
        if not ticket.resolution and set(ticket.blocked_by).issubset(resolved):
            return ticket
    raise DeliveryError("wayfinder_frontier_stalled")


def _finding_map(report: ReviewReport) -> dict[str, ReviewFinding]:
    findings: dict[str, ReviewFinding] = {}
    for axis, rows in (("standards", report.standards), ("spec", report.spec)):
        for index, finding in enumerate(rows, 1):
            findings[f"{axis}:{index}"] = finding
    return findings


def _require_success(outcome: DispatchOutcome, role: str) -> None:
    if outcome.error_code is not None or outcome.state not in {
        AgentState.IDLE,
        AgentState.DONE,
    }:
        raise DeliveryError(
            f"delivery_dispatch_failed:{role}:"
            f"{outcome.error_code or outcome.state.value}"
        )


def _agent_name(role: str, workspace: Path, harness: Harness) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "agent"
    digest = hashlib.sha256(
        f"{role}\0{workspace.resolve()}\0{harness.value}".encode()
    ).hexdigest()[:7]
    prefix = f"hd-{normalized[:17].rstrip('-')}"
    return f"{prefix}-{digest}"[:32].rstrip("-")


def _controller_agent_name(
    workflow_name: str,
    workspace: Path,
    harness: Harness,
) -> str:
    digest = hashlib.sha256(
        f"{workflow_name}\0{workspace.resolve()}\0delivery-control\0{harness.value}".encode()
    ).hexdigest()[:8]
    return f"hd-control-{harness.value}-{digest}"


def _map_payload(map_: WayfinderMap) -> dict[str, object]:
    return {
        "destination": map_.destination,
        "notes": list(map_.notes),
        "decisions": [
            {
                "id": ticket.ticket_id,
                "title": ticket.title,
                "question": ticket.question,
                "kind": ticket.kind,
                "blocked_by": list(ticket.blocked_by),
                "resolution": ticket.resolution,
            }
            for ticket in map_.decisions
        ],
        "not_yet_specified": list(map_.not_yet_specified),
        "out_of_scope": list(map_.out_of_scope),
    }


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_completed_result(path: Path, run_id: str) -> DeliveryResult | None:
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
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
            isinstance(key, str) and isinstance(value, str)
            for key, value in references.items()
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
