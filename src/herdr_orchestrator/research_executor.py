from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from herdr_orchestrator.research_evidence import (
    Claim,
    ClaimRecord,
    CriticalityPolicy,
    EVIDENCE_NORMALIZATION_VERSION,
    EvidenceRelation,
    EvidenceRelationRecord,
    ExcerptReceipt,
    MAX_CLAIM_BYTES,
    MAX_EXCERPT_BYTES,
    MAX_SOURCE_URL_BYTES,
    RESEARCH_EVIDENCE_SCHEMA_VERSION,
    ResearchEvidenceError,
    ResearchEvidenceRegister,
    SUPPORTED_CLAIM_TYPES,
    SUPPORTED_EVIDENCE_RELATIONS,
    SUPPORTED_SOURCE_OUTCOMES,
    SourceExcerpt,
    SourceExcerptReceipt,
    SourceReceipt,
    TypedClaim,
    VerificationAssignment,
    VerificationDisposition,
    derive_criticality,
    parse_evidence_relation,
    parse_evidence_register,
    parse_excerpt_receipt,
    parse_source_receipt,
    parse_typed_claim,
    validate_claim,
    validate_evidence_relation,
    validate_evidence_register,
    validate_excerpt_receipt,
    validate_source_receipt,
)


RESEARCH_INPUT_VERSION = 1
MAX_QUESTION_BYTES = 16_384
MAX_FACETS = 32
MAX_PERSPECTIVES = 32
MAX_REQUIRED_CELLS = 256
DEFAULT_REQUIRED_INPUT_ID = "user-material"
DEFAULT_INPUT_MAX_BYTES = 16_384
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,31}\Z")
_REFERENCE_IDENTIFIER = re.compile(
    r"[a-z][a-z0-9_-]{0,255}(?::[a-z][a-z0-9_-]{0,255})?\Z"
)


class ResearchInputError(ValueError):
    """Raised when research input or its fixed decomposition is invalid."""

    code = "research_input_invalid"


@dataclass(frozen=True, slots=True)
class ResearchInput:
    version: int
    kind: str
    question: str
    digest: str

    def to_dict(self) -> dict[str, object]:
        return {
            "version": self.version,
            "kind": self.kind,
            "question_digest": self.digest,
            "question_bytes": len(self.question.encode("utf-8")),
        }


@dataclass(frozen=True, slots=True)
class Dimension:
    id: str
    label: str

    def to_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


Facet = Dimension
Perspective = Dimension


@dataclass(frozen=True, slots=True)
class MatrixCell:
    id: str
    facet_id: str
    perspective_id: str

    def to_dict(self) -> dict[str, str]:
        return {
            "id": self.id,
            "facet": self.facet_id,
            "perspective": self.perspective_id,
        }


@dataclass(frozen=True, slots=True)
class CoveragePolicy:
    threshold: float
    allow_multi_cell_credit: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "formula": "credited_required_cells / total_required_cells",
            "threshold": self.threshold,
            "allow_multi_cell_credit": self.allow_multi_cell_credit,
            "credit_unit": "required_facet_perspective_cell",
        }


@dataclass(frozen=True, slots=True)
class CoverageResult:
    credited_cells: int
    total_cells: int
    ratio: float
    threshold: float | None = None

    @property
    def sufficient(self) -> bool:
        return self.threshold is None or self.ratio >= self.threshold

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "credited_cells": self.credited_cells,
            "total_cells": self.total_cells,
            "ratio": self.ratio,
            "formula": "credited_required_cells / total_required_cells",
        }
        if self.threshold is not None:
            result["threshold"] = self.threshold
            result["sufficient"] = self.sufficient
        return result


RESEARCH_BUDGET_SCHEMA_VERSION = 1
SUPPORTED_CONTESTED_DISPOSITIONS = frozenset(
    {"contested", "verified", "unverified", "rejected"}
)


@dataclass(frozen=True, slots=True)
class ResearchBudgets:
    """Pinned, finite limits for semantic research expansion."""

    max_lead_rounds: int = 3
    max_work_items: int = 64
    max_turns: int = 128
    max_seconds: int = 900
    max_stagnant_rounds: int = 2
    max_public_route_attempts: int = 2
    schema_version: int = RESEARCH_BUDGET_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != RESEARCH_BUDGET_SCHEMA_VERSION
        ):
            raise ResearchInputError("research_budget_schema_version_unsupported")
        for field_name in (
            "max_lead_rounds",
            "max_work_items",
            "max_turns",
            "max_seconds",
            "max_stagnant_rounds",
            "max_public_route_attempts",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 1
            ):
                raise ResearchInputError(f"research_budget_{field_name}_invalid")
            maximum = {
                "max_lead_rounds": 1024,
                "max_work_items": 100_000,
                "max_turns": 100_000,
                "max_seconds": 86_400,
                "max_stagnant_rounds": 1024,
                "max_public_route_attempts": 1024,
            }[field_name]
            if value > maximum:
                raise ResearchInputError(
                    f"research_budget_{field_name}_over_limit"
                )

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any] | None,
        *,
        loop: Mapping[str, Any] | None = None,
    ) -> ResearchBudgets:
        entries: list[tuple[str, Any]] = []
        if loop is not None:
            if not isinstance(loop, Mapping):
                raise ResearchInputError("research_loop_must_be_table")
            entries.extend(loop.items())
        if value is not None:
            if not isinstance(value, Mapping):
                raise ResearchInputError("research_budgets_must_be_table")
            entries.extend(value.items())
        aliases = {
            "max_rounds": "max_lead_rounds",
            "max_time_seconds": "max_seconds",
            "max_time": "max_seconds",
            "max_required_public_route_attempts": "max_public_route_attempts",
            "max_route_attempts": "max_public_route_attempts",
        }
        normalized: dict[str, Any] = {}
        allowed = {
            "schema_version",
            "max_lead_rounds",
            "max_work_items",
            "max_turns",
            "max_seconds",
            "max_stagnant_rounds",
            "max_public_route_attempts",
            *aliases,
        }
        unknown = {key for key, _ in entries} - allowed
        if unknown:
            raise ResearchInputError(
                f"research_budget_unknown_field:{sorted(unknown, key=str)[0]}"
            )
        for key, item in entries:
            canonical = aliases.get(key, key)
            if canonical in normalized and normalized[canonical] != item:
                raise ResearchInputError(
                    f"research_budget_conflict:{canonical}"
                )
            normalized[canonical] = item
        version = normalized.pop("schema_version", RESEARCH_BUDGET_SCHEMA_VERSION)
        if (
            not isinstance(version, int)
            or isinstance(version, bool)
            or version != RESEARCH_BUDGET_SCHEMA_VERSION
        ):
            raise ResearchInputError("research_budget_schema_version_unsupported")
        return cls(schema_version=version, **normalized)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "max_lead_rounds": self.max_lead_rounds,
            "max_work_items": self.max_work_items,
            "max_turns": self.max_turns,
            "max_seconds": self.max_seconds,
            "max_stagnant_rounds": self.max_stagnant_rounds,
            "max_public_route_attempts": self.max_public_route_attempts,
        }


