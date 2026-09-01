from __future__ import annotations

import hashlib
import time
from collections.abc import Callable, Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from herdr_orchestrator.catalog import (
    execution_prompt,
    profile_for_harness,
    render_compact_catalog,
)
from herdr_orchestrator.completion import (
    CompletionIdentity,
    CompletionPolicy,
    structured_completion_prompt,
)
from herdr_orchestrator.harness_health import (
    HarnessHealth,
    HealthProbe,
)
from herdr_orchestrator.herdr import (
    HerdrTransport,
    replica_slot_names,
    worktree_agent_name,
)
from herdr_orchestrator.model import (
    AgentState,
    AttemptPhase,
    AttemptProgress,
    AttemptTransition,
    ClaimedJob,
    DispatchContext,
    DispatchOutcome,
    Harness,
    HarnessProfile,
    JobState,
    NewJob,
    PlacementTarget,
    TaskReceipt,
    WorkflowConfig,
)
from herdr_orchestrator.observability import Observability
from herdr_orchestrator.planner import (
    PlannerOutputError,
    load_planner_tasks,
    load_worker_selection,
    planner_prompt,
    worker_selection_prompt,
)
from herdr_orchestrator.protocol import TransportError
from herdr_orchestrator.selection import (
    effective_worker_harnesses,
    eligible_worker_harnesses,
    select_controller_harness,
)
from herdr_orchestrator.store import Store
from herdr_orchestrator.topology import (
    TopologyDecisionError,
    load_topology_decision,
    static_placement,
    topology_decision_prompt,
)

if TYPE_CHECKING:
    from herdr_orchestrator.harness_health import EligibilitySnapshot


class Dispatcher(Protocol):
    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: float,
        agent_name: str | None = None,
        context: DispatchContext | None = None,
    ) -> DispatchOutcome: ...

    def respond(
        self,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
        expected_pane_id: str,
        context: DispatchContext | None,
    ) -> DispatchOutcome: ...


class _DispatchDeadlineExceeded(RuntimeError):
    pass


class OperationInterrupted(RuntimeError):
    pass


