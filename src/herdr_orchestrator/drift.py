"""Optional pre-claim Git guard; observations use local refs and never fetch."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from herdr_orchestrator.herdr_layout import HerdrLayout
from herdr_orchestrator.model import DispatchContext, PlacementTarget, WorkflowConfig
from herdr_orchestrator.protocol import CommandRunner, subprocess_runner

REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}\Z")


@dataclass(frozen=True)
class BaseDriftGuard:
    base_ref: str
    max_behind: int
    runner: CommandRunner = subprocess_runner

    def __post_init__(self) -> None:
        if not REF.fullmatch(self.base_ref) or ".." in self.base_ref:
            raise ValueError("base_ref_invalid")
        if type(self.max_behind) is not int or not 0 <= self.max_behind <= 10000:
            raise ValueError("max_base_behind_invalid")

    def observe(self, path: Path, *, timeout_seconds: float = 5) -> dict[str, object]:
        observation: dict[str, object] = {
            "path": str(path),
            "base": self.base_ref,
            "source": "local_refs",
            "behind": None,
            "eligible": False,
            "reason": "base_drift_unknown",
        }
        if timeout_seconds <= 0:
            return observation
        try:
            process = self.runner(
                ["git", "rev-list", "--count", f"HEAD..{self.base_ref}", "--"],
                cwd=str(path),
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.TimeoutExpired):
            return observation
        value = process.stdout.strip()
        if process.returncode or not value.isascii() or not value.isdecimal():
            return observation
        behind = int(value)
        return observation | {
            "behind": behind,
            "eligible": behind <= self.max_behind,
            "reason": "base_current" if behind <= self.max_behind else "base_drift_exceeded",
        }

    def filter_jobs(
        self,
        config: WorkflowConfig,
        jobs: list[dict[str, object]],
        *,
        deadline: float | None = None,
    ) -> tuple[set[int], list[dict[str, object]]]:
        eligible: set[int] = set()
        deferred: list[dict[str, object]] = []
        observations: dict[Path, dict[str, object]] = {}
        layout = HerdrLayout(config.name, config.workspace, "", self.runner)
        for job in jobs:
            job_id = int(str(job["id"]))
            if job["state"] != "pending":
                eligible.add(job_id)
                continue
            if job["placement"] is None:
                continue
            target = layout.execution_workspace(
                DispatchContext(
                    PlacementTarget(str(job["placement"])),
                    str(job["title"]),
                    str(job["dedupe_key"]),
                    worktree_root=config.placement.worktree_root,
                )
            )
            # A not-yet-created worktree starts at the workflow checkout's HEAD.
            path = target if target.exists() else config.workspace
            if path not in observations:
                remaining = 5.0 if deadline is None else min(5, deadline - time.monotonic())
                observations[path] = self.observe(path, timeout_seconds=remaining)
            evidence = observations[path]
            if evidence["eligible"]:
                eligible.add(job_id)
            else:
                deferred.append({"job_id": job_id, **evidence})
        return eligible, deferred
