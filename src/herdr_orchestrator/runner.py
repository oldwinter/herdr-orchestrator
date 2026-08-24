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
from herdr_orchestrator.herdr import HerdrTransport, replica_slot_names
from herdr_orchestrator.model import (
    AgentState,
    ClaimedJob,
    DispatchOutcome,
    Harness,
    HarnessProfile,
    JobState,
    NewJob,
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


class Dispatcher(Protocol):
    def dispatch(
        self,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str | None = None,
    ) -> DispatchOutcome: ...


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
            _, created = self.store.enqueue(
                NewJob(
                    workflow=self.config.name,
                    title=seed.title,
                    harness=seed.harness,
                    prompt=seed.prompt_file.read_text(encoding="utf-8").strip(),
                    dedupe_key=seed.dedupe_key,
                    max_attempts=self.config.coordinator.max_attempts,
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
            )
        )
        if not created:
            existing = self.store.existing_job(self.config.name, dedupe_key)
            if existing is None:
                raise ValueError("dedupe_lookup_failed")
            job_id, selected = existing
        return job_id, created, selected

    def run_once(self) -> dict[str, int]:
        self.initialize()
        self._run_planner_if_due()
        jobs = self.store.claim(
            self.config.name,
            limit=self.config.coordinator.max_parallel,
            lease_seconds=self.config.coordinator.lease_seconds,
            slot_names={
                worker.harness.value: replica_slot_names(
                    self.config.name,
                    self.config.workspace,
                    worker.harness,
                    worker.replicas,
                )
                for worker in self.config.workers
            },
            allowed_harnesses=self.worker_harnesses,
        )
        results = {state.value: 0 for state in JobState}
        if not jobs:
            return results
        with ThreadPoolExecutor(max_workers=len(jobs), thread_name_prefix="harness") as executor:
            futures = {executor.submit(self._dispatch_job, job): job for job in jobs}
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
        return results

    def run_forever(self) -> None:
        self.initialize()
        while True:
            self.run_once()
            time.sleep(self.config.coordinator.poll_seconds)

    def _dispatch_job(self, job: ClaimedJob) -> DispatchOutcome:
        profile = profile_for_harness(self.config.profiles, job.harness)
        return self.dispatcher.dispatch(
            job.harness,
            execution_prompt(profile, job.prompt),
            timeout_seconds=self.config.coordinator.agent_timeout_seconds,
            agent_name=job.agent_name,
        )

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

    def _run_planner_if_due(self) -> None:
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
            timeout_seconds=self.config.coordinator.agent_timeout_seconds,
            agent_name=_controller_agent_name(
                self.config.name,
                self.config.workspace,
                controller,
            ),
        )
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
                )
            )


def _controller_agent_name(
    workflow_name: str,
    workspace: Path,
    harness: Harness,
) -> str:
    digest = hashlib.sha256(
        f"{workflow_name}\0{workspace.resolve()}\0controller\0{harness.value}".encode()
    ).hexdigest()[:8]
    return f"ho-control-{harness.value}-{digest}"