class Coordinator:
    def __init__(
        self,
        config: WorkflowConfig,
        *,
        store: Store | None = None,
        dispatcher: Dispatcher | None = None,
        controller_harness: Harness | None = None,
        controller_auto: bool = False,
        worker_harnesses: Iterable[Harness] | None = None,
        observability: Observability | None = None,
        transition_observer: Callable[[AttemptTransition], None] | None = None,
        health: HarnessHealth | None = None,
        readiness_probe: HealthProbe | None = None,
    ) -> None:
        self.config = config
        self.store = store or Store(config.state_db)
        self.dispatcher = dispatcher or HerdrTransport(config.name, config.workspace)
        self.controller_harness = controller_harness
        self.controller_auto = controller_auto
        self.worker_harnesses = effective_worker_harnesses(config, worker_harnesses)
        self.observability = observability or Observability(
            config.state_db.parent / "telemetry",
            config.name,
        )
        self.transition_observer = transition_observer
        self.health = health
        self.readiness_probe = readiness_probe
        health_harnesses = list(self.worker_harnesses)
        for harness in (self.controller_harness, self.config.planner.harness):
            if harness is not None and harness not in health_harnesses:
                health_harnesses.append(harness)
        self._health_harnesses = tuple(health_harnesses)
        self._last_health_snapshot: EligibilitySnapshot | None = None

    def initialize(self) -> None:
        self.store.initialize()

    def seed(self) -> tuple[int, int]:
        self.initialize()
        added = 0
        existing = 0
        for seed in self.config.seed_jobs:
            prompt = seed.prompt_file.read_text(encoding="utf-8").strip()
            _, created = self.store.enqueue(
                NewJob(
                    workflow=self.config.name,
                    workspace=str(self.config.workspace.resolve()),
                    title=seed.title,
                    harness=seed.harness,
                    prompt=prompt,
                    dedupe_key=seed.dedupe_key,
                    max_attempts=self.config.coordinator.max_attempts,
                    placement=self._static_placement(
                        seed.title,
                        prompt,
                        seed.harness,
                        seed.placement,
                    ),
                )
            )
            added += int(created)
            existing += int(not created)
        return added, existing

    def enqueue_prompt_file(
        self,
        *,
        harness: Harness | None,
        title: str,
        prompt_file: Path,
        dedupe_key: str,
        placement: PlacementTarget | None = None,
        receipt: TaskReceipt | None = None,
        completion_policy: CompletionPolicy | None = None,
    ) -> tuple[int, bool, Harness]:
        self.initialize()
        if not prompt_file.is_file():
            raise ValueError(f"prompt_file_not_found: {prompt_file}")
        prompt = prompt_file.read_text(encoding="utf-8").strip()
        if not prompt:
            raise ValueError("prompt_file_empty")
        existing = self.store.existing_job_for_enqueue(
            self.config.name,
            dedupe_key,
            title=title,
            prompt=prompt,
            harness=harness,
            placement=placement,
            receipt=receipt,
            completion_policy=completion_policy,
            workspace=str(self.config.workspace.resolve()),
        )
        if existing is not None:
            job_id, existing_harness = existing
            return job_id, False, existing_harness
        selected = harness or self._select_worker_harness(title, prompt, dedupe_key)
        if selected not in self.worker_harnesses:
            raise ValueError(f"harness_has_no_worker: {selected.value}")
        if self.health is not None and harness is not None:
            self.health.require(
                selected,
                role="worker",
                probe=self.readiness_probe,
                static_reason=self.health.static_reason(selected),
            )
        job_id, created = self.store.enqueue(
            NewJob(
                workflow=self.config.name,
                workspace=str(self.config.workspace.resolve()),
                title=title,
                harness=selected,
                prompt=prompt,
                dedupe_key=dedupe_key,
                max_attempts=self.config.coordinator.max_attempts,
                placement=self._static_placement(
                    title,
                    prompt,
                    selected,
                    placement,
                ),
                receipt=receipt,
                completion_policy=completion_policy,
            )
        )
        if not created:
            existing = self.store.existing_job(self.config.name, dedupe_key)
            if existing is None:
                raise ValueError("dedupe_lookup_failed")
            job_id, selected = existing
        return job_id, created, selected

    def run_once(
        self,
        *,
        dispatch_deadline: float | None = None,
    ) -> dict[str, object]:
        self.initialize()
        health_snapshot = (
            self._health_snapshot(
                refresh=True,
                dispatch_deadline=dispatch_deadline,
                refresh_harnesses=self._refresh_harnesses_for_run(),
            )
            if self.health is not None
            else None
        )
        self._last_health_snapshot = health_snapshot
        self._run_planner_if_due(
            dispatch_deadline,
            health_snapshot=health_snapshot,
        )
        self._assign_pending_placements(
            dispatch_deadline,
            health_snapshot=health_snapshot,
        )
        eligible_workers = self._eligible_worker_harnesses(
            dispatch_deadline=dispatch_deadline,
            health_snapshot=health_snapshot,
        )
        batch_key = f"run-{time.time_ns()}"
        slot_names = self._slot_names()
        jobs = self.store.claim(
            self.config.name,
            limit=self.config.coordinator.max_parallel,
            lease_seconds=self.config.coordinator.lease_seconds,
            slot_names=slot_names,
            slot_limits={worker.harness.value: worker.replicas for worker in self.config.workers},
            allowed_harnesses=eligible_workers,
            workspace=str(self.config.workspace.resolve()),
            require_fresh_health=self.health is not None,
            include_legacy=True,
        )
        results = {state.value: 0 for state in JobState}
        if not jobs:
            return self._run_report(
                results,
                claimed=0,
                health_snapshot=health_snapshot,
            )
        for job in jobs:
            if not job.recovery:
                self._observe_transition(job, AttemptPhase.CLAIMED)
        with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="harness") as executor:
            futures = {
                executor.submit(
                    self._dispatch_job,
                    job,
                    batch_key,
                    dispatch_deadline,
                ): job
                for job in jobs
            }
            for future in as_completed(futures):
                job = futures[future]
                try:
                    outcome = future.result()
                except OperationInterrupted:
                    raise
                except TransportError as exc:
                    outcome = DispatchOutcome(
                        agent_name=job.agent_name,
                        state=(
                            AgentState.BLOCKED
                            if exc.code == "agent_blocked"
                            else AgentState.UNKNOWN
                        ),
                        member_reused=False,
                        pane_id=None,
                        error_code=exc.code,
                        placement=job.placement,
                        error_summary=exc.summary,
                        agent_settled=exc.agent_settled,
                        correlation_id=job.correlation_id,
                    )
                except Exception:
                    outcome = DispatchOutcome(
                        agent_name=job.agent_name,
                        state=AgentState.UNKNOWN,
                        member_reused=False,
                        pane_id=None,
                        error_code="dispatcher_unhandled_error",
                        placement=job.placement,
                        correlation_id=job.correlation_id,
                    )
                state = self.store.record_outcome(job, outcome)
                self._record_health(job.harness, outcome)
                self._observe_transition(job, self.store.attempt_phase(job.attempt_id))
                results[state.value] += 1
        return self._run_report(
            results,
            claimed=len(jobs),
            health_snapshot=health_snapshot,
        )

    def _record_attempt_progress(
        self,
        job: ClaimedJob,
        progress: AttemptProgress,
    ) -> None:
        self.store.record_attempt_progress(job, progress)
        self._observe_transition(job, progress.phase)

    def _observe_transition(self, job: ClaimedJob, phase: AttemptPhase) -> None:
        if self.transition_observer is None:
            return
        self.transition_observer(
            AttemptTransition(
                job.job_id,
                job.attempt_id,
                job.attempt,
                job.operation_sequence,
                phase,
            )
        )

    def _run_report(
        self,
        batch: dict[str, int],
        *,
        claimed: int,
        health_snapshot: EligibilitySnapshot | None = None,
    ) -> dict[str, object]:
        workspace = str(self.config.workspace.resolve())
        report: dict[str, object] = {
            **batch,
            "claimed": claimed,
            "batch": dict(batch),
            "queue": self.store.status_counts(
                self.config.name,
                workspace=workspace,
                include_legacy=True,
            ),
        }
        if self.health is not None:
            snapshot = health_snapshot or self._health_snapshot(refresh=False)
            report["harness_health"] = snapshot.public_json()
            report["deferred_jobs"] = self._deferred_jobs(snapshot)
        return report

    def run_until_idle(self, *, timeout_seconds: int) -> dict[str, object]:
        if timeout_seconds < 1:
            raise ValueError("drain_timeout_must_be_positive")
        self.initialize()
        deadline = time.monotonic() + timeout_seconds
        aggregate = {state.value: 0 for state in JobState}
        total_claimed = 0
        waves = 0
        workspace = str(self.config.workspace.resolve())
        last_queue = self.store.status_counts(
            self.config.name,
            workspace=workspace,
            include_legacy=True,
        )
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._drain_report(
                    aggregate,
                    idle=False,
                    reason="drain_timeout",
                    waves=waves,
                    claimed=total_claimed,
                    queue=last_queue,
                )
            try:
                report = self.run_once(dispatch_deadline=deadline)
            except _DispatchDeadlineExceeded:
                last_queue = self.store.status_counts(
                    self.config.name,
                    workspace=workspace,
                    include_legacy=True,
                )
                active = self.store.status_counts(
                    self.config.name,
                    allowed_harnesses=self.worker_harnesses,
                    workspace=workspace,
                    include_legacy=True,
                )
                return self._drain_report(
                    aggregate,
                    idle=False,
                    reason="drain_timeout",
                    waves=waves,
                    claimed=total_claimed,
                    queue=last_queue,
                    worker_pool_idle=_queue_is_idle(active),
                    queue_idle=_queue_is_idle(last_queue),
                    health_snapshot=self._last_health_snapshot,
                )
            waves += 1
            claimed = _integer(report["claimed"])
            total_claimed += claimed
            batch = report["batch"]
            assert isinstance(batch, dict)
            for state in JobState:
                aggregate[state.value] += _integer(batch[state.value])
            queue = report["queue"]
            assert isinstance(queue, dict)
            last_queue = {str(key): _integer(value) for key, value in queue.items()}
            active = self.store.status_counts(
                self.config.name,
                allowed_harnesses=self.worker_harnesses,
                workspace=workspace,
                include_legacy=True,
            )
            worker_pool_idle = _queue_is_idle(active)
            queue_idle = _queue_is_idle(last_queue)
            if active[JobState.BLOCKED.value] > 0:
                return self._drain_report(
                    aggregate,
                    idle=False,
                    reason="blocked",
                    waves=waves,
                    claimed=total_claimed,
                    queue=last_queue,
                    worker_pool_idle=False,
                    queue_idle=queue_idle,
                )
            if self._capacity_degraded(
                last_queue,
                dispatch_deadline=deadline,
                health_snapshot=self._last_health_snapshot,
            ):
                return self._drain_report(
                    aggregate,
                    idle=False,
                    reason="degraded_capacity",
                    waves=waves,
                    claimed=total_claimed,
                    queue=last_queue,
                    worker_pool_idle=False,
                    queue_idle=queue_idle,
                    health_snapshot=self._last_health_snapshot,
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._drain_report(
                    aggregate,
                    idle=False,
                    reason="drain_timeout",
                    waves=waves,
                    claimed=total_claimed,
                    queue=last_queue,
                    worker_pool_idle=worker_pool_idle,
                    queue_idle=queue_idle,
                    health_snapshot=self._last_health_snapshot,
                )
            if worker_pool_idle:
                return self._drain_report(
                    aggregate,
                    idle=True,
                    reason="queue_idle" if queue_idle else "worker_pool_idle",
                    waves=waves,
                    claimed=total_claimed,
                    queue=last_queue,
                    worker_pool_idle=True,
                    queue_idle=queue_idle,
                    health_snapshot=self._last_health_snapshot,
                )
            if claimed == 0:
                time.sleep(min(self.config.coordinator.poll_seconds, remaining))

    def resume_blocked(
        self,
        job_id: int,
        response: str,
    ) -> dict[str, object]:
        if not response.strip():
            raise ValueError("blocked_response_empty")
        responder = getattr(self.dispatcher, "respond", None)
        if not callable(responder):
            raise ValueError("dispatcher_resume_unsupported")
        self.initialize()
        job, expected_pane_id = self.store.claim_blocked_for_resume(
            self.config.name,
            job_id,
            lease_seconds=self.config.coordinator.lease_seconds,
        )
        if not job.recovery:
            self._observe_transition(job, AttemptPhase.CLAIMED)
        context = DispatchContext(
            placement=job.placement,
            title=job.title,
            task_key=job.dedupe_key,
            batch_key=f"resume-{job.job_id}-{job.attempt}-{job.operation_sequence}",
            worktree_root=self.config.placement.worktree_root,
            receipt=job.receipt,
            correlation_id=job.correlation_id,
            attempt_progress=lambda progress: self._record_attempt_progress(job, progress),
            completion_identity=_completion_identity(job),
        )
        try:
            if job.recovery:
                outcome = self._recover_job(
                    job,
                    response,
                    timeout_seconds=self.config.coordinator.agent_timeout_seconds,
                    context=context,
                )
            else:
                outcome = responder(
                    job.agent_name,
                    job.harness,
                    response,
                    timeout_seconds=self.config.coordinator.agent_timeout_seconds,
                    expected_pane_id=expected_pane_id,
                    context=context,
                )
            outcome = replace(outcome, correlation_id=job.correlation_id)
        except OperationInterrupted:
            raise
        except TransportError as exc:
            outcome = DispatchOutcome(
                agent_name=job.agent_name,
                state=AgentState.BLOCKED if exc.code == "agent_blocked" else AgentState.UNKNOWN,
                member_reused=True,
                pane_id=expected_pane_id,
                error_code=exc.code,
                placement=job.placement,
                error_summary=exc.summary,
                agent_settled=exc.agent_settled,
                correlation_id=job.correlation_id,
            )
        except Exception:
            outcome = DispatchOutcome(
                agent_name=job.agent_name,
                state=AgentState.UNKNOWN,
                member_reused=True,
                pane_id=expected_pane_id,
                error_code="resume_unhandled_error",
                placement=job.placement,
                correlation_id=job.correlation_id,
            )
        state = self.store.record_resume_outcome(job, outcome)
        self._record_health(job.harness, outcome)
        self._observe_transition(job, self.store.attempt_phase(job.attempt_id))
        return {
            "job_id": job.job_id,
            "state": state.value,
            "attempt": job.attempt,
            "agent_name": job.agent_name,
            "pane_id": expected_pane_id,
            "agent_state": outcome.state.value,
            "error_code": outcome.error_code,
            "agent_settled": outcome.agent_settled,
            "task_verified": outcome.task_verified,
            "queue": self.store.status_counts(self.config.name),
        }

    def _drain_report(
        self,
        aggregate: dict[str, int],
        *,
        idle: bool,
        reason: str,
        waves: int,
        claimed: int,
        queue: dict[str, int],
        worker_pool_idle: bool = False,
        queue_idle: bool = False,
        health_snapshot: EligibilitySnapshot | None = None,
    ) -> dict[str, object]:
        report: dict[str, object] = {
            **aggregate,
            "mode": "until_idle",
            "scope": "worker_pool",
            "idle": idle,
            "worker_pool_idle": worker_pool_idle,
            "queue_idle": queue_idle,
            "reason": reason,
            "waves": waves,
            "claimed": claimed,
            "batch": dict(aggregate),
            "queue": queue,
        }
        if self.health is not None:
            snapshot = health_snapshot or self._health_snapshot(refresh=False)
            report["harness_health"] = snapshot.public_json()
            report["deferred_jobs"] = self._deferred_jobs(snapshot)
        return report

    def gc_succeeded_agents(self, *, dry_run: bool = True) -> dict[str, object]:
        return self._gc_agents({JobState.SUCCEEDED}, dry_run=dry_run)

    def gc_failed_agents(self, *, dry_run: bool = True) -> dict[str, object]:
        return self._gc_agents({JobState.FAILED}, dry_run=dry_run)

    def _gc_agents(
        self,
        target_states: set[JobState],
        *,
        dry_run: bool,
    ) -> dict[str, object]:
        if not target_states or not target_states <= {
            JobState.SUCCEEDED,
            JobState.FAILED,
        }:
            raise ValueError("gc_states_invalid")
        self.initialize()
        rows = self.store.jobs(self.config.name)
        target_values = {state.value for state in target_states}
        created_panes = self.store.created_agent_panes(self.config.name)
        owned_names = {
            name
            for worker in self.config.workers
            for placement in (PlacementTarget.TAB, PlacementTarget.PANE)
            for name in replica_slot_names(
                self.config.name,
                self.config.workspace,
                worker.harness,
                worker.replicas,
                placement,
            )
        }
        active_names = {
            str(row["agent_name"])
            for row in rows
            if row["state"] not in target_values and row["agent_name"]
        }
        candidates_by_name: dict[str, dict[str, object]] = {}
        skipped_worktrees = 0
        skipped_unowned = 0
        skipped_active = 0
        skipped_blocked = 0
        for row in rows:
            if row["state"] == JobState.BLOCKED.value:
                skipped_blocked += 1
            if row["state"] not in target_values:
                continue
            if row["placement"] == PlacementTarget.WORKTREE.value:
                skipped_worktrees += 1
                continue
            name = row["agent_name"]
            if not isinstance(name, str) or name not in owned_names or name not in created_panes:
                skipped_unowned += 1
                continue
            if name in active_names:
                skipped_active += 1
                continue
            candidates_by_name[name] = {
                "job_id": _integer(row["id"]),
                "agent_name": name,
                "placement": str(row["placement"]),
                "pane_id": created_panes[name],
                "state": str(row["state"]),
            }
        closer = getattr(self.dispatcher, "close_agent_terminal", None)
        if not callable(closer):
            raise ValueError("dispatcher_cleanup_unsupported")
        candidates = list(candidates_by_name.values())
        actions = [
            closer(
                str(candidate["agent_name"]),
                PlacementTarget(str(candidate["placement"])),
                expected_pane_id=str(candidate["pane_id"]),
                dry_run=dry_run,
            )
            for candidate in candidates
        ]
        return {
            "dry_run": dry_run,
            "candidate_count": len(candidates),
            "candidates": candidates,
            "actions": actions,
            "states": sorted(target_values),
            "skipped_blocked": skipped_blocked,
            "skipped_worktrees": skipped_worktrees,
            "skipped_unowned": skipped_unowned,
            "skipped_active": skipped_active,
        }

    def run_forever(self) -> None:
        self.initialize()
        while True:
            self.run_once()
            time.sleep(self.config.coordinator.poll_seconds)

    def _health_snapshot(
        self,
        *,
        refresh: bool,
        dispatch_deadline: float | None = None,
        harnesses: Iterable[Harness] | None = None,
        refresh_harnesses: Iterable[Harness] | None = None,
    ) -> EligibilitySnapshot:
        if self.health is None:
            raise ValueError("harness_health_not_configured")
        selected = self._health_harnesses if harnesses is None else tuple(harnesses)
        return self.health.snapshot(
            selected,
            refresh=refresh,
            probe=self.readiness_probe,
            timeout_seconds=self._health_timeout_seconds(dispatch_deadline),
            deadline=dispatch_deadline,
            refresh_harnesses=refresh_harnesses,
        )

    def _refresh_harnesses_for_run(self) -> tuple[Harness, ...]:
        workspace = str(self.config.workspace.resolve())
        pending = set(
            self.store.pending_harnesses(
                self.config.name,
                workspace=workspace,
                include_legacy=True,
            )
        )
        if not pending:
            return ()
        if self.config.planner.enabled or self.store.unplaced_jobs(
            self.config.name,
            allowed_harnesses=self.worker_harnesses,
            workspace=workspace,
            include_legacy=True,
        ):
            return self._health_harnesses
        return tuple(harness for harness in self._health_harnesses if harness in pending)

    def _eligible_worker_harnesses(
        self,
        *,
        dispatch_deadline: float | None = None,
        health_snapshot: EligibilitySnapshot | None = None,
    ) -> tuple[Harness, ...]:
        if self.health is None:
            return self.worker_harnesses
        snapshot = health_snapshot or self._health_snapshot(
            refresh=True,
            dispatch_deadline=dispatch_deadline,
        )
        self.health.record_selection(snapshot, role="worker")
        return eligible_worker_harnesses(
            self.config,
            self.worker_harnesses,
            health=self.health,
            health_snapshot=snapshot,
        )

    def _health_timeout_seconds(self, dispatch_deadline: float | None) -> int | None:
        if self.health is None or dispatch_deadline is None:
            return None
        remaining = dispatch_deadline - time.monotonic()
        if remaining < 5:
            return 0
        return min(self.health.probe_timeout_seconds, int(remaining))

    def _capacity_degraded(
        self,
        queue: dict[str, int],
        *,
        dispatch_deadline: float | None = None,
        health_snapshot: EligibilitySnapshot | None = None,
    ) -> bool:
        if self.health is None or queue.get(JobState.PENDING.value, 0) <= 0:
            return False
        pending = set(
            self.store.pending_harnesses(
                self.config.name,
                workspace=str(self.config.workspace.resolve()),
                include_legacy=True,
            )
        ) & set(self.worker_harnesses)
        if not pending:
            return False
        eligible = self._eligible_worker_harnesses(
            dispatch_deadline=dispatch_deadline,
            health_snapshot=health_snapshot,
        )
        return not (pending & set(eligible))

    def _record_health(self, harness: Harness, outcome: DispatchOutcome) -> None:
        if self.health is not None:
            self.health.record_dispatch(harness, outcome)

    def _record_dispatch_exception(self, harness: Harness, error: Exception) -> None:
        code = getattr(error, "code", None)
        if not isinstance(code, str) or not code:
            code = "dispatcher_unhandled_error"
        self._record_health(
            harness,
            DispatchOutcome(
                agent_name="coordinator",
                state=AgentState.BLOCKED if code == "agent_blocked" else AgentState.UNKNOWN,
                member_reused=False,
                pane_id=None,
                error_code=code,
                agent_settled=getattr(error, "agent_settled", None),
            ),
        )

    def _deferred_jobs(self, snapshot: EligibilitySnapshot) -> list[dict[str, object]]:
        records = {record.harness: record for record in snapshot.records}
        deferred: list[dict[str, object]] = []
        for job in self.store.jobs(
            self.config.name,
            workspace=str(self.config.workspace.resolve()),
            include_legacy=True,
        ):
            if job["state"] != JobState.PENDING.value:
                continue
            harness = Harness(str(job["harness"]))
            record = records.get(harness)
            if record is not None and not record.eligible_at(snapshot.evaluated_at):
                deferred.append(
                    {
                        "job_id": _integer(job["id"]),
                        "harness": harness.value,
                        "reason": record.reason,
                    }
                )
        return deferred

    def _dispatch_job(
        self,
        job: ClaimedJob,
        batch_key: str,
        dispatch_deadline: float | None = None,
    ) -> DispatchOutcome:
        started = time.monotonic()
        self.observability.event(
            "dispatch_started",
            correlation_id=job.correlation_id,
            fields={
                "attempt": job.attempt,
                "harness": job.harness.value,
                "job_id": job.job_id,
                "placement": job.placement.value,
            },
        )
        try:
            outcome = self._dispatch_job_turn(job, batch_key, dispatch_deadline)
        except OperationInterrupted:
            raise
        except TransportError as exc:
            outcome = DispatchOutcome(
                agent_name=job.agent_name,
                state=AgentState.BLOCKED if exc.code == "agent_blocked" else AgentState.UNKNOWN,
                member_reused=False,
                pane_id=None,
                error_code=exc.code,
                placement=job.placement,
                error_summary=exc.summary,
                agent_settled=exc.agent_settled,
                correlation_id=job.correlation_id,
            )
        except _DispatchDeadlineExceeded:
            outcome = DispatchOutcome(
                agent_name=job.agent_name,
                state=AgentState.UNKNOWN,
                member_reused=False,
                pane_id=None,
                error_code="herdr_timeout",
                placement=job.placement,
                correlation_id=job.correlation_id,
            )
        except Exception:
            outcome = DispatchOutcome(
                agent_name=job.agent_name,
                state=AgentState.UNKNOWN,
                member_reused=False,
                pane_id=None,
                error_code="dispatcher_unhandled_error",
                placement=job.placement,
                correlation_id=job.correlation_id,
            )
        duration = time.monotonic() - started
        fields = {
            "attempt": job.attempt,
            "error_code": outcome.error_code,
            "harness": job.harness.value,
            "job_id": job.job_id,
            "state": outcome.state.value,
        }
        self.observability.event(
            "dispatch_finished",
            correlation_id=job.correlation_id,
            fields=fields,
        )
        self.observability.metric(
            "dispatch_duration_seconds",
            duration,
            correlation_id=job.correlation_id,
            fields=fields,
        )
        if outcome.error_code is not None or outcome.state is AgentState.BLOCKED:
            self.observability.alert(
                "dispatch_needs_attention",
                correlation_id=job.correlation_id,
                fields=fields,
            )
        return outcome

    def _dispatch_job_turn(
        self,
        job: ClaimedJob,
        batch_key: str,
        dispatch_deadline: float | None,
    ) -> DispatchOutcome:
        profile = profile_for_harness(self.config.profiles, job.harness)
        timeout_seconds = self._dispatch_timeout(dispatch_deadline)
        completion_identity = _completion_identity(job)
        task_prompt = (
            structured_completion_prompt(job.prompt, completion_identity)
            if completion_identity is not None
            else job.prompt
        )
        prompt = execution_prompt(profile, task_prompt)
        context = DispatchContext(
            placement=job.placement,
            title=job.title,
            task_key=job.dedupe_key,
            batch_key=batch_key,
            worktree_root=self.config.placement.worktree_root,
            receipt=job.receipt,
            correlation_id=job.correlation_id,
            attempt_progress=lambda progress: self._record_attempt_progress(job, progress),
            completion_identity=completion_identity,
        )
        if job.recovery:
            outcome = self._recover_job(
                job,
                prompt,
                timeout_seconds=timeout_seconds,
                context=context,
            )
        else:
            outcome = self.dispatcher.dispatch(
                job.harness,
                prompt,
                timeout_seconds=timeout_seconds,
                agent_name=job.agent_name,
                context=context,
            )
        return replace(outcome, correlation_id=job.correlation_id)

    def _recover_job(
        self,
        job: ClaimedJob,
        prompt: str,
        *,
        timeout_seconds: float,
        context: DispatchContext,
    ) -> DispatchOutcome:
        runtime = job.runtime
        if runtime is None:
            return DispatchOutcome(
                agent_name=job.agent_name,
                state=AgentState.UNKNOWN,
                member_reused=True,
                pane_id=runtime.pane_id if runtime is not None else None,
                error_code="lease_expired_unaccepted",
                placement=job.placement,
            )
        recoverer = getattr(self.dispatcher, "recover", None)
        if not callable(recoverer):
            return DispatchOutcome(
                agent_name=job.agent_name,
                state=AgentState.UNKNOWN,
                member_reused=True,
                pane_id=runtime.pane_id,
                error_code="unsafe_turn_adoption",
                placement=job.placement,
                execution_path=runtime.execution_path,
                herdr_workspace_id=runtime.herdr_workspace_id,
            )
        outcome = recoverer(
            job.harness,
            prompt,
            timeout_seconds=timeout_seconds,
            agent_name=job.agent_name,
            context=context,
            runtime=runtime,
        )
        if not isinstance(outcome, DispatchOutcome):
            raise ValueError("dispatcher_recovery_invalid")
        return outcome

    def _slot_names(self) -> dict[str, tuple[str, ...]]:
        names: dict[str, tuple[str, ...]] = {}
        for worker in self.config.workers:
            for placement in (PlacementTarget.TAB, PlacementTarget.PANE):
                names[f"{worker.harness.value}:{placement.value}"] = replica_slot_names(
                    self.config.name,
                    self.config.workspace,
                    worker.harness,
                    worker.replicas,
                    placement,
                )
        for row in self.store.jobs(
            self.config.name,
            workspace=str(self.config.workspace.resolve()),
            include_legacy=True,
        ):
            if row["placement"] != PlacementTarget.WORKTREE.value:
                continue
            job_id = _integer(row["id"])
            harness = Harness(str(row["harness"]))
            names[f"{harness.value}:worktree:{job_id}"] = (
                worktree_agent_name(self.config.name, harness, job_id),
            )
        return names

    def _assign_pending_placements(
        self,
        dispatch_deadline: float | None,
        *,
        health_snapshot: EligibilitySnapshot | None = None,
    ) -> None:
        eligible = set(
            self._eligible_worker_harnesses(
                dispatch_deadline=dispatch_deadline,
                health_snapshot=health_snapshot,
            )
        )
        for row in self.store.unplaced_jobs(
            self.config.name,
            allowed_harnesses=self.worker_harnesses,
            workspace=str(self.config.workspace.resolve()),
            include_legacy=True,
        ):
            self._dispatch_timeout(dispatch_deadline)
            harness = Harness(str(row["harness"]))
            if self.health is not None and harness not in eligible:
                continue
            placement = self._static_placement(
                str(row["title"]),
                str(row["prompt"]),
                harness,
                None,
            )
            if placement is None:
                placement = self._select_topology(
                    _integer(row["id"]),
                    str(row["title"]),
                    str(row["prompt"]),
                    str(row["dedupe_key"]),
                    dispatch_deadline,
                    health_snapshot=health_snapshot,
                )
            self.store.set_placement(_integer(row["id"]), placement)

    def _static_placement(
        self,
        title: str,
        prompt: str,
        harness: Harness,
        override: PlacementTarget | None,
    ) -> PlacementTarget | None:
        worker = next(worker for worker in self.config.workers if worker.harness is harness)
        return static_placement(
            self.config.placement.mode,
            title,
            prompt,
            override=override,
            worker_default=worker.placement,
            supports_worktree=self._supports_worktree(),
        )

    def _select_topology(
        self,
        job_id: int,
        title: str,
        prompt: str,
        dedupe_key: str,
        dispatch_deadline: float | None = None,
        *,
        health_snapshot: EligibilitySnapshot | None = None,
    ) -> PlacementTarget:
        controller = self._controller_harness(
            dispatch_deadline,
            health_snapshot=health_snapshot,
        )
        digest = hashlib.sha256(f"{self.config.name}\0topology\0{dedupe_key}".encode()).hexdigest()[
            :12
        ]
        output_file = self.config.planner.output_file.parent / f"topology-{digest}.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.unlink(missing_ok=True)
        try:
            try:
                outcome = self.dispatcher.dispatch(
                    controller,
                    topology_decision_prompt(
                        title,
                        prompt,
                        output_file,
                        supports_worktree=self._supports_worktree(),
                    ),
                    timeout_seconds=self._dispatch_timeout(dispatch_deadline),
                    agent_name=_controller_agent_name(
                        self.config.name,
                        self.config.workspace,
                        controller,
                    ),
                    context=DispatchContext(
                        PlacementTarget.TAB,
                        "Topology decision",
                        f"topology:{job_id}",
                    ),
                )
            except Exception as exc:
                self._record_dispatch_exception(controller, exc)
                raise
            self._record_health(controller, outcome)
            self._dispatch_timeout(dispatch_deadline)
            if outcome.error_code is not None or outcome.state not in {
                AgentState.IDLE,
                AgentState.DONE,
            }:
                raise ValueError(
                    f"topology_selection_failed:{outcome.error_code or outcome.state.value}"
                )
            return load_topology_decision(
                output_file,
                supports_worktree=self._supports_worktree(),
            )
        except TopologyDecisionError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            output_file.unlink(missing_ok=True)

    def _supports_worktree(self) -> bool:
        return (self.config.workspace / ".git").exists()

    def _select_worker_harness(
        self,
        title: str,
        prompt: str,
        dedupe_key: str,
        dispatch_deadline: float | None = None,
        *,
        health_snapshot: EligibilitySnapshot | None = None,
    ) -> Harness:
        if self.health is not None and health_snapshot is None:
            health_snapshot = self._health_snapshot(
                refresh=True,
                dispatch_deadline=dispatch_deadline,
            )
        controller = self._controller_harness(
            dispatch_deadline,
            health_snapshot=health_snapshot,
        )
        controller_name = _controller_agent_name(
            self.config.name,
            self.config.workspace,
            controller,
        )
        allowed_harnesses = self._eligible_worker_harnesses(
            dispatch_deadline=dispatch_deadline,
            health_snapshot=health_snapshot,
        )
        if not allowed_harnesses:
            snapshot = self._health_snapshot(refresh=False)
            reasons = ",".join(
                f"{harness.value}={snapshot.record_for(harness).reason}"
                for harness in self.worker_harnesses
            )
            raise ValueError(f"worker_harness_unavailable:{reasons}")
        profiles = self._worker_profiles(allowed_harnesses)
        digest = hashlib.sha256(f"{self.config.name}\0{dedupe_key}".encode()).hexdigest()[:12]
        output_file = self.config.planner.output_file.parent / f"route-{digest}.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.unlink(missing_ok=True)
        outcome: DispatchOutcome | None = None
        try:
            try:
                outcome = self.dispatcher.dispatch(
                    controller,
                    worker_selection_prompt(
                        f"Title: {title}\n\nPrompt:\n{prompt}",
                        output_file,
                        render_compact_catalog(profiles),
                        allowed_harnesses,
                    ),
                    timeout_seconds=self._dispatch_timeout(dispatch_deadline),
                    agent_name=controller_name,
                    context=DispatchContext(
                        PlacementTarget.TAB,
                        "Worker routing",
                        f"route:{dedupe_key}",
                    ),
                )
            except Exception as exc:
                self._record_dispatch_exception(controller, exc)
                raise
            self._record_health(controller, outcome)
            if outcome.error_code is not None or outcome.state not in {
                AgentState.IDLE,
                AgentState.DONE,
            }:
                raise ValueError(
                    f"worker_selection_failed:{outcome.error_code or outcome.state.value}"
                )
            return load_worker_selection(
                output_file,
                allowed_harnesses=allowed_harnesses,
            )
        except PlannerOutputError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            output_file.unlink(missing_ok=True)
            self._close_ephemeral_controller(controller_name, outcome)

    def _close_ephemeral_controller(
        self,
        controller_name: str,
        outcome: DispatchOutcome | None,
    ) -> None:
        if outcome is None or outcome.member_reused:
            return
        closer = getattr(self.dispatcher, "close_created_agent", None)
        if not callable(closer):
            raise ValueError("dispatcher_controller_cleanup_unsupported")
        closer(controller_name)

    def _controller_harness(
        self,
        dispatch_deadline: float | None = None,
        *,
        health_snapshot: EligibilitySnapshot | None = None,
    ) -> Harness:
        health_timeout_seconds = self._health_timeout_seconds(dispatch_deadline)
        return select_controller_harness(
            self.config,
            worker_harnesses=self.worker_harnesses,
            override=self.controller_harness,
            force_auto=self.controller_auto,
            health=self.health,
            readiness_probe=self.readiness_probe,
            health_timeout_seconds=health_timeout_seconds,
            health_deadline=dispatch_deadline,
            health_snapshot=health_snapshot,
        )

    def _worker_profiles(
        self,
        harnesses: Iterable[Harness] | None = None,
    ) -> tuple[HarnessProfile, ...]:
        selected = self.worker_harnesses if harnesses is None else tuple(harnesses)
        return tuple(profile_for_harness(self.config.profiles, harness) for harness in selected)

    def _run_planner_if_due(
        self,
        dispatch_deadline: float | None = None,
        *,
        health_snapshot: EligibilitySnapshot | None = None,
    ) -> None:
        planner = self.config.planner
        if not planner.enabled:
            return
        allowed_harnesses = self._eligible_worker_harnesses(
            dispatch_deadline=dispatch_deadline,
            health_snapshot=health_snapshot,
        )
        if not allowed_harnesses:
            return
        controller = self._controller_harness(
            dispatch_deadline,
            health_snapshot=health_snapshot,
        )
        if not self.store.reserve_planner_run(
            self.config.name,
            planner.interval_seconds,
        ):
            return
        planner.output_file.parent.mkdir(parents=True, exist_ok=True)
        planner.output_file.unlink(missing_ok=True)
        profiles = self._worker_profiles(allowed_harnesses)
        try:
            outcome = self.dispatcher.dispatch(
                controller,
                planner_prompt(
                    planner.prompt_file.read_text(encoding="utf-8"),
                    planner.output_file,
                    planner.max_tasks,
                    render_compact_catalog(profiles),
                    allowed_harnesses,
                ),
                timeout_seconds=self._dispatch_timeout(dispatch_deadline),
                agent_name=_controller_agent_name(
                    self.config.name,
                    self.config.workspace,
                    controller,
                ),
                context=DispatchContext(
                    PlacementTarget.TAB,
                    "Planner",
                    f"planner:{self.config.name}",
                ),
            )
        except Exception as exc:
            self._record_dispatch_exception(controller, exc)
            raise
        self._record_health(controller, outcome)
        self._dispatch_timeout(dispatch_deadline)
        if outcome.error_code is not None or outcome.state not in {
            AgentState.IDLE,
            AgentState.DONE,
        }:
            return
        try:
            tasks = load_planner_tasks(planner.output_file, max_tasks=planner.max_tasks)
        except PlannerOutputError:
            return
        allowed_set = set(allowed_harnesses)
        if any(task.harness not in allowed_set for task in tasks):
            return
        for task in tasks:
            self.store.enqueue(
                NewJob(
                    workflow=self.config.name,
                    workspace=str(self.config.workspace.resolve()),
                    title=task.title,
                    harness=task.harness,
                    prompt=task.prompt,
                    dedupe_key=task.dedupe_key,
                    max_attempts=self.config.coordinator.max_attempts,
                    placement=self._static_placement(
                        task.title,
                        task.prompt,
                        task.harness,
                        None,
                    ),
                )
            )

    def _dispatch_timeout(self, dispatch_deadline: float | None) -> float:
        timeout_seconds = float(self.config.coordinator.agent_timeout_seconds)
        if dispatch_deadline is None:
            return timeout_seconds
        remaining = dispatch_deadline - time.monotonic()
        if remaining <= 0:
            raise _DispatchDeadlineExceeded
        return min(timeout_seconds, remaining)


def _completion_identity(job: ClaimedJob) -> CompletionIdentity | None:
    if job.completion_policy is not CompletionPolicy.STRUCTURED_V2:
        return None
    return CompletionIdentity(job.job_id, job.attempt, job.fencing_token)


def _controller_agent_name(
    workflow_name: str,
    workspace: Path,
    harness: Harness,
) -> str:
    digest = hashlib.sha256(
        f"{workflow_name}\0{workspace.resolve()}\0controller\0{harness.value}".encode()
    ).hexdigest()[:8]
    return f"ho-control-{harness.value}-{digest}"


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("integer_value_invalid")
    return int(value)


def _queue_is_idle(counts: dict[str, int]) -> bool:
    return all(
        counts[state.value] == 0 for state in (JobState.PENDING, JobState.RUNNING, JobState.BLOCKED)
    )
