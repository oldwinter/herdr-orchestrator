from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.catalog import (
    execution_prompt,
    profile_for_harness,
    render_compact_catalog,
)
from herdr_orchestrator.herdr import (
    HerdrTransport,
    replica_slot_names,
    worktree_agent_name,
)
from herdr_orchestrator.model import (
    AgentState,
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
from herdr_orchestrator.planner import (
    PlannerOutputError,
    load_planner_tasks,
    load_worker_selection,
    planner_prompt,
    worker_selection_prompt,
)
from herdr_orchestrator.selection import (
    effective_worker_harnesses,
    select_controller_harness,
)
from herdr_orchestrator.store import Store
from herdr_orchestrator.topology import (
    TopologyDecisionError,
    load_topology_decision,
    static_placement,
    topology_decision_prompt,
)


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


class _DispatchDeadlineExceeded(RuntimeError):
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
    ) -> None:
        self.config = config
        self.store = store or Store(config.state_db)
        self.dispatcher = dispatcher or HerdrTransport(config.name, config.workspace)
        self.controller_harness = controller_harness
        self.controller_auto = controller_auto
        self.worker_harnesses = effective_worker_harnesses(config, worker_harnesses)

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
    ) -> tuple[int, bool, Harness]:
        self.initialize()
        if not prompt_file.is_file():
            raise ValueError(f"prompt_file_not_found: {prompt_file}")
        prompt = prompt_file.read_text(encoding="utf-8").strip()
        if not prompt:
            raise ValueError("prompt_file_empty")
        existing = self.store.existing_job(self.config.name, dedupe_key)
        if existing is not None:
            job_id, existing_harness = existing
            return job_id, False, existing_harness
        selected = harness or self._select_worker_harness(title, prompt, dedupe_key)
        if selected not in self.worker_harnesses:
            raise ValueError(f"harness_has_no_worker: {selected.value}")
        job_id, created = self.store.enqueue(
            NewJob(
                workflow=self.config.name,
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
        self._run_planner_if_due(dispatch_deadline)
        self._assign_pending_placements(dispatch_deadline)
        batch_key = f"run-{time.time_ns()}"
        slot_names = self._slot_names()
        jobs = self.store.claim(
            self.config.name,
            limit=self.config.coordinator.max_parallel,
            lease_seconds=self.config.coordinator.lease_seconds,
            slot_names=slot_names,
            slot_limits={
                worker.harness.value: worker.replicas
                for worker in self.config.workers
            },
            allowed_harnesses=self.worker_harnesses,
        )
        results = {state.value: 0 for state in JobState}
        if not jobs:
            return self._run_report(results, claimed=0)
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
                except Exception:
                    outcome = DispatchOutcome(
                        agent_name=f"failed-{job.harness.value}",
                        state=AgentState.UNKNOWN,
                        member_reused=False,
                        pane_id=None,
                        error_code="dispatcher_unhandled_error",
                    )
                state = self.store.record_outcome(job, outcome)
                results[state.value] += 1
        return self._run_report(results, claimed=len(jobs))

    def _run_report(
        self,
        batch: dict[str, int],
        *,
        claimed: int,
    ) -> dict[str, object]:
        return {
            **batch,
            "claimed": claimed,
            "batch": dict(batch),
            "queue": self.store.status_counts(self.config.name),
        }

    def run_until_idle(self, *, timeout_seconds: int) -> dict[str, object]:
        if timeout_seconds < 1:
            raise ValueError("drain_timeout_must_be_positive")
        self.initialize()
        deadline = time.monotonic() + timeout_seconds
        aggregate = {state.value: 0 for state in JobState}
        total_claimed = 0
        waves = 0
        last_queue = self.store.status_counts(self.config.name)
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
                last_queue = self.store.status_counts(self.config.name)
                active = self.store.status_counts(
                    self.config.name,
                    allowed_harnesses=self.worker_harnesses,
                )
                return self._drain_report(
                    aggregate,
                    idle=False,
                    reason="drain_timeout",
                    waves=waves,
                    claimed=total_claimed,
                    queue=last_queue,
                    worker_pool_idle=(
                        active[JobState.PENDING.value] == 0
                        and active[JobState.RUNNING.value] == 0
                    ),
                    queue_idle=(
                        last_queue[JobState.PENDING.value] == 0
                        and last_queue[JobState.RUNNING.value] == 0
                    ),
                )
            waves += 1
            claimed = int(report["claimed"])
            total_claimed += claimed
            batch = report["batch"]
            assert isinstance(batch, dict)
            for state in JobState:
                aggregate[state.value] += int(batch[state.value])
            queue = report["queue"]
            assert isinstance(queue, dict)
            last_queue = {str(key): int(value) for key, value in queue.items()}
            active = self.store.status_counts(
                self.config.name,
                allowed_harnesses=self.worker_harnesses,
            )
            worker_pool_idle = (
                active[JobState.PENDING.value] == 0
                and active[JobState.RUNNING.value] == 0
            )
            queue_idle = (
                last_queue[JobState.PENDING.value] == 0
                and last_queue[JobState.RUNNING.value] == 0
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
                )
            if claimed == 0:
                time.sleep(min(self.config.coordinator.poll_seconds, remaining))

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
    ) -> dict[str, object]:
        return {
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

    def gc_succeeded_agents(self, *, dry_run: bool = True) -> dict[str, object]:
        self.initialize()
        rows = self.store.jobs(self.config.name)
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
            if row["state"] != JobState.SUCCEEDED.value and row["agent_name"]
        }
        candidates_by_name: dict[str, dict[str, object]] = {}
        skipped_worktrees = 0
        skipped_unowned = 0
        skipped_active = 0
        for row in rows:
            if row["state"] != JobState.SUCCEEDED.value:
                continue
            if row["placement"] == PlacementTarget.WORKTREE.value:
                skipped_worktrees += 1
                continue
            name = row["agent_name"]
            if (
                not isinstance(name, str)
                or name not in owned_names
                or name not in created_panes
            ):
                skipped_unowned += 1
                continue
            if name in active_names:
                skipped_active += 1
                continue
            candidates_by_name[name] = {
                "job_id": int(row["id"]),
                "agent_name": name,
                "placement": str(row["placement"]),
                "pane_id": created_panes[name],
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
            "skipped_worktrees": skipped_worktrees,
            "skipped_unowned": skipped_unowned,
            "skipped_active": skipped_active,
        }

    def run_forever(self) -> None:
        self.initialize()
        while True:
            self.run_once()
            time.sleep(self.config.coordinator.poll_seconds)

    def _dispatch_job(
        self,
        job: ClaimedJob,
        batch_key: str,
        dispatch_deadline: float | None = None,
    ) -> DispatchOutcome:
        profile = profile_for_harness(self.config.profiles, job.harness)
        try:
            timeout_seconds = self._dispatch_timeout(dispatch_deadline)
        except _DispatchDeadlineExceeded:
            return DispatchOutcome(
                agent_name=job.agent_name,
                state=AgentState.UNKNOWN,
                member_reused=False,
                pane_id=None,
                error_code="herdr_timeout",
                placement=job.placement,
            )
        return self.dispatcher.dispatch(
            job.harness,
            execution_prompt(profile, job.prompt),
            timeout_seconds=timeout_seconds,
            agent_name=job.agent_name,
            context=DispatchContext(
                placement=job.placement,
                title=job.title,
                task_key=job.dedupe_key,
                batch_key=batch_key,
                worktree_root=self.config.placement.worktree_root,
                receipt=job.receipt,
            ),
        )

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
        for row in self.store.jobs(self.config.name):
            if row["placement"] != PlacementTarget.WORKTREE.value:
                continue
            job_id = int(row["id"])
            harness = Harness(str(row["harness"]))
            names[f"{harness.value}:worktree:{job_id}"] = (
                worktree_agent_name(self.config.name, harness, job_id),
            )
        return names

    def _assign_pending_placements(self, dispatch_deadline: float | None) -> None:
        for row in self.store.unplaced_jobs(
            self.config.name,
            allowed_harnesses=self.worker_harnesses,
        ):
            self._dispatch_timeout(dispatch_deadline)
            harness = Harness(str(row["harness"]))
            placement = self._static_placement(
                str(row["title"]),
                str(row["prompt"]),
                harness,
                None,
            )
            if placement is None:
                placement = self._select_topology(
                    int(row["id"]),
                    str(row["title"]),
                    str(row["prompt"]),
                    str(row["dedupe_key"]),
                    dispatch_deadline,
                )
            self.store.set_placement(int(row["id"]), placement)

    def _static_placement(
        self,
        title: str,
        prompt: str,
        harness: Harness,
        override: PlacementTarget | None,
    ) -> PlacementTarget | None:
        worker = next(
            worker for worker in self.config.workers if worker.harness is harness
        )
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
    ) -> PlacementTarget:
        controller = self._controller_harness()
        digest = hashlib.sha256(
            f"{self.config.name}\0topology\0{dedupe_key}".encode()
        ).hexdigest()[:12]
        output_file = self.config.planner.output_file.parent / f"topology-{digest}.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.unlink(missing_ok=True)
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
    ) -> Harness:
        controller = self._controller_harness()
        profiles = self._worker_profiles()
        digest = hashlib.sha256(
            f"{self.config.name}\0{dedupe_key}".encode()
        ).hexdigest()[:12]
        output_file = self.config.planner.output_file.parent / f"route-{digest}.json"
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.unlink(missing_ok=True)
        try:
            outcome = self.dispatcher.dispatch(
                controller,
                worker_selection_prompt(
                    f"Title: {title}\n\nPrompt:\n{prompt}",
                    output_file,
                    render_compact_catalog(profiles),
                    self.worker_harnesses,
                ),
                timeout_seconds=self.config.coordinator.agent_timeout_seconds,
                agent_name=_controller_agent_name(
                    self.config.name,
                    self.config.workspace,
                    controller,
                ),
                context=DispatchContext(
                    PlacementTarget.TAB,
                    "Worker routing",
                    f"route:{dedupe_key}",
                ),
            )
            if outcome.error_code is not None or outcome.state not in {
                AgentState.IDLE,
                AgentState.DONE,
            }:
                raise ValueError(
                    f"worker_selection_failed:{outcome.error_code or outcome.state.value}"
                )
            return load_worker_selection(
                output_file,
                allowed_harnesses=self.worker_harnesses,
            )
        except PlannerOutputError as exc:
            raise ValueError(str(exc)) from exc
        finally:
            output_file.unlink(missing_ok=True)

    def _controller_harness(self) -> Harness:
        return select_controller_harness(
            self.config,
            worker_harnesses=self.worker_harnesses,
            override=self.controller_harness,
            force_auto=self.controller_auto,
        )

    def _worker_profiles(self) -> tuple[HarnessProfile, ...]:
        return tuple(
            profile_for_harness(self.config.profiles, harness)
            for harness in self.worker_harnesses
        )

    def _run_planner_if_due(self, dispatch_deadline: float | None = None) -> None:
        planner = self.config.planner
        if not planner.enabled:
            return
        metadata_key = f"planner_last_attempt:{self.config.name}"
        now = time.time()
        last_attempt = self.store.metadata_float(metadata_key)
        if last_attempt is not None and now - last_attempt < planner.interval_seconds:
            return
        self.store.set_metadata_float(metadata_key, now)
        planner.output_file.parent.mkdir(parents=True, exist_ok=True)
        planner.output_file.unlink(missing_ok=True)
        controller = self._controller_harness()
        profiles = self._worker_profiles()
        outcome = self.dispatcher.dispatch(
            controller,
            planner_prompt(
                planner.prompt_file.read_text(encoding="utf-8"),
                planner.output_file,
                planner.max_tasks,
                render_compact_catalog(profiles),
                self.worker_harnesses,
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
        allowed_harnesses = set(self.worker_harnesses)
        if any(task.harness not in allowed_harnesses for task in tasks):
            return
        for task in tasks:
            self.store.enqueue(
                NewJob(
                    workflow=self.config.name,
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


def _controller_agent_name(
    workflow_name: str,
    workspace: Path,
    harness: Harness,
) -> str:
    digest = hashlib.sha256(
        f"{workflow_name}\0{workspace.resolve()}\0controller\0{harness.value}".encode()
    ).hexdigest()[:8]
    return f"ho-control-{harness.value}-{digest}"
