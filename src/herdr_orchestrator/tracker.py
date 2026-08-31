from __future__ import annotations

import contextlib
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from herdr_orchestrator.delivery_protocol import DeliveryPlan, DeliveryTicket, TicketReceipt
from herdr_orchestrator.model import StandardizedDeliveryConfig, TrackerBackend

GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
GITHUB_ISSUE_NUMBER = re.compile(r"[1-9][0-9]*\Z")


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


def _subprocess_process_runner(
    argv: list[str],
    *,
    capture_output: bool,
    text: bool,
    check: bool,
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        capture_output=capture_output,
        text=text,
        check=check,
        timeout=timeout,
    )


class LocalMarkdownTracker:
    def __init__(self, root: Path) -> None:
        try:
            self.root = root.expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise TrackerError("local_tracker_root_invalid") from exc
        self.references: dict[str, TrackerTicket] = {}

    def publish(self, plan: DeliveryPlan) -> dict[str, TrackerTicket]:
        feature_root = _safe_tracker_path(self.root, plan.slug)
        issues_root = _safe_tracker_path(feature_root, "issues")
        try:
            issues_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise TrackerError("local_tracker_unavailable") from exc
        _write_once_or_match(_safe_tracker_path(feature_root, "spec.md"), render_spec(plan))
        tickets: dict[str, TrackerTicket] = {}
        for ticket in plan.tickets:
            path = _safe_tracker_path(
                issues_root,
                f"{ticket.ticket_id}-{_slug(ticket.title)}.md",
            )
            ready_content = render_ticket(ticket)
            if path.exists():
                existing = _read_tracker_text(path)
                if existing != ready_content and not _completed_ticket_matches(
                    existing,
                    ticket,
                ):
                    raise TrackerError(f"tracker_artifact_conflict: {path}")
            else:
                _write_tracker_text(path, ready_content)
            tickets[ticket.ticket_id] = TrackerTicket(ticket.ticket_id, str(path))
        self.references = tickets
        return tickets

    def close(self, ticket: DeliveryTicket, receipt: TicketReceipt) -> None:
        reference = self.references.get(ticket.ticket_id)
        if reference is None:
            raise TrackerError(f"local_ticket_unknown: {ticket.ticket_id}")
        path = _safe_tracker_path(self.root, reference.reference)
        existing = _read_tracker_text(path)
        completed = render_ticket(ticket, receipt=receipt)
        if existing not in {render_ticket(ticket), completed}:
            raise TrackerError(f"tracker_artifact_conflict: {path}")
        if existing != completed:
            _write_tracker_text(path, completed)


class GithubTracker:
    def __init__(
        self,
        repository: str,
        *,
        runner: ProcessRunner = _subprocess_process_runner,
    ) -> None:
        if not GITHUB_REPOSITORY.fullmatch(repository):
            raise TrackerError("github_repository_invalid")
        self.repository = repository
        self.runner = runner
        self.references: dict[str, TrackerTicket] = {}
        self.spec_url: str | None = None

    def publish(self, plan: DeliveryPlan) -> dict[str, TrackerTicket]:
        self.references = {}
        self.spec_url = None
        spec_url = self._create_issue(f"[Spec] {plan.title}", render_spec(plan))
        self.spec_url = spec_url
        for ticket in plan.tickets:
            blocker_references: dict[str, str] = {}
            for blocker in ticket.blocked_by:
                reference = self.references.get(blocker)
                if reference is None:
                    raise TrackerError(f"github_blocker_unknown: {blocker}")
                blocker_references[blocker] = reference.reference
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
        issue_number = _github_issue_number(self.repository, reference.reference)
        if issue_number is None:
            raise TrackerError(f"github_ticket_reference_invalid: {ticket.ticket_id}")
        body = f"## Parent\n\n{self.spec_url}\n\n" f"{render_ticket(ticket, receipt=receipt)}"
        self._run_with_body(
            [
                "gh",
                "issue",
                "edit",
                issue_number,
                "--repo",
                self.repository,
            ],
            body,
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
        process = self._run_with_body(
            [
                "gh",
                "issue",
                "create",
                "--repo",
                self.repository,
                "--title",
                title,
            ],
            body,
        )
        try:
            stdout = process.stdout
        except (AttributeError, TypeError, UnicodeError) as exc:
            raise TrackerError("github_invalid_response") from exc
        if not isinstance(stdout, str):
            raise TrackerError("github_issue_create_invalid_response")
        url = stdout.strip()
        if _github_issue_number(self.repository, url) is None:
            raise TrackerError("github_issue_create_invalid_response")
        return url

    def _run_with_body(self, argv: list[str], body: str) -> subprocess.CompletedProcess[str]:
        body_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                prefix="herdr-tracker-",
                suffix=".md",
                delete=False,
            ) as handle:
                body_path = Path(handle.name)
                handle.write(body)
            return self._run([*argv, "--body-file", str(body_path)])
        except UnicodeError as exc:
            raise TrackerError("github_invalid_response") from exc
        except OSError as exc:
            raise TrackerError("github_body_file_failed") from exc
        finally:
            if body_path is not None:
                with contextlib.suppress(OSError):
                    body_path.unlink()

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
        except UnicodeDecodeError as exc:
            raise TrackerError("github_invalid_response") from exc
        try:
            returncode = process.returncode
        except (AttributeError, TypeError) as exc:
            raise TrackerError("github_invalid_response") from exc
        if returncode != 0:
            raise TrackerError("github_command_failed")
        return process


