from __future__ import annotations

import time
from collections import Counter
from collections.abc import Callable, Mapping

from herdr_orchestrator.dashboard.observer import (
    HerdrObservation,
    HerdrObserver,
    QueueObservation,
    SqliteObserver,
)

TERMINAL_JOB_STATES = {"succeeded", "blocked", "failed"}
ACTIVE_AGENT_STATES = {"working", "blocked"}


class RuntimeProjector:
    def __init__(
        self,
        workflow: str,
        queue_observer: SqliteObserver,
        herdr_observer: HerdrObserver,
        *,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.workflow = workflow
        self.queue_observer = queue_observer
        self.herdr_observer = herdr_observer
        self.clock = clock

    def snapshot(self) -> dict[str, object]:
        queue = self.queue_observer.observe()
        herdr = self.herdr_observer.observe()
        generated_at = self.clock()
        agents_by_name = {
            str(row["name"]): row for row in herdr.agents if isinstance(row.get("name"), str)
        }
        jobs = [self._project_job(row, agents_by_name, generated_at) for row in queue.jobs]
        attention = _attention(jobs, herdr, generated_at)
        counts = Counter(str(job["state"]) for job in jobs)
        summary = {
            "total": len(jobs),
            "pending": counts["pending"],
            "running": counts["running"],
            "blocked": counts["blocked"],
            "failed": counts["failed"],
            "succeeded": counts["succeeded"],
            "active_agents": sum(
                str(agent.get("agent_status")) in ACTIVE_AGENT_STATES for agent in herdr.agents
            ),
            "worktrees": sum(row.get("is_linked_worktree") is True for row in herdr.worktrees),
            "needs_attention": len(attention),
        }
        return {
            "schema_version": 1,
            "workflow": self.workflow,
            "generated_at": generated_at,
            "source_health": {
                "queue": "ok",
                "herdr": herdr.health,
                "herdr_error": herdr.error_code,
            },
            "summary": summary,
            "jobs": jobs,
            "attention": attention,
            "topology": _topology(herdr, self.workflow),
            "timeline": _timeline(queue, jobs),
        }

    def _project_job(
        self,
        row: Mapping[str, object],
        agents_by_name: Mapping[str, Mapping[str, object]],
        generated_at: float,
    ) -> dict[str, object]:
        agent_name = row.get("agent_name")
        agent = agents_by_name.get(agent_name) if isinstance(agent_name, str) else None
        runtime = dict(agent) if agent is not None else None
        state = str(row["state"])
        drift: list[str] = []
        if state == "running" and runtime is None:
            drift.append("running_agent_missing")
        if (
            state in TERMINAL_JOB_STATES
            and runtime is not None
            and runtime.get("agent_status") == "working"
        ):
            drift.append("terminal_job_agent_working")
        if (
            row.get("herdr_workspace_id")
            and runtime is not None
            and row["herdr_workspace_id"] != runtime.get("workspace_id")
        ):
            drift.append("workspace_mismatch")
        lease_until = row.get("lease_until")
        if (
            state == "running"
            and isinstance(lease_until, (int, float))
            and lease_until <= generated_at
        ):
            drift.append("lease_expired")
        return {
            "id": _integer(row["id"]),
            "title": str(row["title"]),
            "harness": str(row["harness"]),
            "dedupe_key": str(row["dedupe_key"]),
            "placement": row.get("placement"),
            "state": state,
            "attempts": _integer(row["attempts"]),
            "max_attempts": _integer(row["max_attempts"]),
            "available_at": row.get("available_at"),
            "lease_until": lease_until,
            "agent_name": agent_name,
            "error_code": row.get("error_code"),
            "error_summary": row.get("error_summary"),
            "correlation_id": row.get("correlation_id"),
            "agent_settled": _nullable_bool(row.get("agent_settled")),
            "task_verified": _nullable_bool(row.get("task_verified")),
            "receipt_kind": row.get("receipt_kind"),
            "execution_path": row.get("execution_path"),
            "herdr_workspace_id": row.get("herdr_workspace_id"),
            "created_at": _number(row["created_at"]),
            "updated_at": _number(row["updated_at"]),
            "runtime": runtime,
            "drift": drift,
        }


def _attention(
    jobs: list[dict[str, object]],
    herdr: HerdrObservation,
    now: float,
) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    if herdr.health != "ok":
        items.append(
            {
                "severity": "critical",
                "code": "herdr_unavailable",
                "job_id": None,
                "title": "Herdr observation unavailable",
                "message": herdr.error_code or "unknown Herdr error",
            }
        )
    for job in jobs:
        state = str(job["state"])
        if state == "blocked":
            items.append(
                _job_attention(
                    job,
                    "critical",
                    "job_blocked",
                    job.get("error_code") or "Agent needs attention",
                )
            )
        elif state == "failed":
            items.append(
                _job_attention(
                    job,
                    "critical",
                    "job_failed",
                    job.get("error_code") or "Attempts exhausted",
                )
            )
        drift = job.get("drift")
        for code in drift if isinstance(drift, list) else []:
            items.append(
                _job_attention(
                    job,
                    "warning",
                    str(code),
                    str(code).replace("_", " "),
                )
            )
        updated_at = job.get("updated_at")
        if state == "running" and isinstance(updated_at, (int, float)) and now - updated_at > 300:
            items.append(
                _job_attention(
                    job,
                    "warning",
                    "job_stale",
                    "No durable state change for more than 5 minutes",
                )
            )
    return items


def _nullable_bool(value: object) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _job_attention(
    job: Mapping[str, object],
    severity: str,
    code: str,
    message: object,
) -> dict[str, object]:
    return {
        "severity": severity,
        "code": code,
        "job_id": job["id"],
        "title": job["title"],
        "message": str(message),
    }


def _topology(herdr: HerdrObservation, project_label: str) -> dict[str, object]:
    agents_by_pane: dict[str, list[dict[str, object]]] = {}
    for agent in herdr.agents:
        pane_id = agent.get("pane_id")
        if isinstance(pane_id, str):
            agents_by_pane.setdefault(pane_id, []).append(agent)
    panes_by_tab: dict[str, list[dict[str, object]]] = {}
    for pane in herdr.panes:
        tab_id = pane.get("tab_id")
        if not isinstance(tab_id, str):
            continue
        projected = dict(pane)
        candidates = agents_by_pane.get(str(pane.get("pane_id")), [])
        matching_agent = next(
            (candidate for candidate in candidates if _same_topology_location(pane, candidate)),
            None,
        )
        projected["agent"] = dict(matching_agent) if matching_agent is not None else None
        panes_by_tab.setdefault(tab_id, []).append(projected)
    tabs_by_workspace: dict[str, list[dict[str, object]]] = {}
    for tab in herdr.tabs:
        workspace_id = tab.get("workspace_id")
        tab_id = tab.get("tab_id")
        if not isinstance(workspace_id, str) or not isinstance(tab_id, str):
            continue
        projected = dict(tab)
        projected["panes"] = [
            pane for pane in panes_by_tab.get(tab_id, []) if _same_topology_location(tab, pane)
        ]
        tabs_by_workspace.setdefault(workspace_id, []).append(projected)
    worktree_by_workspace = {
        str(row["open_workspace_id"]): row
        for row in herdr.worktrees
        if isinstance(row.get("open_workspace_id"), str)
    }
    workspaces: list[dict[str, object]] = []
    worktrees: list[dict[str, object]] = []
    for workspace in herdr.workspaces:
        workspace_id = workspace.get("workspace_id")
        if not isinstance(workspace_id, str):
            continue
        projected = dict(workspace)
        projected["tabs"] = tabs_by_workspace.get(workspace_id, [])
        linked_worktree = worktree_by_workspace.get(
            workspace_id,
            projected.get("worktree"),
        )
        projected["worktree"] = linked_worktree
        workspaces.append(projected)
        worktrees.append(
            {
                "worktree_id": workspace_id,
                "workspace_id": workspace_id,
                "label": (
                    linked_worktree.get("label") if isinstance(linked_worktree, dict) else None
                )
                or projected.get("label")
                or workspace_id,
                "path": (
                    linked_worktree.get("path") if isinstance(linked_worktree, dict) else None
                ),
                "branch": (
                    linked_worktree.get("branch") if isinstance(linked_worktree, dict) else None
                ),
                "is_linked_worktree": bool(
                    isinstance(linked_worktree, dict)
                    and linked_worktree.get("is_linked_worktree") is True
                ),
                "tabs": projected["tabs"],
            }
        )
    projects = (
        [
            {
                "project_id": f"workflow:{project_label}",
                "label": project_label,
                "worktrees": worktrees,
            }
        ]
        if worktrees
        else []
    )
    return {"workspaces": workspaces, "projects": projects}


def _same_topology_location(
    left: Mapping[str, object],
    right: Mapping[str, object],
) -> bool:
    for field in ("workspace_id", "tab_id"):
        left_value = left.get(field)
        right_value = right.get(field)
        if (
            isinstance(left_value, str)
            and isinstance(right_value, str)
            and left_value != right_value
        ):
            return False
    return True


def _timeline(
    queue: QueueObservation,
    jobs: list[dict[str, object]],
) -> list[dict[str, object]]:
    jobs_by_id = {_integer(job["id"]): job for job in jobs}
    events: list[dict[str, object]] = []
    for job in jobs:
        events.append(
            {
                "id": f"job:{job['id']}:created",
                "type": "enqueued",
                "at": job["created_at"],
                "job_id": job["id"],
                "title": job["title"],
                "state": "pending",
                "attempt": 0,
                "agent_state": None,
                "error_code": None,
                "detail": f"{job['harness']} · {job.get('placement') or 'auto'}",
            }
        )
    for receipt in queue.receipts:
        job_id = _integer(receipt["job_id"])
        matching_job = jobs_by_id.get(job_id)
        if matching_job is None:
            continue
        events.append(
            {
                "id": f"receipt:{receipt['id']}",
                "type": "receipt",
                "at": _number(receipt["observed_at"]),
                "job_id": job_id,
                "title": matching_job["title"],
                "state": receipt["state"],
                "attempt": _integer(receipt["attempt"]),
                "agent_state": receipt["agent_state"],
                "error_code": receipt["error_code"],
                "correlation_id": receipt.get("correlation_id"),
                "detail": (
                    f"{receipt['agent_name']} · "
                    f"{receipt.get('placement') or matching_job.get('placement') or 'tab'}"
                ),
            }
        )
    events.sort(key=lambda event: (_number(event["at"]), str(event["id"])), reverse=True)
    return events[:100]


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise ValueError("dashboard_integer_invalid")
    return int(value)


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError("dashboard_number_invalid")
    return float(value)
