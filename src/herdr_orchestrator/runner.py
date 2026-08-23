from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.herdr import HerdrTransport
from herdr_orchestrator.model import (
    AgentState,
    ClaimedJob,
    DispatchOutcome,
    Harness,
    JobState,
    NewJob,
    WorkflowConfig,
)
from herdr_orchestrator.planner import PlannerOutputError, load_planner_tasks, planner_prompt
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
    ) -> None:
        self.config = config
        self.store = store or Store(config.state_db)
        self.dispatcher = dispatcher or HerdrTransport(config.name, config.workspace)

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
        harness: Harness,
        title: str,
        prompt_file: Path,
        dedupe_key: str,
    ) -> tuple[int, bool]:
        self.initialize()
        if harness not in {worker.harness for worker in self.config.workers}:
            raise ValueError(f"harness_has_no_worker: {harness.value}")
        if not prompt_file.is_file():
            raise ValueError(f"prompt_file_not_found: {prompt_file}")
        prompt = prompt_file.read_text(encoding="utf-8").strip()
        if not prompt:
            raise ValueError("prompt_file_empty")
        return self.store.enqueue(
            NewJob(
                workflow=self.config.name,
                title=title,
                harness=harness,
                prompt=prompt,
                dedupe_key=dedupe_key,
                max_attempts=self.config.coordinator.max_attempts,
            )
        )

    def run_once(self) -> dict[str, int]:
        self.initialize()
        self._run_planner_if_due()
        jobs = self.store.claim(
            self.config.name,
            limit=self.config.coordinator.max_parallel,
            lease_seconds=self.config.coordinator.lease_seconds,
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
        return self.dispatcher.dispatch(
            job.harness,
            job.prompt,
            timeout_seconds=self.config.coordinator.agent_timeout_seconds,
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
        outcome = self.dispatcher.dispatch(
            planner.harness,
            planner_prompt(
                planner.prompt_file.read_text(encoding="utf-8"),
                planner.output_file,
                planner.max_tasks,
            ),
            timeout_seconds=self.config.coordinator.agent_timeout_seconds,
            agent_name=f"planner-{planner.harness.value}-{self.config.name}"[:32],
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
        allowed_harnesses = {worker.harness for worker in self.config.workers}
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