@dataclass(frozen=True, slots=True)
class ResearchTerminal:
    """A deterministic non-success terminal selected by the budget gate."""

    code: str
    reason: str
    budget: str
    state: str = "failed"

    def to_dict(self) -> dict[str, str]:
        return {
            "state": self.state,
            "code": self.code,
            "reason": self.reason,
            "budget": self.budget,
        }


@dataclass(slots=True)
class ResearchBudgetState:
    """Durable counters and the last semantic round signature."""

    lead_rounds_used: int = 0
    work_items_used: int = 0
    turns_used: int = 0
    elapsed_seconds: int | float = 0
    stagnant_rounds: int = 0
    public_route_attempts_used: int = 0
    last_progress_signature: str | None = None
    last_progress_state: ProgressSignature | None = None

    def __post_init__(self) -> None:
        for field_name in (
            "lead_rounds_used",
            "work_items_used",
            "turns_used",
            "stagnant_rounds",
            "public_route_attempts_used",
        ):
            value = getattr(self, field_name)
            if (
                not isinstance(value, int)
                or isinstance(value, bool)
                or value < 0
            ):
                raise ResearchInputError(f"research_budget_state_{field_name}_invalid")
        if (
            not isinstance(self.elapsed_seconds, (int, float))
            or isinstance(self.elapsed_seconds, bool)
            or not math.isfinite(float(self.elapsed_seconds))
            or self.elapsed_seconds < 0
        ):
            raise ResearchInputError("research_budget_state_elapsed_seconds_invalid")
        if self.last_progress_signature is not None and (
            not isinstance(self.last_progress_signature, str)
            or not self.last_progress_signature
        ):
            raise ResearchInputError("research_budget_state_signature_invalid")
        if self.last_progress_state is not None and not isinstance(
            self.last_progress_state,
            ProgressSignature,
        ):
            raise ResearchInputError("research_budget_state_signature_invalid")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ResearchBudgetState:
        if not isinstance(value, Mapping):
            raise ResearchInputError("research_budget_state_object_required")
        allowed = {
            "lead_rounds_used",
            "work_items_used",
            "turns_used",
            "elapsed_seconds",
            "stagnant_rounds",
            "public_route_attempts_used",
            "last_progress_signature",
            "last_progress",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ResearchInputError(
                f"research_budget_state_unknown_field:{sorted(unknown, key=str)[0]}"
            )
        last_progress = value.get("last_progress")
        parsed_progress = (
            None
            if last_progress is None
            else ProgressSignature.from_mapping(last_progress)
        )
        digest = value.get("last_progress_signature")
        if digest is None and parsed_progress is not None:
            digest = parsed_progress.digest
        if digest is not None and (
            not isinstance(digest, str) or not digest
        ):
            raise ResearchInputError("research_budget_state_signature_invalid")
        if parsed_progress is not None and digest != parsed_progress.digest:
            raise ResearchInputError("research_budget_state_signature_mismatch")
        return cls(
            lead_rounds_used=value.get("lead_rounds_used", 0),
            work_items_used=value.get("work_items_used", 0),
            turns_used=value.get("turns_used", 0),
            elapsed_seconds=value.get("elapsed_seconds", 0),
            stagnant_rounds=value.get("stagnant_rounds", 0),
            public_route_attempts_used=value.get(
                "public_route_attempts_used",
                0,
            ),
            last_progress_signature=digest,
            last_progress_state=parsed_progress,
        )

    def observe_signature(
        self,
        signature: ProgressSignature | str,
    ) -> bool:
        """Record one settled round and return whether semantic progress changed."""

        is_structured = isinstance(signature, ProgressSignature)
        digest = signature.digest if is_structured else signature
        if not isinstance(digest, str) or not digest:
            raise ResearchInputError("research_progress_signature_invalid")
        if is_structured:
            changed = progress_changed(self.last_progress_state, signature)
            self.last_progress_state = signature
        else:
            changed = self.last_progress_signature != digest
        self.stagnant_rounds = 0 if changed else self.stagnant_rounds + 1
        self.last_progress_signature = digest
        return changed

    record_signature = observe_signature
    update_progress = observe_signature

    def to_dict(self) -> dict[str, object]:
        return {
            "lead_rounds_used": self.lead_rounds_used,
            "work_items_used": self.work_items_used,
            "turns_used": self.turns_used,
            "elapsed_seconds": self.elapsed_seconds,
            "stagnant_rounds": self.stagnant_rounds,
            "public_route_attempts_used": self.public_route_attempts_used,
            "last_progress_signature": self.last_progress_signature,
            "last_progress": (
                None
                if self.last_progress_state is None
                else self.last_progress_state.to_dict()
            ),
        }

    def terminal(
        self,
        *,
        max_work_items: int,
        max_turns: int,
        max_seconds: int | float,
        max_lead_rounds: int,
        max_public_route_attempts: int,
        max_stagnant_rounds: int,
        unresolved_coverage: bool,
    ) -> ResearchTerminal | None:
        for field_name, limit in (
            ("max_work_items", max_work_items),
            ("max_turns", max_turns),
            ("max_lead_rounds", max_lead_rounds),
            ("max_public_route_attempts", max_public_route_attempts),
            ("max_stagnant_rounds", max_stagnant_rounds),
        ):
            if (
                not isinstance(limit, int)
                or isinstance(limit, bool)
                or limit < 1
            ):
                raise ResearchInputError(f"research_budget_{field_name}_invalid")
        if (
            not isinstance(max_seconds, (int, float))
            or isinstance(max_seconds, bool)
            or not math.isfinite(float(max_seconds))
            or max_seconds < 1
        ):
            raise ResearchInputError("research_budget_max_seconds_invalid")
        if not unresolved_coverage:
            return None
        # The order is part of the public deterministic decision policy.  A
        # fixture normally exhausts one counter at a time, while this order
        # makes a corrupted/multi-exhausted state deterministic as well.
        limits = (
            (
                self.stagnant_rounds >= max_stagnant_rounds,
                "coverage_stagnant",
                "stagnant_rounds",
                "required coverage made no qualifying progress",
            ),
            (
                self.work_items_used >= max_work_items,
                "work_item_budget_exhausted",
                "work_items",
                "research work-item budget exhausted",
            ),
            (
                self.turns_used >= max_turns,
                "turn_budget_exhausted",
                "turns",
                "research turn budget exhausted",
            ),
            (
                float(self.elapsed_seconds) >= float(max_seconds),
                "time_budget_exhausted",
                "time",
                "research time budget exhausted",
            ),
            (
                self.lead_rounds_used >= max_lead_rounds,
                "lead_round_budget_exhausted",
                "lead_rounds",
                "research lead-round budget exhausted",
            ),
            (
                self.public_route_attempts_used >= max_public_route_attempts,
                "required_public_route_exhausted",
                "required_public_route_attempts",
                "required public route budget exhausted",
            ),
        )
        for exhausted, code, budget, reason in limits:
            if exhausted:
                return ResearchTerminal(code=code, reason=reason, budget=budget)
        return None

    def terminal_for(self, budgets: ResearchBudgets, *, unresolved_coverage: bool) -> ResearchTerminal | None:
        return self.terminal(
            max_work_items=budgets.max_work_items,
            max_turns=budgets.max_turns,
            max_seconds=budgets.max_seconds,
            max_lead_rounds=budgets.max_lead_rounds,
            max_public_route_attempts=budgets.max_public_route_attempts,
            max_stagnant_rounds=budgets.max_stagnant_rounds,
            unresolved_coverage=unresolved_coverage,
        )


@dataclass(frozen=True, slots=True)
class ProgressSignature:
    """Canonical semantic changes that are allowed to reset stagnation."""

    canonical_evidence_ids: tuple[str, ...]
    credited_required_cell_ids: tuple[str, ...]
    resolved_verification_ids: tuple[str, ...]
    contested_disposition_transitions: tuple[tuple[str, str, tuple[str, ...]], ...]
    schema_version: int = RESEARCH_BUDGET_SCHEMA_VERSION

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ProgressSignature:
        if not isinstance(value, Mapping):
            raise ResearchInputError("research_progress_signature_object_required")
        allowed = {
            "schema_version",
            "canonical_evidence_ids",
            "credited_required_cell_ids",
            "resolved_verification_ids",
            "contested_disposition_transitions",
            "digest",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ResearchInputError(
                f"research_progress_signature_unknown_field:{sorted(unknown, key=str)[0]}"
            )
        schema_version = value.get("schema_version", RESEARCH_BUDGET_SCHEMA_VERSION)
        if (
            not isinstance(schema_version, int)
            or isinstance(schema_version, bool)
            or schema_version != RESEARCH_BUDGET_SCHEMA_VERSION
        ):
            raise ResearchInputError(
                "research_progress_signature_schema_version_unsupported"
            )
        current = build_progress_signature(
            canonical_evidence_ids=value.get("canonical_evidence_ids", []),
            credited_required_cell_ids=value.get(
                "credited_required_cell_ids",
                [],
            ),
            resolved_verification_ids=value.get(
                "resolved_verification_ids",
                [],
            ),
            contested_disposition_transitions=value.get(
                "contested_disposition_transitions",
                [],
            ),
        )
        supplied_digest = value.get("digest")
        if supplied_digest is not None and supplied_digest != current.digest:
            raise ResearchInputError("research_progress_signature_digest_mismatch")
        return current

    @property
    def digest(self) -> str:
        value = {
            "schema_version": self.schema_version,
            "canonical_evidence_ids": list(self.canonical_evidence_ids),
            "credited_required_cell_ids": list(self.credited_required_cell_ids),
            "resolved_verification_ids": list(self.resolved_verification_ids),
            "contested_disposition_transitions": [
                {
                    "disposition_id": disposition_id,
                    "disposition": disposition,
                    "evidence_ids": list(evidence_ids),
                }
                for disposition_id, disposition, evidence_ids in
                self.contested_disposition_transitions
            ],
        }
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "canonical_evidence_ids": list(self.canonical_evidence_ids),
            "credited_required_cell_ids": list(self.credited_required_cell_ids),
            "resolved_verification_ids": list(self.resolved_verification_ids),
            "contested_disposition_transitions": [
                {
                    "disposition_id": disposition_id,
                    "disposition": disposition,
                    "evidence_ids": list(evidence_ids),
                }
                for disposition_id, disposition, evidence_ids in
                self.contested_disposition_transitions
            ],
            "digest": self.digest,
        }


def _canonical_ids(values: Iterable[str], field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ResearchInputError(f"research_{field}_invalid")
    result: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not _REFERENCE_IDENTIFIER.fullmatch(value):
            raise ResearchInputError(f"research_{field}_invalid")
        result.add(value)
    return tuple(sorted(result))


def _canonical_disposition_transitions(
    values: Iterable[Mapping[str, Any] | tuple[Any, ...]],
) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Iterable):
        raise ResearchInputError("research_progress_disposition_invalid")
    result: set[tuple[str, str, tuple[str, ...]]] = set()
    for value in values:
        if isinstance(value, Mapping):
            disposition_id = value.get("disposition_id")
            disposition = value.get("disposition")
            evidence_ids = value.get("evidence_ids", ())
            if set(value) - {"disposition_id", "disposition", "evidence_ids"}:
                raise ResearchInputError("research_progress_disposition_unknown_field")
        elif isinstance(value, tuple) and len(value) == 3:
            disposition_id, disposition, evidence_ids = value
        else:
            raise ResearchInputError("research_progress_disposition_invalid")
        if (
            not isinstance(disposition_id, str)
            or not _IDENTIFIER.fullmatch(disposition_id)
            or not isinstance(disposition, str)
            or disposition not in SUPPORTED_CONTESTED_DISPOSITIONS
        ):
            raise ResearchInputError("research_progress_disposition_invalid")
        if isinstance(evidence_ids, (str, bytes)):
            raise ResearchInputError("research_progress_evidence_ids_invalid")
        evidence = _canonical_ids(evidence_ids, "progress_evidence_ids")
        if not evidence:
            raise ResearchInputError("research_progress_disposition_evidence_required")
        result.add((disposition_id, disposition, evidence))
    return tuple(sorted(result))


def build_progress_signature(
    *,
    canonical_evidence_ids: Iterable[str],
    credited_required_cell_ids: Iterable[str],
    resolved_verification_ids: Iterable[str],
    contested_disposition_transitions: Iterable[
        Mapping[str, Any] | tuple[Any, ...]
    ],
) -> ProgressSignature:
    return ProgressSignature(
        canonical_evidence_ids=_canonical_ids(
            canonical_evidence_ids,
            "canonical_evidence_ids",
        ),
        credited_required_cell_ids=_canonical_ids(
            credited_required_cell_ids,
            "credited_required_cell_ids",
        ),
        resolved_verification_ids=_canonical_ids(
            resolved_verification_ids,
            "resolved_verification_ids",
        ),
        contested_disposition_transitions=_canonical_disposition_transitions(
            contested_disposition_transitions
        ),
    )


def progress_changed(
    previous: ProgressSignature | None,
    current: ProgressSignature,
) -> bool:
    if previous is None:
        return any(
            (
                current.canonical_evidence_ids,
                current.credited_required_cell_ids,
                current.resolved_verification_ids,
                current.contested_disposition_transitions,
            )
        )
    if previous == current:
        return False
    previous_evidence = set(previous.canonical_evidence_ids)
    current_evidence = set(current.canonical_evidence_ids)
    previous_cells = set(previous.credited_required_cell_ids)
    current_cells = set(current.credited_required_cell_ids)
    previous_verifications = set(previous.resolved_verification_ids)
    current_verifications = set(current.resolved_verification_ids)
    previous_dispositions = set(previous.contested_disposition_transitions)
    current_dispositions = set(current.contested_disposition_transitions)
    previous_dispositions_by_id: dict[
        str,
        set[tuple[str, str, tuple[str, ...]]],
    ] = {}
    current_dispositions_by_id: dict[
        str,
        set[tuple[str, str, tuple[str, ...]]],
    ] = {}
    for transition in previous_dispositions:
        previous_dispositions_by_id.setdefault(transition[0], set()).add(transition)
    for transition in current_dispositions:
        current_dispositions_by_id.setdefault(transition[0], set()).add(transition)
    if not set(previous_dispositions_by_id) <= set(current_dispositions_by_id):
        return False
    disposition_transition_changed = any(
        disposition_id not in previous_dispositions_by_id
        or any(
            _qualifying_disposition_transition(previous_transition, current_transition)
            for previous_transition in previous_dispositions_by_id[disposition_id]
            for current_transition in current_dispositions_by_id[disposition_id]
            if current_transition not in previous_dispositions_by_id[disposition_id]
        )
        for disposition_id in current_dispositions_by_id
    )
    # Progress is monotonic.  A replay/reorder has no delta, and a malformed
    # projection that drops an earlier semantic record cannot reset
    # stagnation merely because its digest changed.
    if not (
        previous_evidence <= current_evidence
        and previous_cells <= current_cells
        and previous_verifications <= current_verifications
    ):
        return False
    return bool(
        current_evidence - previous_evidence
        or current_cells - previous_cells
        or current_verifications - previous_verifications
        or disposition_transition_changed
    )


_DISPOSITION_PROGRESS_RANK = {
    "contested": 0,
    "unverified": 1,
    "verified": 2,
    "rejected": 2,
}


def _qualifying_disposition_transition(
    previous: tuple[str, str, tuple[str, ...]],
    current: tuple[str, str, tuple[str, ...]],
) -> bool:
    if previous[0] != current[0] or previous == current:
        return False
    previous_evidence = set(previous[2])
    current_evidence = set(current[2])
    if not previous_evidence <= current_evidence:
        return False
    return bool(
        current_evidence - previous_evidence
        or _DISPOSITION_PROGRESS_RANK[current[1]]
        > _DISPOSITION_PROGRESS_RANK[previous[1]]
    )


@dataclass(frozen=True, slots=True)
class NovelLead:
    """A canonical, evidence-grounded request for one unresolved cell."""

    lead_id: str
    canonical_id: str
    origin_evidence_ids: tuple[str, ...]
    target_cell_id: str
    declared_round: int
    schema_version: int = RESEARCH_BUDGET_SCHEMA_VERSION

    EXACT_KEYS = frozenset(
        {
            "schema_version",
            "lead_id",
            "canonical_id",
            "origin_evidence_ids",
            "target_cell_id",
            "declared_round",
        }
    )

    def __post_init__(self) -> None:
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != RESEARCH_BUDGET_SCHEMA_VERSION
        ):
            raise ResearchInputError("research_lead_schema_version_unsupported")
        for field_name, value in (
            ("lead_id", self.lead_id),
            ("canonical_id", self.canonical_id),
        ):
            if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
                raise ResearchInputError(f"research_lead_{field_name}_invalid")
        if (
            not isinstance(self.target_cell_id, str)
            or not _REFERENCE_IDENTIFIER.fullmatch(self.target_cell_id)
        ):
            raise ResearchInputError("research_lead_target_cell_id_invalid")
        if not self.origin_evidence_ids:
            raise ResearchInputError("research_lead_origin_required")
        _canonical_ids(self.origin_evidence_ids, "lead_origin_evidence_ids")
        if (
            not isinstance(self.declared_round, int)
            or isinstance(self.declared_round, bool)
            or self.declared_round < 0
        ):
            raise ResearchInputError("research_lead_round_invalid")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> NovelLead:
        if not isinstance(value, Mapping):
            raise ResearchInputError("research_lead_object_required")
        unknown = set(value) - cls.EXACT_KEYS
        missing = cls.EXACT_KEYS - set(value)
        if unknown:
            raise ResearchInputError(
                f"research_lead_unknown_field:{sorted(unknown, key=str)[0]}"
            )
        if missing:
            raise ResearchInputError(
                f"research_lead_missing_field:{sorted(missing, key=str)[0]}"
            )
        origins = value["origin_evidence_ids"]
        if isinstance(origins, (str, bytes)) or not isinstance(origins, list):
            raise ResearchInputError("research_lead_origin_evidence_ids_invalid")
        return cls(
            schema_version=value["schema_version"],
            lead_id=value["lead_id"],
            canonical_id=value["canonical_id"],
            origin_evidence_ids=_canonical_ids(
                origins,
                "lead_origin_evidence_ids",
            ),
            target_cell_id=value["target_cell_id"],
            declared_round=value["declared_round"],
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "lead_id": self.lead_id,
            "canonical_id": self.canonical_id,
            "origin_evidence_ids": list(self.origin_evidence_ids),
            "target_cell_id": self.target_cell_id,
            "declared_round": self.declared_round,
        }


