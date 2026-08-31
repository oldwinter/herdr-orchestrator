from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import stat
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

SLUG = re.compile(r"[a-z0-9][a-z0-9-]{0,62}\Z")
TICKET_ID = re.compile(r"\d{2,3}\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


class DeliveryArtifactError(ValueError):
    pass


def validate_artifact_path(path: Path, *, root: Path | None = None) -> Path:
    if not isinstance(path, Path):
        raise DeliveryArtifactError("artifact_path_invalid")
    try:
        candidate = path if path.is_absolute() else path.absolute()
        declared_root = None if root is None else root.absolute()
        if ".." in candidate.parts:
            raise DeliveryArtifactError("artifact_path_escape")
        if declared_root is not None and not candidate.is_relative_to(declared_root):
            raise DeliveryArtifactError("artifact_path_invalid")
        _reject_symlink_chain(candidate)
        resolved = candidate.resolve(strict=False)
        if declared_root is not None and not resolved.is_relative_to(
            declared_root.resolve(strict=False)
        ):
            raise DeliveryArtifactError("artifact_path_invalid")
        return candidate
    except DeliveryArtifactError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise DeliveryArtifactError("artifact_path_invalid") from exc


def _reject_symlink_chain(path: Path) -> None:
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current /= part
        try:
            mode = current.lstat().st_mode
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise DeliveryArtifactError("artifact_path_invalid") from exc
        if stat.S_ISLNK(mode):
            raise DeliveryArtifactError("artifact_path_invalid")


def read_artifact_text(path: Path, artifact: str, *, root: Path | None = None) -> str:
    try:
        candidate = validate_artifact_path(path, root=root)
        descriptor = os.open(candidate, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError as exc:
        raise DeliveryArtifactError(f"{artifact}_missing") from exc
    except DeliveryArtifactError as exc:
        raise DeliveryArtifactError(f"{artifact}_path_invalid") from exc
    except OSError as exc:
        raise DeliveryArtifactError(f"{artifact}_unreadable") from exc
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise DeliveryArtifactError(f"{artifact}_unreadable")
        with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
            descriptor = -1
            return stream.read()
    except DeliveryArtifactError:
        raise
    except FileNotFoundError as exc:
        raise DeliveryArtifactError(f"{artifact}_missing") from exc
    except UnicodeDecodeError as exc:
        raise DeliveryArtifactError(f"{artifact}_invalid_json") from exc
    except OSError as exc:
        raise DeliveryArtifactError(f"{artifact}_unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_artifact_text(
    path: Path,
    content: str,
    *,
    root: Path | None = None,
    error_type: type[Exception] = DeliveryArtifactError,
) -> None:
    temporary: Path | None = None
    try:
        candidate = validate_artifact_path(path, root=root)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        validate_artifact_path(candidate, root=root)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=candidate.parent,
            prefix=f".{candidate.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        validate_artifact_path(temporary, root=root)
        validate_artifact_path(candidate, root=root)
        os.replace(temporary, candidate)
        temporary = None
    except DeliveryArtifactError as exc:
        raise error_type("delivery_artifact_path_invalid") from exc
    except (OSError, UnicodeError) as exc:
        raise error_type("delivery_artifact_write_failed") from exc
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def append_artifact_text(
    path: Path,
    content: str,
    *,
    root: Path | None = None,
    error_type: type[Exception] = DeliveryArtifactError,
) -> None:
    descriptor = -1
    try:
        candidate = validate_artifact_path(path, root=root)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        validate_artifact_path(candidate, root=root)
        descriptor = os.open(
            candidate,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("artifact is not a regular file")
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            descriptor = -1
            stream.write(content)
    except DeliveryArtifactError as exc:
        raise error_type("delivery_artifact_path_invalid") from exc
    except (OSError, UnicodeError) as exc:
        raise error_type("delivery_artifact_write_failed") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


@contextmanager
def exclusive_file_claim(
    path: Path,
    *,
    error_type: type[Exception] = DeliveryArtifactError,
) -> Iterator[None]:
    descriptor = -1
    try:
        candidate = validate_artifact_path(path)
        candidate.parent.mkdir(parents=True, exist_ok=True)
        candidate = validate_artifact_path(candidate)
        descriptor = os.open(
            candidate,
            os.O_RDWR | os.O_CREAT | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("lock is not a regular file")
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        descriptor = -1
    except DeliveryArtifactError as exc:
        raise error_type("delivery_artifact_path_invalid") from exc
    except OSError as exc:
        if descriptor >= 0:
            os.close(descriptor)
        raise error_type("delivery_run_claim_unavailable") from exc
    with handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise error_type("delivery_run_active") from exc
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class ProxyAction(StrEnum):
    ANSWER = "answer"
    APPROVE = "approve"
    DENY = "deny"
    ESCALATE = "escalate"


class AuthorityCategory(StrEnum):
    LOCAL_REVERSIBLE = "local-reversible"
    SPEC_AUTHORIZED = "spec-authorized"
    SECRET = "secret"
    PRODUCTION = "production"


class FindingSeverity(StrEnum):
    MUST_FIX = "must-fix"
    ADVISORY = "advisory"


@dataclass(frozen=True, slots=True)
class WayfinderRoute:
    use_wayfinder: bool
    reason: str


@dataclass(frozen=True, slots=True)
class DecisionTicket:
    ticket_id: str
    title: str
    question: str
    kind: str
    blocked_by: tuple[str, ...]
    resolution: str


@dataclass(frozen=True, slots=True)
class WayfinderMap:
    destination: str
    notes: tuple[str, ...]
    decisions: tuple[DecisionTicket, ...]
    not_yet_specified: tuple[str, ...]
    out_of_scope: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WayfinderResolution:
    ticket_id: str
    resolution: str
    new_decisions: tuple[DecisionTicket, ...]
    not_yet_specified: tuple[str, ...]
    out_of_scope: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeliveryTicket:
    ticket_id: str
    title: str
    what_to_build: str
    blocked_by: tuple[str, ...]
    acceptance_criteria: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class DeliveryPlan:
    slug: str
    title: str
    problem_statement: str
    solution: str
    user_stories: tuple[str, ...]
    implementation_decisions: tuple[str, ...]
    testing_decisions: tuple[str, ...]
    out_of_scope: tuple[str, ...]
    further_notes: tuple[str, ...]
    seams: tuple[str, ...]
    tickets: tuple[DeliveryTicket, ...]


@dataclass(frozen=True, slots=True)
class AcceptanceResult:
    criterion: str
    passed: bool
    evidence: str


@dataclass(frozen=True, slots=True)
class TicketReceipt:
    ticket_id: str
    commit: str
    acceptance: tuple[AcceptanceResult, ...]
    checks: tuple[str, ...]
    summary: str


@dataclass(frozen=True, slots=True)
class ReviewFinding:
    severity: FindingSeverity
    summary: str
    evidence: str
    source: str


@dataclass(frozen=True, slots=True)
class ReviewReport:
    standards: tuple[ReviewFinding, ...]
    spec: tuple[ReviewFinding, ...]

    @property
    def must_fix(self) -> tuple[ReviewFinding, ...]:
        return tuple(
            finding
            for finding in (*self.standards, *self.spec)
            if finding.severity is FindingSeverity.MUST_FIX
        )


@dataclass(frozen=True, slots=True)
class ProxyDecision:
    action: ProxyAction
    category: AuthorityCategory
    response: str
    rationale: str


@dataclass(frozen=True, slots=True)
class ReviewVerdict:
    accepted: tuple[str, ...]
    dismissed: tuple[str, ...]
    rationale: str


def map_payload(map_: WayfinderMap) -> dict[str, object]:
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


def load_tracker_publication(
    path: Path,
    *,
    backend: str,
    ticket_ids: set[str],
) -> tuple[str | None, dict[str, str]]:
    payload = _load_object(path, "tracker_publication")
    tickets = payload.get("tickets")
    spec_url = payload.get("spec_url")
    if (
        set(payload) != {"backend", "spec_url", "tickets"}
        or payload["backend"] != backend
        or (spec_url is not None and (not isinstance(spec_url, str) or not spec_url.strip()))
        or not isinstance(tickets, dict)
        or set(tickets) != ticket_ids
        or any(
            not isinstance(key, str) or not isinstance(value, str) or not value.strip()
            for key, value in tickets.items()
        )
    ):
        raise DeliveryArtifactError("tracker_publication_invalid")
    return spec_url, dict(tickets)


def load_wayfinder_route(path: Path) -> WayfinderRoute:
    payload = _load_object(path, "wayfinder_route")
    _exact_keys(payload, {"use_wayfinder", "reason"}, "wayfinder_route")
    use_wayfinder = payload["use_wayfinder"]
    if not isinstance(use_wayfinder, bool):
        raise DeliveryArtifactError("wayfinder_route_use_wayfinder_invalid")
    return WayfinderRoute(
        use_wayfinder=use_wayfinder,
        reason=_text(payload, "reason", 2_000, "wayfinder_route"),
    )


def load_wayfinder_map(path: Path) -> WayfinderMap:
    payload = _load_object(path, "wayfinder_map")
    _exact_keys(
        payload,
        {
            "destination",
            "notes",
            "decisions",
            "not_yet_specified",
            "out_of_scope",
        },
        "wayfinder_map",
    )
    rows = _object_list(payload, "decisions", 100, "wayfinder_map")
    decisions: list[DecisionTicket] = []
    seen: set[str] = set()
    for row in rows:
        _exact_keys(
            row,
            {"id", "title", "question", "kind", "blocked_by", "resolution"},
            "wayfinder_decision",
        )
        ticket_id = _identifier(row, "id", TICKET_ID, "wayfinder_decision")
        if ticket_id in seen:
            raise DeliveryArtifactError("wayfinder_decision_id_duplicate")
        kind = _text(row, "kind", 20, "wayfinder_decision")
        if kind not in {"research", "prototype", "grilling", "task"}:
            raise DeliveryArtifactError("wayfinder_decision_kind_invalid")
        decisions.append(
            DecisionTicket(
                ticket_id=ticket_id,
                title=_text(row, "title", 200, "wayfinder_decision"),
                question=_text(row, "question", 5_000, "wayfinder_decision"),
                kind=kind,
                blocked_by=_string_list(
                    row,
                    "blocked_by",
                    100,
                    3,
                    "wayfinder_decision",
                ),
                resolution=_optional_text(
                    row,
                    "resolution",
                    10_000,
                    "wayfinder_decision",
                ),
            )
        )
        seen.add(ticket_id)
    _validate_edges(decisions)
    return WayfinderMap(
        destination=_text(payload, "destination", 5_000, "wayfinder_map"),
        notes=_string_list(payload, "notes", 100, 2_000, "wayfinder_map"),
        decisions=tuple(decisions),
        not_yet_specified=_string_list(
            payload,
            "not_yet_specified",
            100,
            2_000,
            "wayfinder_map",
        ),
        out_of_scope=_string_list(
            payload,
            "out_of_scope",
            100,
            2_000,
            "wayfinder_map",
        ),
    )


def load_delivery_plan(path: Path) -> DeliveryPlan:
    payload = _load_object(path, "delivery_plan")
    _exact_keys(
        payload,
        {
            "slug",
            "title",
            "problem_statement",
            "solution",
            "user_stories",
            "implementation_decisions",
            "testing_decisions",
            "out_of_scope",
            "further_notes",
            "seams",
            "tickets",
        },
        "delivery_plan",
    )
    slug = _identifier(payload, "slug", SLUG, "delivery_plan")
    rows = _object_list(payload, "tickets", 100, "delivery_plan")
    if not rows:
        raise DeliveryArtifactError("delivery_plan_tickets_empty")
    tickets: list[DeliveryTicket] = []
    seen: set[str] = set()
    for row in rows:
        _exact_keys(
            row,
            {"id", "title", "what_to_build", "blocked_by", "acceptance_criteria"},
            "delivery_ticket",
        )
        ticket_id = _identifier(row, "id", TICKET_ID, "delivery_ticket")
        if ticket_id in seen:
            raise DeliveryArtifactError("delivery_ticket_id_duplicate")
        acceptance = _string_list(
            row,
            "acceptance_criteria",
            100,
            2_000,
            "delivery_ticket",
        )
        if not acceptance:
            raise DeliveryArtifactError("delivery_ticket_acceptance_empty")
        tickets.append(
            DeliveryTicket(
                ticket_id=ticket_id,
                title=_text(row, "title", 200, "delivery_ticket"),
                what_to_build=_text(row, "what_to_build", 10_000, "delivery_ticket"),
                blocked_by=_string_list(
                    row,
                    "blocked_by",
                    100,
                    3,
                    "delivery_ticket",
                ),
                acceptance_criteria=acceptance,
            )
        )
        seen.add(ticket_id)
    _validate_edges(tickets)
    return DeliveryPlan(
        slug=slug,
        title=_text(payload, "title", 200, "delivery_plan"),
        problem_statement=_text(payload, "problem_statement", 10_000, "delivery_plan"),
        solution=_text(payload, "solution", 10_000, "delivery_plan"),
        user_stories=_non_empty_strings(payload, "user_stories", "delivery_plan"),
        implementation_decisions=_non_empty_strings(
            payload,
            "implementation_decisions",
            "delivery_plan",
        ),
        testing_decisions=_non_empty_strings(
            payload,
            "testing_decisions",
            "delivery_plan",
        ),
        out_of_scope=_string_list(payload, "out_of_scope", 100, 2_000, "delivery_plan"),
        further_notes=_string_list(payload, "further_notes", 100, 2_000, "delivery_plan"),
        seams=_non_empty_strings(payload, "seams", "delivery_plan"),
        tickets=tuple(tickets),
    )


def load_wayfinder_resolution(
    path: Path,
    *,
    selected: DecisionTicket,
    known_ids: tuple[str, ...],
) -> WayfinderResolution:
    payload = _load_object(path, "wayfinder_resolution")
    _exact_keys(
        payload,
        {
            "ticket_id",
            "resolution",
            "new_decisions",
            "not_yet_specified",
            "out_of_scope",
        },
        "wayfinder_resolution",
    )
    ticket_id = _identifier(
        payload,
        "ticket_id",
        TICKET_ID,
        "wayfinder_resolution",
    )
    if ticket_id != selected.ticket_id:
        raise DeliveryArtifactError("wayfinder_resolution_ticket_mismatch")
    rows = _object_list(payload, "new_decisions", 100, "wayfinder_resolution")
    decisions: list[DecisionTicket] = []
    available = set(known_ids)
    for row in rows:
        _exact_keys(
            row,
            {"id", "title", "question", "kind", "blocked_by", "resolution"},
            "wayfinder_decision",
        )
        decision = DecisionTicket(
            ticket_id=_identifier(row, "id", TICKET_ID, "wayfinder_decision"),
            title=_text(row, "title", 200, "wayfinder_decision"),
            question=_text(row, "question", 5_000, "wayfinder_decision"),
            kind=_text(row, "kind", 20, "wayfinder_decision"),
            blocked_by=_string_list(
                row,
                "blocked_by",
                100,
                3,
                "wayfinder_decision",
            ),
            resolution=_optional_text(
                row,
                "resolution",
                10_000,
                "wayfinder_decision",
            ),
        )
        if decision.kind not in {"research", "prototype", "grilling", "task"}:
            raise DeliveryArtifactError("wayfinder_decision_kind_invalid")
        if decision.resolution:
            raise DeliveryArtifactError("wayfinder_new_decision_already_resolved")
        if decision.ticket_id in available:
            raise DeliveryArtifactError("wayfinder_decision_id_duplicate")
        if any(blocker not in available for blocker in decision.blocked_by):
            raise DeliveryArtifactError("wayfinder_decision_blocker_unknown")
        decisions.append(decision)
        available.add(decision.ticket_id)
    return WayfinderResolution(
        ticket_id=ticket_id,
        resolution=_text(payload, "resolution", 10_000, "wayfinder_resolution"),
        new_decisions=tuple(decisions),
        not_yet_specified=_string_list(
            payload,
            "not_yet_specified",
            100,
            2_000,
            "wayfinder_resolution",
        ),
        out_of_scope=_string_list(
            payload,
            "out_of_scope",
            100,
            2_000,
            "wayfinder_resolution",
        ),
    )


def load_ticket_receipt(path: Path, ticket: DeliveryTicket) -> TicketReceipt:
    payload = _load_object(path, "ticket_receipt")
    _exact_keys(
        payload,
        {"ticket_id", "commit", "acceptance", "checks", "summary"},
        "ticket_receipt",
    )
    ticket_id = _identifier(payload, "ticket_id", TICKET_ID, "ticket_receipt")
    if ticket_id != ticket.ticket_id:
        raise DeliveryArtifactError("ticket_receipt_ticket_mismatch")
    commit = _identifier(payload, "commit", COMMIT, "ticket_receipt")
    rows = _object_list(payload, "acceptance", 100, "ticket_receipt")
    acceptance: list[AcceptanceResult] = []
    for row in rows:
        _exact_keys(row, {"criterion", "passed", "evidence"}, "acceptance_result")
        passed = row["passed"]
        if not isinstance(passed, bool):
            raise DeliveryArtifactError("acceptance_result_passed_invalid")
        acceptance.append(
            AcceptanceResult(
                criterion=_text(row, "criterion", 2_000, "acceptance_result"),
                passed=passed,
                evidence=_text(row, "evidence", 5_000, "acceptance_result"),
            )
        )
    if tuple(item.criterion for item in acceptance) != ticket.acceptance_criteria:
        raise DeliveryArtifactError("ticket_receipt_acceptance_mismatch")
    if not acceptance or not all(item.passed for item in acceptance):
        raise DeliveryArtifactError("ticket_receipt_acceptance_failed")
    return TicketReceipt(
        ticket_id=ticket_id,
        commit=commit,
        acceptance=tuple(acceptance),
        checks=_non_empty_strings(payload, "checks", "ticket_receipt"),
        summary=_text(payload, "summary", 5_000, "ticket_receipt"),
    )


def load_review_report(path: Path) -> ReviewReport:
    payload = _load_object(path, "review_report")
    _exact_keys(payload, {"standards", "spec"}, "review_report")
    return ReviewReport(
        standards=_review_findings(payload, "standards"),
        spec=_review_findings(payload, "spec"),
    )


def load_review_axis(path: Path, axis: str) -> tuple[ReviewFinding, ...]:
    if axis not in {"standards", "spec"}:
        raise DeliveryArtifactError("review_axis_invalid")
    payload = _load_object(path, "review_axis")
    _exact_keys(payload, {axis}, "review_axis")
    return _review_findings(payload, axis)


def load_review_verdict(
    path: Path,
    *,
    candidates: tuple[str, ...],
) -> ReviewVerdict:
    payload = _load_object(path, "review_verdict")
    _exact_keys(payload, {"accepted", "dismissed", "rationale"}, "review_verdict")
    accepted = _string_list(payload, "accepted", 200, 40, "review_verdict")
    dismissed = _string_list(payload, "dismissed", 200, 40, "review_verdict")
    if len(set(accepted)) != len(accepted) or len(set(dismissed)) != len(dismissed):
        raise DeliveryArtifactError("review_verdict_duplicate")
    if set(accepted) & set(dismissed):
        raise DeliveryArtifactError("review_verdict_overlap")
    if set(accepted) | set(dismissed) != set(candidates):
        raise DeliveryArtifactError("review_verdict_incomplete")
    return ReviewVerdict(
        accepted=accepted,
        dismissed=dismissed,
        rationale=_text(payload, "rationale", 5_000, "review_verdict"),
    )


def load_proxy_decision(path: Path) -> ProxyDecision:
    payload = _load_object(path, "proxy_decision")
    _exact_keys(
        payload,
        {"action", "category", "response", "rationale"},
        "proxy_decision",
    )
    try:
        action = ProxyAction(_text(payload, "action", 20, "proxy_decision"))
        category = AuthorityCategory(_text(payload, "category", 30, "proxy_decision"))
    except ValueError as exc:
        raise DeliveryArtifactError("proxy_decision_enum_invalid") from exc
    response = _optional_text(payload, "response", 10_000, "proxy_decision")
    if action is not ProxyAction.ESCALATE and not response:
        raise DeliveryArtifactError("proxy_decision_response_required")
    if (
        category in {AuthorityCategory.SECRET, AuthorityCategory.PRODUCTION}
        and action is not ProxyAction.ESCALATE
    ):
        raise DeliveryArtifactError("proxy_decision_must_escalate")
    return ProxyDecision(
        action=action,
        category=category,
        response=response,
        rationale=_text(payload, "rationale", 5_000, "proxy_decision"),
    )


def _review_findings(payload: dict[str, Any], key: str) -> tuple[ReviewFinding, ...]:
    rows = _object_list(payload, key, 100, "review_report")
    findings: list[ReviewFinding] = []
    for row in rows:
        _exact_keys(row, {"severity", "summary", "evidence", "source"}, "review_finding")
        try:
            severity = FindingSeverity(_text(row, "severity", 20, "review_finding"))
        except ValueError as exc:
            raise DeliveryArtifactError("review_finding_severity_invalid") from exc
        findings.append(
            ReviewFinding(
                severity=severity,
                summary=_text(row, "summary", 2_000, "review_finding"),
                evidence=_text(row, "evidence", 5_000, "review_finding"),
                source=_text(row, "source", 2_000, "review_finding"),
            )
        )
    return tuple(findings)


def _load_object(path: Path, artifact: str) -> dict[str, Any]:
    text = read_artifact_text(path, artifact)

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DeliveryArtifactError(f"{artifact}_duplicate_key")
            result[key] = value
        return result

    try:
        payload = json.loads(
            text,
            object_pairs_hook=unique_object,
        )
    except DeliveryArtifactError:
        raise
    except (UnicodeDecodeError, ValueError) as exc:
        raise DeliveryArtifactError(f"{artifact}_invalid_json") from exc
    if not isinstance(payload, dict):
        raise DeliveryArtifactError(f"{artifact}_invalid_shape")
    return payload


def _exact_keys(payload: dict[str, Any], keys: set[str], artifact: str) -> None:
    if set(payload) != keys:
        raise DeliveryArtifactError(f"{artifact}_invalid_shape")


def _text(payload: dict[str, Any], key: str, maximum: int, artifact: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise DeliveryArtifactError(f"{artifact}_{key}_invalid")
    return value.strip()


def _optional_text(
    payload: dict[str, Any],
    key: str,
    maximum: int,
    artifact: str,
) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or len(value) > maximum:
        raise DeliveryArtifactError(f"{artifact}_{key}_invalid")
    return value.strip()


def _identifier(
    payload: dict[str, Any],
    key: str,
    pattern: re.Pattern[str],
    artifact: str,
) -> str:
    value = _text(payload, key, 128, artifact)
    if not pattern.fullmatch(value):
        raise DeliveryArtifactError(f"{artifact}_{key}_invalid")
    return value


def _object_list(
    payload: dict[str, Any],
    key: str,
    maximum: int,
    artifact: str,
) -> list[dict[str, Any]]:
    value = payload.get(key)
    if (
        not isinstance(value, list)
        or len(value) > maximum
        or not all(isinstance(item, dict) for item in value)
    ):
        raise DeliveryArtifactError(f"{artifact}_{key}_invalid")
    return value


def _string_list(
    payload: dict[str, Any],
    key: str,
    maximum_items: int,
    maximum_length: int,
    artifact: str,
) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list) or len(value) > maximum_items:
        raise DeliveryArtifactError(f"{artifact}_{key}_invalid")
    strings: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip() or len(item) > maximum_length:
            raise DeliveryArtifactError(f"{artifact}_{key}_invalid")
        strings.append(item.strip())
    return tuple(strings)


def _non_empty_strings(
    payload: dict[str, Any],
    key: str,
    artifact: str,
) -> tuple[str, ...]:
    values = _string_list(payload, key, 100, 5_000, artifact)
    if not values:
        raise DeliveryArtifactError(f"{artifact}_{key}_empty")
    return values


def _validate_edges(
    tickets: list[DecisionTicket] | list[DeliveryTicket],
) -> None:
    known: set[str] = set()
    for ticket in tickets:
        for blocker in ticket.blocked_by:
            if blocker not in known:
                raise DeliveryArtifactError("ticket_blocker_must_precede_ticket")
        known.add(ticket.ticket_id)
