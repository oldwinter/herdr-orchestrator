from __future__ import annotations

import hashlib
import json
import re
import threading
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.catalog import (
    profile_for_harness,
    render_compact_catalog,
)
from herdr_orchestrator.delivery_journal import (
    DeliveryEffect,
    DeliveryEffectObservation,
    DeliveryJournal,
)
from herdr_orchestrator.delivery_prompts import (
    plan_prompt,
    principal_proxy_prompt,
    spec_review_prompt,
    standards_review_prompt,
    wayfinder_chart_prompt,
    wayfinder_resolve_prompt,
    wayfinder_route_prompt,
)
from herdr_orchestrator.delivery_protocol import (
    AuthorityCategory,
    DecisionTicket,
    DeliveryArtifactError,
    DeliveryPlan,
    ProxyAction,
    ProxyDecision,
    ReviewReport,
    WayfinderMap,
    append_artifact_text,
    exclusive_file_claim,
    load_delivery_plan,
    load_proxy_decision,
    load_review_axis,
    load_wayfinder_map,
    load_wayfinder_resolution,
    load_wayfinder_route,
    map_payload,
)
from herdr_orchestrator.delivery_recovery import (
    DeliveryError as DeliveryError,
)
from herdr_orchestrator.delivery_recovery import (
    DeliveryRecoveryMixin,
    DeliveryResult,
    _agent_is_active,
    _effect_absent,
    _effect_conflict,
    _effect_matched,
    _file_sha256,
    _finite_number,
    _journal_payload,
    _load_completed_result,
    _require_success,
    _safe_delivery_path,
    _validate_worktree_clean,
    _validate_worktree_ownership,
    _write_json,
)
from herdr_orchestrator.delivery_repair import DeliveryRepairMixin
from herdr_orchestrator.git_workspace import GitWorkspace, GitWorkspaceError, Worktree
from herdr_orchestrator.herdr import HerdrTransport
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    HarnessProfile,
    WayfinderMode,
    WorkflowConfig,
)
from herdr_orchestrator.observability import sanitize
from herdr_orchestrator.planner import (
    PlannerOutputError,
    load_worker_selection,
    worker_selection_prompt,
)
from herdr_orchestrator.protocol import Command, TransportError, run_json
from herdr_orchestrator.selection import (
    effective_worker_harnesses,
    select_controller_harness,
)
from herdr_orchestrator.tracker import (
    DeliveryTracker,
    contains_high_confidence_secret,
    tracker_from_config,
)

MAX_WAYFINDER_DECISIONS = 100
MAX_PROXY_ROUNDS = 8
ARTIFACT_PROMPT_ATTEMPTS = 2
DELIVERY_LEASE_GRACE_SECONDS = 30.0
MINIMUM_DELIVERY_LEASE_SECONDS = 60.0
SENSITIVE_QUESTION = re.compile(
    r"(?i)\b(api[ _-]?key|credential|password|secret|token|production|prod)\b"
)


_delivery_run_claim = partial(exclusive_file_claim, error_type=DeliveryError)


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

    def inspect_agent(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
    ) -> DispatchOutcome | None:
        transport = self._transport(workspace)
        try:
            result = run_json(
                transport.runner,
                Command(
                    ["herdr", "agent", "get", name],
                    workspace,
                    10,
                ),
            )
        except TransportError as exc:
            if exc.code == "agent_not_found":
                return None
            raise
        agent = result.get("agent")
        if not isinstance(agent, dict):
            raise TransportError("herdr_invalid_response")
        state_value = agent.get("agent_status")
        pane_id = agent.get("pane_id")
        workspace_id = agent.get("workspace_id")
        if (
            agent.get("name") not in {None, name}
            or agent.get("agent") != harness.value
            or not isinstance(state_value, str)
            or not isinstance(pane_id, str)
            or not pane_id
            or not isinstance(agent.get("interactive_ready"), bool)
            or not agent["interactive_ready"]
            or any(
                not isinstance(agent.get(key), str)
                or Path(agent[key]).resolve() != workspace.resolve()
                for key in ("cwd", "foreground_cwd")
            )
            or (
                workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id)
            )
        ):
            raise TransportError("agent_identity_mismatch")
        try:
            state = AgentState(state_value)
        except ValueError as exc:
            raise TransportError("herdr_invalid_response") from exc
        return DispatchOutcome(
            name,
            state,
            True,
            pane_id,
            execution_path=str(workspace.resolve()),
            herdr_workspace_id=workspace_id,
            agent_settled=state in {AgentState.IDLE, AgentState.DONE},
        )

    def _transport(self, workspace: Path) -> HerdrTransport:
        resolved = workspace.resolve()
        with self._lock:
            transport = self._transports.get(resolved)
            if transport is None:
                transport = HerdrTransport(self.workflow.name, resolved)
                self._transports[resolved] = transport
            return transport