@dataclass(frozen=True, slots=True)
class NovelLeadDecision:
    accepted: bool
    code: str
    reason: str
    lead: NovelLead
    next_round: int | None = None

    def to_dict(self) -> dict[str, object]:
        return {
            "accepted": self.accepted,
            "code": self.code,
            "reason": self.reason,
            "lead": self.lead.to_dict(),
            "next_round": self.next_round,
        }


def decide_novel_lead(
    lead: NovelLead | Mapping[str, Any],
    *,
    admitted_evidence_ids: Iterable[str],
    required_cell_ids: Iterable[str],
    covered_cell_ids: Iterable[str],
    canonical_lead_ids: Iterable[str],
    current_round: int,
    max_lead_rounds: int,
) -> NovelLeadDecision:
    candidate = lead if isinstance(lead, NovelLead) else NovelLead.from_mapping(lead)
    admitted = set(_canonical_ids(admitted_evidence_ids, "admitted_evidence_ids"))
    required = set(_canonical_ids(required_cell_ids, "required_cell_ids"))
    covered = set(_canonical_ids(covered_cell_ids, "covered_cell_ids"))
    existing = set(_canonical_ids(canonical_lead_ids, "canonical_lead_ids"))
    if candidate.canonical_id in existing:
        return NovelLeadDecision(
            False,
            "lead_duplicate",
            "canonical lead identity was already admitted",
            candidate,
        )
    if candidate.target_cell_id not in required:
        return NovelLeadDecision(
            False,
            "lead_unknown_target",
            "lead target is not a declared required cell",
            candidate,
        )
    if candidate.target_cell_id in covered:
        return NovelLeadDecision(
            False,
            "lead_already_covered",
            "lead target already has credited coverage",
            candidate,
        )
    if any(origin not in admitted for origin in candidate.origin_evidence_ids):
        return NovelLeadDecision(
            False,
            "lead_ungrounded",
            "lead origin does not resolve to admitted evidence",
            candidate,
        )
    if (
        not isinstance(current_round, int)
        or isinstance(current_round, bool)
        or current_round < 0
        or candidate.declared_round != current_round
    ):
        return NovelLeadDecision(
            False,
            "lead_round_mismatch",
            "lead was not emitted at the settled round barrier",
            candidate,
        )
    if (
        not isinstance(max_lead_rounds, int)
        or isinstance(max_lead_rounds, bool)
        or max_lead_rounds < 1
        or current_round >= max_lead_rounds
    ):
        return NovelLeadDecision(
            False,
            "lead_round_exhausted",
            "lead-round budget is exhausted",
            candidate,
        )
    return NovelLeadDecision(
        True,
        "lead_accepted",
        "canonical evidence-grounded lead targets unresolved coverage",
        candidate,
        next_round=current_round + 1,
    )