def tracker_from_config(config: StandardizedDeliveryConfig) -> DeliveryTracker:
    if config.tracker_backend is TrackerBackend.LOCAL_MARKDOWN:
        return LocalMarkdownTracker(config.tracker_root)
    if config.github_repository is None:
        raise TrackerError("github_repository_required")
    return GithubTracker(config.github_repository)


def _safe_tracker_path(root: Path, *parts: str) -> Path:
    if not all(isinstance(part, str) for part in parts):
        raise TrackerError("local_tracker_path_invalid")
    try:
        resolved_root = root.resolve()
        candidate = resolved_root.joinpath(*parts)
        relative_parts = candidate.relative_to(resolved_root).parts
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrackerError("local_tracker_path_invalid") from exc
    current = resolved_root
    for part in relative_parts:
        current /= part
        try:
            if current.is_symlink():
                raise TrackerError("local_tracker_path_symlink")
        except OSError as exc:
            raise TrackerError("local_tracker_path_invalid") from exc
    try:
        resolved_candidate = candidate.resolve(strict=False)
    except (OSError, RuntimeError, ValueError) as exc:
        raise TrackerError("local_tracker_path_invalid") from exc
    if not resolved_candidate.is_relative_to(resolved_root):
        raise TrackerError("local_tracker_path_invalid")
    return candidate


def _read_tracker_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise TrackerError(f"local_tracker_missing: {path}") from exc
    except UnicodeDecodeError as exc:
        raise TrackerError(f"local_artifact_invalid_encoding: {path}") from exc
    except OSError as exc:
        raise TrackerError(f"local_tracker_unavailable: {path}") from exc


def _write_tracker_text(path: Path, content: str) -> None:
    try:
        path.write_text(content, encoding="utf-8", newline="")
    except UnicodeError as exc:
        raise TrackerError(f"local_artifact_invalid_encoding: {path}") from exc
    except OSError as exc:
        raise TrackerError(f"local_tracker_unavailable: {path}") from exc


def _github_issue_number(repository: str, reference: object) -> str | None:
    if not isinstance(reference, str):
        return None
    try:
        parsed = urlsplit(reference)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc != "github.com" or parsed.query or parsed.fragment:
        return None
    prefix = f"/{repository}/issues/"
    if not parsed.path.startswith(prefix):
        return None
    number = parsed.path[len(prefix) :]
    if number.endswith("/"):
        number = number[:-1]
    return number if GITHUB_ISSUE_NUMBER.fullmatch(number) else None


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
        f"- [{'x' if accepted else ' '}] {criterion}" for criterion in ticket.acceptance_criteria
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
    try:
        exists = path.exists()
    except OSError as exc:
        raise TrackerError(f"local_tracker_unavailable: {path}") from exc
    if exists:
        if _read_tracker_text(path) != content:
            raise TrackerError(f"tracker_artifact_conflict: {path}")
        return
    _write_tracker_text(path, content)


def _completed_ticket_matches(content: str, ticket: DeliveryTicket) -> bool:
    blockers = ", ".join(ticket.blocked_by) if ticket.blocked_by else "None — can start immediately"
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