class StandardizedDelivery(DeliveryRecoveryMixin, DeliveryRepairMixin):
    def __init__(
        self,
        config: WorkflowConfig,
        *,
        dispatcher: DeliveryDispatcher | None = None,
        tracker: DeliveryTracker | None = None,
        controller_harness: Harness | None = None,
        controller_auto: bool = False,
        worker_harnesses: Iterable[Harness] | None = None,
        lease_seconds: float | None = None,
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
        self._run_id = ""
        self._run_root = Path()
        self._ledger_lock = threading.Lock()
        self._previous_state: dict[str, object] = {}
        self._journal: DeliveryJournal | None = None
        if lease_seconds is not None and (not _finite_number(lease_seconds) or lease_seconds <= 0):
            raise DeliveryError("delivery_lease_seconds_invalid")
        self._lease_seconds = lease_seconds

    def run(self, goal_file: Path) -> DeliveryResult:
        goal_path = _safe_delivery_path(goal_file)
        if not goal_path.is_file():
            raise DeliveryError(f"delivery_goal_not_found: {goal_path}")
        goal = goal_path.read_text(encoding="utf-8").strip()
        if not goal:
            raise DeliveryError("delivery_goal_empty")
        if (
            self.config.standardized_delivery.tracker_backend.value == "github"
            and contains_high_confidence_secret(goal)
        ):
            raise DeliveryError(
                "delivery_secret_material_rejected: remove secret material before retry"
            )
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
        self._run_id = run_id
        self._run_root = _safe_delivery_path(
            self.config.standardized_delivery.artifact_root / run_id,
            root=self.config.standardized_delivery.artifact_root,
        )
        self._run_root.mkdir(parents=True, exist_ok=True)
        _safe_delivery_path(self._run_root, root=self.config.standardized_delivery.artifact_root)
        self._previous_state = {}
        lease_seconds = self._lease_seconds or max(
            MINIMUM_DELIVERY_LEASE_SECONDS,
            self.config.coordinator.agent_timeout_seconds + DELIVERY_LEASE_GRACE_SECONDS,
        )
        with DeliveryJournal.claim(
            self._run_root,
            run_id,
            lease_seconds,
            error_type=DeliveryError,
            payload_validator=_journal_payload,
        ) as journal:
            self._journal = journal
            try:
                return self._run_claimed(run_id)
            finally:
                self._journal = None

    def _run_claimed(self, run_id: str) -> DeliveryResult:
        if (state_path := self._run_root / "state.json").is_file():
            _safe_delivery_path(state_path, root=self._run_root)
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._write_state("failed", stage="stopped", error=type(exc).__name__)
                raise DeliveryError("delivery_state_invalid") from exc
            if not isinstance(state, dict):
                self._write_state("failed", stage="stopped", error="DeliveryStateInvalid")
                raise DeliveryError("delivery_state_invalid")
            self._previous_state = state
        self._recover_proxy_responses()
        try:
            completed_result = _load_completed_result(self._run_root / "result.json", run_id)
        except Exception as exc:
            self._write_state("failed", stage="stopped", error=type(exc).__name__)
            raise
        if completed_result is not None:
            try:
                self._validate_completed_result(completed_result)
                self._publish_result(completed_result)
            except Exception as exc:
                self._write_state(
                    "failed",
                    stage="stopped",
                    error=type(exc).__name__,
                )
                raise
            self._write_state(
                "succeeded",
                stage="complete",
                integration_branch=completed_result.integration_branch,
                integration_commit=completed_result.integration_commit,
            )
            return completed_result
        failed_stage = "wayfinder"
        self._write_state("running", stage="wayfinder")
        try:
            self._delivery_base_commit(
                GitWorkspace(self.config.workspace, self._run_root, "delivery")
            )
            wayfinder = self._run_wayfinder()
            failed_stage = "spec-and-tickets"
            self._write_state("running", stage="spec-and-tickets")
            plan = self._create_plan(wayfinder)
            failed_stage = "tracker-publish"
            self._write_state("running", stage="tracker-publish")
            references = self._publish_tracker(plan)
            self._record(
                "tracker_published",
                {
                    "backend": self.config.standardized_delivery.tracker_backend.value,
                    "tickets": {key: value.reference for key, value in references.items()},
                },
            )
            failed_stage = "implementation"
            self._write_state("running", stage="implementation")
            integration, tickets_completed = self._implement_plan(plan)
            failed_stage = "final-review"
            self._write_state("running", stage="final-review")
            review_rounds = self._review_and_repair(plan, integration)
            git = GitWorkspace(
                self.config.workspace,
                self._run_root,
                plan.slug,
            )
            _validate_worktree_ownership(
                git,
                self._run_root / "worktrees" / "integration",
                integration,
            )
            commit = git.validate_commit(integration)
            result = DeliveryResult(
                run_id=run_id,
                status="succeeded",
                artifact_root=self._run_root,
                tracker_references={key: value.reference for key, value in references.items()},
                integration_branch=integration.branch,
                integration_commit=commit,
                tickets_completed=tickets_completed,
                review_rounds=review_rounds,
            )
            self._publish_result(result)
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
                failed_stage=failed_stage,
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
            if self._journal is not None or not output.is_file():
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
        if self._journal is not None or not map_path.is_file():
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
            resolution_path = self._run_root / "wayfinder" / f"resolution-{selected.ticket_id}.json"
            _safe_delivery_path(resolution_path, root=self._run_root)
            resolution_path.parent.mkdir(parents=True, exist_ok=True)
            self._dispatch_artifact(
                self.config.workspace,
                self.controller,
                wayfinder_resolve_prompt(
                    self._goal,
                    json.dumps(map_payload(map_), ensure_ascii=False, indent=2),
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
            decisions = (
                tuple(
                    (
                        replace(ticket, resolution=resolution.resolution)
                        if ticket.ticket_id == selected.ticket_id
                        else ticket
                    )
                    for ticket in map_.decisions
                )
                + resolution.new_decisions
            )
            map_ = WayfinderMap(
                destination=map_.destination,
                notes=map_.notes,
                decisions=decisions,
                not_yet_specified=resolution.not_yet_specified,
                out_of_scope=resolution.out_of_scope,
            )
            _write_json(map_path, map_payload(map_))
            self._record(
                "wayfinder_decision_resolved",
                {
                    "ticket_id": selected.ticket_id,
                    "title": selected.title,
                    "new_decisions": [ticket.ticket_id for ticket in resolution.new_decisions],
                },
            )
            iterations += 1
        if map_.not_yet_specified:
            raise DeliveryError("wayfinder_fog_remaining")
        return map_

    def _create_plan(self, wayfinder: WayfinderMap | None) -> DeliveryPlan:
        output = self._run_root / "delivery-plan.json"
        if self._journal is not None or not output.is_file():
            self._dispatch_artifact(
                self.config.workspace,
                self.controller,
                plan_prompt(self._goal, output, wayfinder=wayfinder),
                output,
                role="plan",
            )
        plan = load_delivery_plan(output)
        if (
            self.config.standardized_delivery.tracker_backend.value == "github"
            and contains_high_confidence_secret(plan)
        ):
            raise DeliveryError(
                "delivery_secret_material_rejected: remove secret material before retry"
            )
        self._record(
            "delivery_plan_accepted",
            {
                "slug": plan.slug,
                "seams": list(plan.seams),
                "tickets": [ticket.ticket_id for ticket in plan.tickets],
            },
        )
        return plan

    def _review(
        self,
        plan: DeliveryPlan,
        integration: Worktree,
        round_number: int,
    ) -> ReviewReport:
        git = GitWorkspace(self.config.workspace, self._run_root, plan.slug)
        _validate_worktree_ownership(
            git,
            self._run_root / "worktrees" / "integration",
            integration,
        )
        _validate_worktree_clean(git, integration.path)
        if self._journal is not None:
            self._assert_integration_frontier(git, integration)
        head_before = git.validate_commit(integration)
        review_root = self._run_root / "reviews" / f"round-{round_number}"
        _safe_delivery_path(review_root, root=self._run_root)
        review_root.mkdir(parents=True, exist_ok=True)
        standards_file = review_root / "standards.json"
        spec_file = review_root / "spec.json"
        base_commit = self._delivery_base_commit(git)
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
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="delivery-review") as executor:
            futures = {
                executor.submit(
                    self._dispatch_artifact,
                    integration.path,
                    harness,
                    prompt,
                    output,
                    role=f"review-{axis}-{round_number}",
                    journal_context={
                        "round": round_number,
                        "axis": axis,
                        "integration_commit": head_before,
                    },
                ): axis
                for axis, output, prompt, harness in routed
            }
            for future in as_completed(futures):
                future.result()
        for axis, output, _, _ in routed:
            load_review_axis(output, axis)
        _validate_worktree_ownership(
            git,
            self._run_root / "worktrees" / "integration",
            integration,
        )
        head_after = git.validate_commit(integration)
        if head_after != head_before:
            raise DeliveryError("delivery_review_mutated_integration")
        report = ReviewReport(
            standards=load_review_axis(standards_file, "standards"),
            spec=load_review_axis(spec_file, "spec"),
        )
        journal = self._journal
        if journal is not None:
            expected = {
                "round": round_number,
                "integration_commit": head_before,
                "standards_sha256": _file_sha256(standards_file),
                "spec_sha256": _file_sha256(spec_file),
                "standards_findings": len(report.standards),
                "spec_findings": len(report.spec),
            }

            def observe(
                confirmation: dict[str, object] | None,
                started: bool,
            ) -> DeliveryEffectObservation:
                try:
                    current = git.validate_commit(integration)
                    observed_report = ReviewReport(
                        standards=load_review_axis(standards_file, "standards"),
                        spec=load_review_axis(spec_file, "spec"),
                    )
                except (DeliveryArtifactError, GitWorkspaceError):
                    return _effect_conflict()
                if current != head_before:
                    return _effect_conflict()
                details = {
                    "round": round_number,
                    "integration_commit": current,
                    "standards_sha256": _file_sha256(standards_file),
                    "spec_sha256": _file_sha256(spec_file),
                    "standards_findings": len(observed_report.standards),
                    "spec_findings": len(observed_report.spec),
                }
                return _effect_matched(details)

            payload = journal.reconcile(
                DeliveryEffect(
                    key=f"review:accept:{round_number}",
                    kind="review.accept",
                    intent={
                        "round": round_number,
                        "integration_commit": head_before,
                    },
                    observe=observe,
                    apply=lambda: expected,
                )
            )
            if payload != expected:
                raise DeliveryError("delivery_recovery_conflict:review.accept")
        return report

    def _select_worker(self, title: str, prompt: str, dedupe_key: str) -> Harness:
        profiles = self._worker_profiles()
        digest = hashlib.sha256(f"{self.config.name}\0delivery\0{dedupe_key}".encode()).hexdigest()[
            :12
        ]
        output = self._run_root / "routes" / f"{digest}.json"
        _safe_delivery_path(output, root=self._run_root)
        output.parent.mkdir(parents=True, exist_ok=True)
        if self._journal is not None or not output.is_file():
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
        journal_context: dict[str, object] | None = None,
        use_principal_proxy: bool = True,
        agent_name_override: str | None = None,
    ) -> None:
        _safe_delivery_path(output_file, root=self._run_root)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        journal = self._journal
        if journal is None:
            output_file.unlink(missing_ok=True)
            self._run_artifact_dispatch(
                workspace,
                harness,
                prompt,
                output_file,
                role=role,
                use_principal_proxy=use_principal_proxy,
                agent_name_override=agent_name_override,
            )
            return
        normalized_role = re.sub(r"[^a-z0-9.-]+", "-", role.lower()).strip("-")
        operation_key = f"agent:artifact:{normalized_role}"
        artifact = str(output_file.relative_to(self._run_root))
        agent_name = agent_name_override or self._delivery_agent_name(
            workspace,
            harness,
            role,
        )
        pending = journal.has_intent(operation_key)
        prompt_sha256 = hashlib.sha256(prompt.encode()).hexdigest()
        context = {} if journal_context is None else journal_context

        def dispatch() -> dict[str, object]:
            if not pending:
                output_file.unlink(missing_ok=True)
            outcome = (
                None
                if output_file.is_file()
                else self._run_artifact_dispatch(
                    workspace,
                    harness,
                    prompt,
                    output_file,
                    role=role,
                    use_principal_proxy=use_principal_proxy,
                    agent_name_override=agent_name,
                )
            )
            return {
                "role": role,
                "artifact": artifact,
                "artifact_sha256": _file_sha256(output_file),
                "agent_name": agent_name,
                "harness": harness.value,
                "pane_id": None if outcome is None else outcome.pane_id,
                "herdr_workspace_id": (None if outcome is None else outcome.herdr_workspace_id),
                "state": "recovered" if outcome is None else outcome.state.value,
                "prompt_sha256": prompt_sha256,
                "context": context,
                "use_principal_proxy": use_principal_proxy,
            }

        def observe(
            expected: dict[str, object] | None,
            started: bool,
        ) -> DeliveryEffectObservation:
            inspection_supported = False
            inspected: DispatchOutcome | None = None
            if started:
                try:
                    inspection_supported, inspected = self._inspect_delivery_agent(
                        workspace,
                        agent_name,
                        harness,
                    )
                except TransportError:
                    return _effect_conflict()
                if _agent_is_active(inspected):
                    return _effect_conflict()
            if not output_file.is_file():
                return _effect_absent()
            if inspection_supported and inspected is None:
                return _effect_conflict()
            invariant = {
                "role": role,
                "artifact": artifact,
                "artifact_sha256": _file_sha256(output_file),
                "agent_name": agent_name,
                "harness": harness.value,
                "prompt_sha256": prompt_sha256,
                "context": context,
                "use_principal_proxy": use_principal_proxy,
            }
            if expected is not None:
                if any(expected.get(key) != value for key, value in invariant.items()):
                    return _effect_conflict()
                return _effect_matched(expected)
            return _effect_matched(
                {
                    **invariant,
                    "pane_id": None,
                    "herdr_workspace_id": None,
                    "state": "recovered",
                }
            )

        payload = journal.reconcile(
            DeliveryEffect(
                key=operation_key,
                kind="agent.dispatch",
                intent={
                    "role": role,
                    "artifact": artifact,
                    "agent_name": agent_name,
                    "harness": harness.value,
                    "prompt_sha256": prompt_sha256,
                    "context": context,
                    "use_principal_proxy": use_principal_proxy,
                },
                observe=observe,
                apply=dispatch,
            )
        )
        if (
            not output_file.is_file()
            or payload.get("role") != role
            or payload.get("artifact") != artifact
            or payload.get("agent_name") != agent_name
            or payload.get("harness") != harness.value
            or payload.get("prompt_sha256") != prompt_sha256
            or payload.get("context") != context
            or payload.get("use_principal_proxy") != use_principal_proxy
            or payload.get("artifact_sha256") != _file_sha256(output_file)
        ):
            raise DeliveryError("delivery_recovery_conflict:agent.dispatch")

    def _run_artifact_dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        output_file: Path,
        *,
        role: str,
        use_principal_proxy: bool,
        agent_name_override: str | None,
    ) -> DispatchOutcome:
        for attempt in range(ARTIFACT_PROMPT_ATTEMPTS):
            if use_principal_proxy:
                outcome = self._dispatch_with_proxy(
                    workspace,
                    harness,
                    prompt,
                    role=role,
                    agent_name_override=agent_name_override,
                )
            else:
                outcome = self.dispatcher.dispatch(
                    workspace,
                    harness,
                    prompt,
                    timeout_seconds=self.config.coordinator.agent_timeout_seconds,
                    agent_name=(
                        agent_name_override or self._delivery_agent_name(workspace, harness, role)
                    ),
                )
                if outcome.state is AgentState.BLOCKED:
                    raise DeliveryEscalation("principal_proxy_controller_blocked")
            _require_success(outcome, role)
            if output_file.is_file():
                return outcome
            self._record(
                "artifact_prompt_retried",
                {"role": role, "attempt": attempt + 1},
            )
        raise DeliveryError(f"delivery_artifact_missing: {role}")

    def _delivery_agent_name(
        self,
        workspace: Path,
        harness: Harness,
        role: str,
    ) -> str:
        if workspace.resolve() == self.config.workspace.resolve() and harness is self.controller:
            return _controller_agent_name(
                self.config.name,
                workspace,
                harness,
            )
        return _agent_name(role, workspace, harness)

    def _inspect_delivery_agent(
        self,
        workspace: Path,
        agent_name: str,
        harness: Harness,
    ) -> tuple[bool, DispatchOutcome | None]:
        inspect = getattr(self.dispatcher, "inspect_agent", None)
        if not callable(inspect):
            return False, None
        return True, inspect(workspace, agent_name, harness)

    def _dispatch_with_proxy(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        role: str,
        agent_name_override: str | None = None,
    ) -> DispatchOutcome:
        agent_name = agent_name_override or self._delivery_agent_name(
            workspace,
            harness,
            role,
        )
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
                raise DeliveryError(f"principal_proxy_read_failed:{exc.code}") from exc
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
            decision_file = self._run_root / "proxy" / f"{agent_name}-{proxy_round + 1}.json"
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
            outcome = self._respond_with_journal(
                workspace,
                agent_name,
                harness,
                decision,
                question_hash=question_hash,
                proxy_round=proxy_round + 1,
                decision_file=decision_file,
            )
        raise DeliveryError("principal_proxy_rounds_exhausted")

    def _respond_with_journal(
        self,
        workspace: Path,
        agent_name: str,
        harness: Harness,
        decision: ProxyDecision,
        *,
        question_hash: str,
        proxy_round: int,
        decision_file: Path,
    ) -> DispatchOutcome:
        if self._journal is None:
            return self.dispatcher.respond(
                workspace,
                agent_name,
                harness,
                decision.response,
                timeout_seconds=self.config.coordinator.agent_timeout_seconds,
            )
        result: DispatchOutcome | None = None
        workspace_key = (
            "source"
            if workspace.resolve() == self.config.workspace.resolve()
            else str(workspace.relative_to(self._run_root))
        )
        result_base: dict[str, object] = {
            "worker": agent_name,
            "harness": harness.value,
            "workspace": workspace_key,
            "question_hash": question_hash,
            "action": decision.action.value,
            "category": decision.category.value,
            "proxy_round": proxy_round,
            "decision_artifact": str(decision_file.relative_to(self._run_root)),
        }

        def details(outcome: DispatchOutcome) -> dict[str, object]:
            return {
                **result_base,
                "pane_id": outcome.pane_id,
                "herdr_workspace_id": outcome.herdr_workspace_id,
                "settled": outcome.state in {AgentState.IDLE, AgentState.DONE},
            }

        def respond() -> dict[str, object]:
            nonlocal result
            result = self.dispatcher.respond(
                workspace,
                agent_name,
                harness,
                decision.response,
                timeout_seconds=self.config.coordinator.agent_timeout_seconds,
            )
            _require_success(result, f"principal_proxy_response_{proxy_round}")
            return details(result)

        def observe(
            expected: dict[str, object] | None,
            started: bool,
        ) -> DeliveryEffectObservation:
            try:
                supported, inspected = self._inspect_delivery_agent(
                    workspace,
                    agent_name,
                    harness,
                )
            except TransportError:
                return _effect_conflict()
            if not supported:
                return _effect_absent() if expected is None else _effect_matched(expected)
            if inspected is None:
                return _effect_conflict()
            if inspected.state is AgentState.BLOCKED:
                return _effect_absent()
            if inspected.state in {AgentState.WORKING, AgentState.UNKNOWN}:
                return _effect_conflict()
            observed = details(inspected)
            return _effect_matched(observed)

        payload = self._journal.reconcile(
            DeliveryEffect(
                key=f"agent:response:{agent_name}:{question_hash}:{proxy_round}",
                kind="agent.respond",
                intent=result_base,
                observe=observe,
                apply=respond,
            )
        )
        if payload.get("settled") is not True:
            raise DeliveryError("delivery_recovery_conflict:agent.respond")
        if result is not None:
            return result
        pane_id = payload.get("pane_id")
        workspace_id = payload.get("herdr_workspace_id")
        return DispatchOutcome(
            agent_name,
            AgentState.DONE,
            True,
            pane_id if isinstance(pane_id, str) else None,
            herdr_workspace_id=(workspace_id if isinstance(workspace_id, str) else None),
            agent_settled=True,
        )

    def _recover_proxy_responses(self) -> None:
        journal = self._journal
        if journal is None:
            return
        for pending in journal.pending_effects(kind="agent.respond"):
            intent = pending.intent
            worker = intent.get("worker")
            harness_value = intent.get("harness")
            workspace_value = intent.get("workspace")
            question_hash = intent.get("question_hash")
            proxy_round = intent.get("proxy_round")
            artifact_value = intent.get("decision_artifact")
            if (
                not isinstance(worker, str)
                or not isinstance(harness_value, str)
                or not isinstance(workspace_value, str)
                or not isinstance(question_hash, str)
                or re.fullmatch(r"[0-9a-f]{12}", question_hash) is None
                or not isinstance(proxy_round, int)
                or isinstance(proxy_round, bool)
                or proxy_round < 1
                or not isinstance(artifact_value, str)
            ):
                raise DeliveryError("delivery_journal_invalid")
            try:
                harness = Harness(harness_value)
            except ValueError as exc:
                raise DeliveryError("delivery_journal_invalid") from exc
            workspace = (
                self.config.workspace
                if workspace_value == "source"
                else _safe_delivery_path(
                    self._run_root / workspace_value,
                    root=self._run_root,
                )
            )
            decision_file = _safe_delivery_path(
                self._run_root / artifact_value,
                root=self._run_root,
            )
            decision = load_proxy_decision(decision_file)
            if (
                intent.get("action") != decision.action.value
                or intent.get("category") != decision.category.value
            ):
                raise DeliveryError("delivery_recovery_conflict:agent.respond")
            self._respond_with_journal(
                workspace,
                worker,
                harness,
                decision,
                question_hash=question_hash,
                proxy_round=proxy_round,
                decision_file=decision_file,
            )

    def _run_proxy_decision(
        self,
        question: str,
        decision_file: Path,
        *,
        proxy_round: int,
    ) -> None:
        _safe_delivery_path(decision_file, root=self._run_root)
        decision_file.parent.mkdir(parents=True, exist_ok=True)
        name = _agent_name(
            f"principal-proxy-{proxy_round}",
            self.config.workspace,
            self.controller,
        )
        prompt = principal_proxy_prompt(self._goal, question, decision_file)
        self._dispatch_artifact(
            self.config.workspace,
            self.controller,
            prompt,
            decision_file,
            role=f"principal-proxy-{proxy_round}",
            journal_context={
                "proxy_round": proxy_round,
                "question_hash": hashlib.sha256(question.encode()).hexdigest()[:12],
            },
            use_principal_proxy=False,
            agent_name_override=name,
        )

    def _worker_profiles(self) -> tuple[HarnessProfile, ...]:
        return tuple(
            profile_for_harness(self.config.profiles, harness) for harness in self.worker_harnesses
        )

    def _record(self, event: str, details: dict[str, object]) -> None:
        row = {
            "event": event,
            "observed_at": time.time(),
            "details": sanitize(details),
        }
        with self._ledger_lock:
            path = self._run_root / "decision-ledger.jsonl"
            append_artifact_text(
                path,
                json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n",
                root=self._run_root,
                error_type=DeliveryError,
            )

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
    resolved = {ticket.ticket_id for ticket in map_.decisions if ticket.resolution}
    for ticket in map_.decisions:
        if not ticket.resolution and set(ticket.blocked_by).issubset(resolved):
            return ticket
    raise DeliveryError("wayfinder_frontier_stalled")


def _agent_name(role: str, workspace: Path, harness: Harness) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", role.lower()).strip("-") or "agent"
    digest = hashlib.sha256(f"{role}\0{workspace.resolve()}\0{harness.value}".encode()).hexdigest()[
        :7
    ]
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