@dataclass(frozen=True, slots=True)
class SourceAvailabilityDecision:
    source_id: str
    outcome: str
    action: str
    code: str
    reason: str
    coverage_credit: bool
    attempt_consumed: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "source_id": self.source_id,
            "outcome": self.outcome,
            "action": self.action,
            "code": self.code,
            "reason": self.reason,
            "coverage_credit": self.coverage_credit,
            "attempt_consumed": self.attempt_consumed,
        }


def decide_source_availability(
    source: SourceReceipt,
    *,
    attempts_used: int,
    max_attempts: int,
    replacement_available: bool,
) -> SourceAvailabilityDecision:
    if not isinstance(source, SourceReceipt):
        raise ResearchInputError("research_source_receipt_required")
    if (
        not isinstance(attempts_used, int)
        or isinstance(attempts_used, bool)
        or attempts_used < 0
        or not isinstance(max_attempts, int)
        or isinstance(max_attempts, bool)
        or max_attempts < 1
    ):
        raise ResearchInputError("research_source_attempt_budget_invalid")
    if not isinstance(replacement_available, bool):
        raise ResearchInputError("research_source_replacement_flag_invalid")
    if source.outcome == "retrieved":
        return SourceAvailabilityDecision(
            source.source_id,
            source.outcome,
            "accept",
            "source_retrieved",
            "source retrieval is available; excerpt admission remains required",
            False,
        )
    if source.outcome in {"changed", "unavailable", "denied"} and replacement_available:
        return SourceAvailabilityDecision(
            source.source_id,
            source.outcome,
            "replace",
            (
                "source_changed_replace"
                if source.outcome == "changed"
                else f"source_{source.outcome}_replace"
            ),
            (
                "source bytes changed; require a new receipted observation"
                if source.outcome == "changed"
                else "source route is unavailable; require a new receipted observation"
            ),
            False,
        )
    if attempts_used < max_attempts:
        code = (
            "source_changed_retry"
            if source.outcome == "changed"
            else "source_unavailable_retry"
            if source.outcome == "unavailable"
            else "source_denied_retry"
        )
        return SourceAvailabilityDecision(
            source.source_id,
            source.outcome,
            "retry",
            code,
            "source is unavailable for this attempt; retry remains bounded",
            False,
        )
    return SourceAvailabilityDecision(
        source.source_id,
        source.outcome,
        "fail",
        "required_public_route_exhausted",
        "required source route exhausted without an admitted replacement",
        False,
    )


