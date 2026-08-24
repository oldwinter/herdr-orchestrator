from __future__ import annotations

import hashlib
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
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

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> ResearchConfig:
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ResearchInputError("research_config_must_be_table")
        allowed = {"decomposition", "coverage", "input", "roles", "verification"}
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
        return cls(decomposition, coverage, input_policy, roles, verification)

    def to_dict(self) -> dict[str, object]:
        return {
            "decomposition": self.decomposition.to_dict(),
            "coverage": self.coverage.to_dict(),
            "input": self.input_policy.to_dict(),
            "roles": dict(self.roles),
            "verification": self.verification.to_dict(),
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
    return {
        "input": value.to_dict(),
        "classification": classification.to_dict(),
        "decomposition": decomposition.to_dict(),
        "coverage": coverage.to_dict(),
        "phase": "input_required" if classification.requires_input else "decomposed",
        "required_input": required_input,
        "verification_policy": policy.to_dict(),
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
