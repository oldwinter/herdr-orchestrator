from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.delivery_protocol import DeliveryPlan, DeliveryTicket, TicketReceipt
from herdr_orchestrator.model import StandardizedDeliveryConfig, TrackerBackend

GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")


class TrackerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class TrackerTicket:
    ticket_id: str
    reference: str


class DeliveryTracker(Protocol):
    def publish(self, plan: DeliveryPlan) -> dict[str, TrackerTicket]: ...

    def close(self, ticket: DeliveryTicket, receipt: TicketReceipt) -> None: ...


class ProcessRunner(Protocol):
    def __call__(
        self,
        argv: list[str],
        *,
        capture_output: bool,
        text: bool,
        check: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess[str]: ...


class LocalMarkdownTracker:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.references: dict[str, TrackerTicket] = {}

    def publish(self, plan: DeliveryPlan) -> dict[str, TrackerTicket]:
        feature_root = self.root / plan.slug
        issues_root = feature_root / "issues"
        issues_root.mkdir(parents=True, exist_ok=True)
        _write_once_or_match(feature_root / "spec.md", render_spec(plan))
        tickets: dict[str, TrackerTicket] = {}
        for ticket in plan.tickets:
            path = issues_root / f"{ticket.ticket_id}-{_slug(ticket.title)}.md"
            ready_content = render_ticket(ticket)
            if path.exists():
                existing = path.read_text(encoding="utf-8")
                if existing != ready_content and not _completed_ticket_matches(
                    existing,
                    ticket,
                ):
                    raise TrackerError(f"tracker_artifact_conflict: {path}")
            else:
                path.write_text(ready_content, encoding="utf-8")
            tickets[ticket.ticket_id] = TrackerTicket(ticket.ticket_id, str(path))
        self.references = tickets
        return tickets

    def close(self, ticket: DeliveryTicket, receipt: TicketReceipt) -> None:
        reference = self.references.get(ticket.ticket_id)
        if reference is None:
            raise TrackerError(f"local_ticket_unknown: {ticket.ticket_id}")
        path = Path(reference.reference)
        existing = path.read_text(encoding="utf-8")
        completed = render_ticket(ticket, receipt=receipt)
        if existing not in {render_ticket(ticket), completed}:
            raise TrackerError(f"tracker_artifact_conflict: {path}")
        if existing != completed:
            path.write_text(completed, encoding="utf-8")


class GithubTracker:
    def __init__(
        self,
        repository: str,
        *,
        runner: ProcessRunner = subprocess.run,
    ) -> None:
        if not GITHUB_REPOSITORY.fullmatch(repository):
            raise TrackerError("github_repository_invalid")
        self.repository = repository
        self.runner = runner
        self.references: dict[str, TrackerTicket] = {}
        self.spec_url: str | None = None

    def publish(self, plan: DeliveryPlan) -> dict[str, TrackerTicket]:
        spec_url = self._create_issue(f"[Spec] {plan.title}", render_spec(plan))
        self.spec_url = spec_url
        for ticket in plan.tickets:
            blocker_references = {
                blocker: self.references[blocker].reference
                for blocker in ticket.blocked_by
            }
            body = (
                f"## Parent\n\n{spec_url}\n\n"
                f"{render_ticket(ticket, blocker_references=blocker_references)}"
            )
            url = self._create_issue(ticket.title, body)
            self.references[ticket.ticket_id] = TrackerTicket(ticket.ticket_id, url)
        return dict(self.references)

    def close(self, ticket: DeliveryTicket, receipt: TicketReceipt) -> None:
        reference = self.references.get(ticket.ticket_id)
        if reference is None:
            raise TrackerError(f"github_ticket_unknown: {ticket.ticket_id}")
        if self.spec_url is None:
            raise TrackerError("github_spec_unknown")
        issue_number = reference.reference.rstrip("/").rsplit("/", 1)[-1]
        body = (
            f"## Parent\n\n{self.spec_url}\n\n"
            f"{render_ticket(ticket, receipt=receipt)}"
        )
        self._run(
            [
                "gh",
                "issue",
                "edit",
                issue_number,
                "--repo",
                self.repository,
                "--body",
                body,
            ]
        )
        self._run(
            [
                "gh",
                "issue",
                "close",
                issue_number,
                "--repo",
                self.repository,
                "--reason",
                "completed",
            ]
        )

    def _create_issue(self, title: str, body: str) -> str:
        process = self._run(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                self.repository,
                "--title",
                title,
                "--body",
                body,
            ]
        )
        url = process.stdout.strip()
        if not url.startswith("https://"):
            raise TrackerError("github_issue_create_invalid_response")
        return url

    def _run(self, argv: list[str]) -> subprocess.CompletedProcess[str]:
        try:
            process = self.runner(
                argv,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except subprocess.TimeoutExpired as exc:
            raise TrackerError("github_command_timeout") from exc
        except OSError as exc:
            raise TrackerError("github_unavailable") from exc
        if process.returncode != 0:
            raise TrackerError("github_command_failed")
        return process


def tracker_from_config(config: StandardizedDeliveryConfig) -> DeliveryTracker:
    if config.tracker_backend is TrackerBackend.LOCAL_MARKDOWN:
        return LocalMarkdownTracker(config.tracker_root)
    if config.github_repository is None:
        raise TrackerError("github_repository_required")
    return GithubTracker(config.github_repository)


def render_spec(plan: DeliveryPlan) -> str:
    return "\n".join(
        [
            f"# {plan.title}",
            "",
            "## Problem Statement",
            "",
            plan.problem_statement,
            "",
            "## Solution",
            "",
            plan.solution,
            "",
            "## User Stories",
            "",
            _numbered(plan.user_stories),
            "",
            "## Implementation Decisions",
            "",
            _bullets(plan.implementation_decisions),
            "",
            "## Testing Decisions",
            "",
            _bullets(plan.testing_decisions),
            "",
            "## Testing Seams",
            "",
            _bullets(plan.seams),
            "",
            "## Out of Scope",
            "",
            _bullets(plan.out_of_scope) or "None.",
            "",
            "## Further Notes",
            "",
            _bullets(plan.further_notes) or "None.",
            "",
        ]
    )


def render_ticket(
    ticket: DeliveryTicket,
    *,
    receipt: TicketReceipt | None = None,
    blocker_references: dict[str, str] | None = None,
) -> str:
    blockers = (
        ", ".join(
            (
                blocker_references.get(blocker, blocker)
                if blocker_references is not None
                else blocker
            )
            for blocker in ticket.blocked_by
        )
        if ticket.blocked_by
        else "None — can start immediately"
    )
    accepted = receipt is not None
    lines = [
        f"# {ticket.ticket_id} — {ticket.title}",
        "",
        f"**What to build:** {ticket.what_to_build}",
        "",
        f"**Blocked by:** {blockers}",
        "",
        f"**Status:** {'completed' if accepted else 'ready-for-agent'}",
        "",
    ]
    lines.extend(
        f"- [{'x' if accepted else ' '}] {criterion}"
        for criterion in ticket.acceptance_criteria
    )
    if receipt is not None:
        lines.extend(
            [
                "",
                "## Completion receipt",
                "",
                f"- Commit: `{receipt.commit}`",
                f"- Checks: {', '.join(receipt.checks)}",
                f"- Summary: {receipt.summary}",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def _write_once_or_match(path: Path, content: str) -> None:
    if path.exists():
        if path.read_text(encoding="utf-8") != content:
            raise TrackerError(f"tracker_artifact_conflict: {path}")
        return
    path.write_text(content, encoding="utf-8")


def _completed_ticket_matches(content: str, ticket: DeliveryTicket) -> bool:
    blockers = (
        ", ".join(ticket.blocked_by)
        if ticket.blocked_by
        else "None — can start immediately"
    )
    required = {
        f"# {ticket.ticket_id} — {ticket.title}",
        f"**What to build:** {ticket.what_to_build}",
        f"**Blocked by:** {blockers}",
        "**Status:** completed",
        *(f"- [x] {criterion}" for criterion in ticket.acceptance_criteria),
    }
    return required.issubset(set(content.splitlines()))


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return (slug or "ticket")[:63].rstrip("-")


def _numbered(items: tuple[str, ...]) -> str:
    return "\n".join(f"{index}. {item}" for index, item in enumerate(items, 1))


def _bullets(items: tuple[str, ...]) -> str:
    return "\n".join(f"- {item}" for item in items)