@dataclass(frozen=True, slots=True)
class DecompositionConfig:
    facets: tuple[Dimension, ...]
    perspectives: tuple[Dimension, ...]
    required_cells: tuple[MatrixCell, ...]
    max_facets: int
    max_perspectives: int
    max_required_cells: int

    def to_dict(self) -> dict[str, object]:
        return {
            "max_facets": self.max_facets,
            "max_perspectives": self.max_perspectives,
            "max_required_cells": self.max_required_cells,
            "facets": [facet.to_dict() for facet in self.facets],
            "perspectives": [item.to_dict() for item in self.perspectives],
            "required_cells": [cell.to_dict() for cell in self.required_cells],
        }


@dataclass(frozen=True, slots=True)
class InputPolicy:
    requires_user_material: bool
    required_input_id: str
    accepted_form: str
    max_bytes: int
    public_route_permitted: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "requires_user_material": self.requires_user_material,
            "required_input_id": self.required_input_id,
            "accepted_form": self.accepted_form,
            "max_bytes": self.max_bytes,
            "public_route_permitted": self.public_route_permitted,
        }


@dataclass(frozen=True, slots=True)
class ResearchConfig:
    decomposition: DecompositionConfig
    coverage: CoveragePolicy
    input_policy: InputPolicy
    roles: Mapping[str, str]
    verification: CriticalityPolicy
    budgets: ResearchBudgets = field(default_factory=ResearchBudgets)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> ResearchConfig:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ResearchInputError("research_config_must_be_table")
        allowed = {
            "decomposition",
            "coverage",
            "input",
            "roles",
            "verification",
            "loop",
            "budgets",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ResearchInputError(
                f"research_config_unknown_field:{sorted(unknown, key=str)[0]}"
            )
        decomposition = _parse_decomposition(value.get("decomposition"))
        coverage = _parse_coverage(value.get("coverage"))
        input_policy = _parse_input_policy(value.get("input"))
        roles = _parse_roles(value.get("roles"))
        verification = CriticalityPolicy.from_mapping(value.get("verification"))
        budgets = ResearchBudgets.from_mapping(
            value.get("budgets"),
            loop=value.get("loop"),
        )
        return cls(
            decomposition,
            coverage,
            input_policy,
            roles,
            verification,
            budgets,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "decomposition": self.decomposition.to_dict(),
            "coverage": self.coverage.to_dict(),
            "input": self.input_policy.to_dict(),
            "roles": dict(self.roles),
            "verification": self.verification.to_dict(),
            "budgets": self.budgets.to_dict(),
        }


@dataclass(frozen=True, slots=True)
class Classification:
    kind: str
    route: str
    public_collection_allowed: bool
    requires_input: bool
    reason: str
    required_input_id: str | None = None

    @property
    def admitted(self) -> bool:
        return not self.requires_input

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "route": self.route,
            "public_collection_allowed": self.public_collection_allowed,
            "requires_input": self.requires_input,
            "reason": self.reason,
            "required_input_id": self.required_input_id,
        }


@dataclass(frozen=True, slots=True)
class QuestionDecomposition:
    facets: tuple[Dimension, ...]
    perspectives: tuple[Dimension, ...]
    required_cells: tuple[MatrixCell, ...]
    coverage_policy: CoveragePolicy

    def to_dict(self) -> dict[str, object]:
        return {
            "facets": [facet.to_dict() for facet in self.facets],
            "perspectives": [item.to_dict() for item in self.perspectives],
            "required_cells": [cell.to_dict() for cell in self.required_cells],
            "coverage_policy": self.coverage_policy.to_dict(),
        }


def parse_research_input(
    value: Mapping[str, Any],
    *,
    max_bytes: int = MAX_QUESTION_BYTES,
) -> ResearchInput:
    if not isinstance(value, Mapping):
        raise ResearchInputError("research_input_must_be_object")
    expected = {"version", "kind", "question"}
    unknown = set(value) - expected
    missing = expected - set(value)
    if unknown:
        raise ResearchInputError(
            f"research_input_unknown_field:{sorted(unknown, key=str)[0]}"
        )
    if missing:
        raise ResearchInputError(
            f"research_input_missing_field:{sorted(missing)[0]}"
        )
    version = value["version"]
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != RESEARCH_INPUT_VERSION
    ):
        raise ResearchInputError("research_input_version_unsupported")
    kind = value["kind"]
    if kind != "public-question":
        raise ResearchInputError("research_input_kind_unsupported")
    question = value["question"]
    if not isinstance(question, str) or not question.strip():
        raise ResearchInputError("research_input_missing_question")
    encoded = question.strip().encode("utf-8")
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or max_bytes < 1
        or len(encoded) > max_bytes
    ):
        raise ResearchInputError("research_input_question_over_limit")
    return ResearchInput(
        version=version,
        kind=kind,
        question=question.strip(),
        digest=_digest_bytes(encoded),
    )


def classify_input(
    value: ResearchInput,
    config: ResearchConfig,
) -> Classification:
    if not isinstance(value, ResearchInput):
        raise ResearchInputError("research_input_required")
    policy = config.input_policy
    if policy.requires_user_material:
        return Classification(
            kind="public",
            route="public-question",
            public_collection_allowed=False,
            requires_input=True,
            reason="input_required",
            required_input_id=policy.required_input_id,
        )
    return Classification(
        kind="public",
        route="public-question",
        public_collection_allowed=True,
        requires_input=False,
        reason="classified_public",
    )


def decompose_question(
    value: ResearchInput,
    config: ResearchConfig,
) -> QuestionDecomposition:
    if not isinstance(value, ResearchInput):
        raise ResearchInputError("research_input_required")
    definition = config.decomposition
    facet_ids = {facet.id for facet in definition.facets}
    perspective_ids = {item.id for item in definition.perspectives}
    cells: list[MatrixCell] = []
    seen: set[str] = set()
    for cell in definition.required_cells:
        if cell.facet_id not in facet_ids:
            raise ResearchInputError(
                f"decomposition_unknown_facet:{cell.facet_id}"
            )
        if cell.perspective_id not in perspective_ids:
            raise ResearchInputError(
                f"decomposition_unknown_perspective:{cell.perspective_id}"
            )
        if cell.id in seen:
            raise ResearchInputError(f"decomposition_duplicate_cell:{cell.id}")
        seen.add(cell.id)
        cells.append(cell)
    if not cells:
        raise ResearchInputError("decomposition_required_cells_empty")
    if len(cells) > definition.max_required_cells:
        raise ResearchInputError("decomposition_required_cells_over_budget")
    return QuestionDecomposition(
        facets=definition.facets,
        perspectives=definition.perspectives,
        required_cells=tuple(cells),
        coverage_policy=config.coverage,
    )


def calculate_coverage(
    required_cells: Iterable[MatrixCell | tuple[str, str] | str],
    credited_cells: Iterable[MatrixCell | tuple[str, str] | str],
    *,
    threshold: float | None = None,
) -> CoverageResult:
    required = {_cell_key(cell) for cell in required_cells}
    if not required:
        raise ResearchInputError("coverage_required_cells_empty")
    credited = {_cell_key(cell) for cell in credited_cells}
    credited_required = required & credited
    ratio = len(credited_required) / len(required)
    return CoverageResult(
        credited_cells=len(credited_required),
        total_cells=len(required),
        ratio=ratio,
        threshold=threshold,
    )


def build_research_view(
    value: ResearchInput,
    classification: Classification,
    decomposition: QuestionDecomposition,
    *,
    input_policy: InputPolicy | None = None,
    verification_policy: CriticalityPolicy | Mapping[str, Any] | None = None,
    budgets: ResearchBudgets | Mapping[str, Any] | None = None,
    required_input_admitted: bool = False,
    required_input_digest: str | None = None,
) -> dict[str, object]:
    """Build the persisted, machine-readable research domain projection."""

    coverage = calculate_coverage(
        decomposition.required_cells,
        (),
        threshold=decomposition.coverage_policy.threshold,
    )
    required_input: dict[str, object] | None = None
    if classification.requires_input:
        policy = input_policy or InputPolicy(
            True,
            classification.required_input_id or DEFAULT_REQUIRED_INPUT_ID,
            "text",
            DEFAULT_INPUT_MAX_BYTES,
            False,
        )
        required_input = {
            "input_id": classification.required_input_id,
            "reason": "input_required" if not required_input_admitted else "admitted",
            "accepted_form": policy.accepted_form,
            "max_bytes": policy.max_bytes,
            "public_route_permitted": policy.public_route_permitted,
            "admitted": required_input_admitted,
        }
        if required_input_digest is not None:
            required_input["content_digest"] = required_input_digest
    policy = (
        verification_policy
        if isinstance(verification_policy, CriticalityPolicy)
        else CriticalityPolicy.from_mapping(verification_policy)
    )
    pinned_budgets = (
        budgets
        if isinstance(budgets, ResearchBudgets)
        else ResearchBudgets.from_mapping(budgets)
    )
    return {
        "input": value.to_dict(),
        "classification": classification.to_dict(),
        "decomposition": decomposition.to_dict(),
        "coverage": coverage.to_dict(),
        "phase": "input_required" if classification.requires_input else "decomposed",
        "required_input": required_input,
        "verification_policy": policy.to_dict(),
        "budgets": pinned_budgets.to_dict(),
        "progress": ResearchBudgetState().to_dict(),
    }


def input_digest(value: ResearchInput | str) -> str:
    if isinstance(value, ResearchInput):
        return value.digest
    if isinstance(value, str):
        return _digest_bytes(value.strip().encode("utf-8"))
    raise ResearchInputError("research_input_required")


def _parse_decomposition(value: Any) -> DecompositionConfig:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ResearchInputError("research_decomposition_must_be_table")
    allowed = {
        "facets",
        "perspectives",
        "required_cells",
        "max_facets",
        "max_perspectives",
        "max_required_cells",
    }
    _reject_unknown(value, allowed, "research_decomposition_unknown_field")
    max_facets = _bounded_int(value.get("max_facets", 8), "decomposition_max_facets")
    max_perspectives = _bounded_int(
        value.get("max_perspectives", 8),
        "decomposition_max_perspectives",
    )
    max_required_cells = _bounded_int(
        value.get("max_required_cells", max_facets * max_perspectives),
        "decomposition_max_required_cells",
        maximum=MAX_REQUIRED_CELLS,
    )
    facets = _parse_dimensions(value.get("facets", ["scope"]), "facet")
    perspectives = _parse_dimensions(
        value.get("perspectives", ["operator"]),
        "perspective",
    )
    if len(facets) > max_facets:
        raise ResearchInputError("decomposition_facets_over_budget")
    if len(perspectives) > max_perspectives:
        raise ResearchInputError("decomposition_perspectives_over_budget")
    facet_ids = {item.id for item in facets}
    perspective_ids = {item.id for item in perspectives}
    raw_cells = value.get("required_cells")
    if raw_cells is None:
        raw_cells = [
            {"facet": facet.id, "perspective": perspective.id}
            for facet in facets
            for perspective in perspectives
        ]
    if (
        isinstance(raw_cells, (str, bytes))
        or not isinstance(raw_cells, list)
        or not raw_cells
    ):
        raise ResearchInputError("decomposition_required_cells_must_be_array")
    cells: list[MatrixCell] = []
    seen: set[str] = set()
    for raw_cell in raw_cells:
        if not isinstance(raw_cell, Mapping):
            raise ResearchInputError("decomposition_cell_must_be_table")
        _reject_unknown(
            raw_cell,
            {"id", "facet", "perspective"},
            "decomposition_cell_unknown_field",
        )
        facet_id = _dimension_id(raw_cell.get("facet"), "facet")
        perspective_id = _dimension_id(raw_cell.get("perspective"), "perspective")
        if facet_id not in facet_ids:
            raise ResearchInputError(f"decomposition_unknown_facet:{facet_id}")
        if perspective_id not in perspective_ids:
            raise ResearchInputError(
                f"decomposition_unknown_perspective:{perspective_id}"
            )
        expected_id = f"{facet_id}:{perspective_id}"
        cell_id = raw_cell.get("id", expected_id)
        if not isinstance(cell_id, str) or cell_id != expected_id:
            raise ResearchInputError("decomposition_cell_id_must_match_coordinates")
        if cell_id in seen:
            raise ResearchInputError(f"decomposition_duplicate_cell:{cell_id}")
        seen.add(cell_id)
        cells.append(MatrixCell(cell_id, facet_id, perspective_id))
    if len(cells) > max_required_cells:
        raise ResearchInputError("decomposition_required_cells_over_budget")
    return DecompositionConfig(
        facets=tuple(facets),
        perspectives=tuple(perspectives),
        required_cells=tuple(cells),
        max_facets=max_facets,
        max_perspectives=max_perspectives,
        max_required_cells=max_required_cells,
    )


def _parse_dimensions(value: Any, kind: str) -> list[Dimension]:
    if isinstance(value, (str, bytes)) or not isinstance(value, list) or not value:
        raise ResearchInputError(f"decomposition_{kind}s_must_be_non_empty_array")
    result: list[Dimension] = []
    seen: set[str] = set()
    for item in value:
        if isinstance(item, str):
            item_id = _dimension_id(item, kind)
            label = item_id.replace("_", " ").replace("-", " ").title()
        elif isinstance(item, Mapping):
            _reject_unknown(item, {"id", "label"}, f"decomposition_{kind}_unknown_field")
            item_id = _dimension_id(item.get("id"), kind)
            label = item.get("label", item_id.replace("_", " ").title())
            if not isinstance(label, str) or not label.strip():
                raise ResearchInputError(f"decomposition_{kind}_label_invalid")
            label = label.strip()
        else:
            raise ResearchInputError(f"decomposition_{kind}_must_be_string_or_table")
        if item_id in seen:
            raise ResearchInputError(f"decomposition_duplicate_{kind}:{item_id}")
        seen.add(item_id)
        result.append(Dimension(item_id, label))
    return result


def _parse_coverage(value: Any) -> CoveragePolicy:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ResearchInputError("research_coverage_must_be_table")
    _reject_unknown(
        value,
        {"threshold", "allow_multi_cell_credit"},
        "research_coverage_unknown_field",
    )
    threshold = value.get("threshold", 1.0)
    if (
        not isinstance(threshold, (int, float))
        or isinstance(threshold, bool)
        or not math.isfinite(float(threshold))
        or not 0.0 <= float(threshold) <= 1.0
    ):
        raise ResearchInputError("coverage_threshold_invalid")
    allow = value.get("allow_multi_cell_credit", False)
    if not isinstance(allow, bool):
        raise ResearchInputError("coverage_multi_cell_credit_invalid")
    return CoveragePolicy(float(threshold), allow)


def _parse_input_policy(value: Any) -> InputPolicy:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ResearchInputError("research_input_policy_must_be_table")
    _reject_unknown(
        value,
        {
            "requires_user_material",
            "required",
            "required_input_id",
            "accepted_form",
            "max_bytes",
            "public_route_permitted",
        },
        "research_input_policy_unknown_field",
    )
    required = value.get("requires_user_material", value.get("required", False))
    if "requires_user_material" in value and "required" in value:
        if value["requires_user_material"] != value["required"]:
            raise ResearchInputError("research_input_policy_duplicate_required")
    if not isinstance(required, bool):
        raise ResearchInputError("research_input_required_must_be_boolean")
    input_id = value.get("required_input_id", DEFAULT_REQUIRED_INPUT_ID)
    input_id = _dimension_id(input_id, "required_input_id")
    accepted_form = value.get("accepted_form", "text")
    if accepted_form not in {"text", "json"}:
        raise ResearchInputError("research_input_accepted_form_unsupported")
    max_bytes = value.get("max_bytes", DEFAULT_INPUT_MAX_BYTES)
    if (
        not isinstance(max_bytes, int)
        or isinstance(max_bytes, bool)
        or not 1 <= max_bytes <= MAX_QUESTION_BYTES
    ):
        raise ResearchInputError("research_input_max_bytes_invalid")
    public_route = value.get("public_route_permitted", False)
    if not isinstance(public_route, bool):
        raise ResearchInputError("research_input_public_route_permitted_invalid")
    return InputPolicy(required, input_id, accepted_form, max_bytes, public_route)


def _parse_roles(value: Any) -> Mapping[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ResearchInputError("research_roles_must_be_table")
    result: dict[str, str] = {}
    for role, worker in value.items():
        if not isinstance(role, str) or not _IDENTIFIER.fullmatch(role):
            raise ResearchInputError("research_role_invalid")
        if isinstance(worker, Mapping):
            _reject_unknown(
                worker,
                {"worker", "capabilities"},
                "research_role_unknown_field",
            )
            capabilities = worker.get("capabilities", [])
            if (
                not isinstance(capabilities, list)
                or not all(isinstance(item, str) and item.strip() for item in capabilities)
            ):
                raise ResearchInputError(f"research_role_capabilities_invalid:{role}")
            worker = worker.get("worker")
        if not isinstance(worker, str) or not worker.strip():
            raise ResearchInputError(f"research_role_worker_invalid:{role}")
        result[role] = worker.strip()
    return result


def _reject_unknown(
    value: Mapping[str, Any],
    allowed: set[str] | frozenset[str],
    prefix: str,
) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise ResearchInputError(f"{prefix}:{sorted(unknown, key=str)[0]}")


def _bounded_int(value: Any, field: str, *, maximum: int = MAX_FACETS) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not 1 <= value <= maximum
    ):
        raise ResearchInputError(f"{field}_invalid")
    return value


def _dimension_id(value: Any, kind: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value.strip()):
        raise ResearchInputError(f"decomposition_{kind}_id_invalid")
    return value.strip()


def _cell_key(value: MatrixCell | tuple[str, str] | str) -> str:
    if isinstance(value, MatrixCell):
        return value.id
    if isinstance(value, tuple) and len(value) == 2:
        facet, perspective = value
        if not isinstance(facet, str) or not isinstance(perspective, str):
            raise ResearchInputError("coverage_cell_invalid")
        return f"{facet}:{perspective}"
    if isinstance(value, str) and value:
        return value
    raise ResearchInputError("coverage_cell_invalid")


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()
