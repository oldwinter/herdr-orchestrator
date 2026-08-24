from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from herdr_orchestrator.executor_protocol import canonical_json


RESEARCH_EVIDENCE_SCHEMA_VERSION = 1
EVIDENCE_NORMALIZATION_VERSION = 1
MAX_SOURCE_URL_BYTES = 4096
MAX_EXCERPT_BYTES = 16_384
MAX_CLAIM_BYTES = 16_384

SUPPORTED_CLAIM_TYPES = frozenset(
    {"factual", "quantitative", "causal", "inference", "recommendation"}
)
SUPPORTED_EVIDENCE_RELATIONS = frozenset(
    {"support", "contradiction", "context"}
)
SUPPORTED_SOURCE_OUTCOMES = frozenset(
    {"retrieved", "unavailable", "denied", "changed"}
)
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")


class ResearchEvidenceError(ValueError):
    """Raised when a research source, excerpt, claim, or relation is invalid."""

    code = "research_evidence_invalid"


def _error(code: str) -> ResearchEvidenceError:
    return ResearchEvidenceError(code)


def _identifier(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER.fullmatch(value):
        raise _error(f"{field}_invalid")
    return value


def _identity(value: Any, field: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or "\x00" in value
        or len(value) > 128
    ):
        raise _error(f"{field}_invalid")
    return value


def _digest(value: Any, field: str) -> str:
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise _error(f"{field}_invalid")
    return value


def _non_empty_text(value: Any, field: str, *, max_bytes: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _error(f"{field}_required")
    result = unicodedata.normalize("NFC", value.strip())
    if len(result.encode("utf-8")) > max_bytes:
        raise _error(f"{field}_over_limit")
    return result


def _https_url(value: Any, field: str) -> str:
    result = _non_empty_text(value, field, max_bytes=MAX_SOURCE_URL_BYTES)
    try:
        parsed = urlsplit(result)
        hostname = parsed.hostname
    except ValueError as exc:
        raise _error(f"{field}_not_public_https") from exc
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or hostname is None
    ):
        raise _error(f"{field}_not_public_https")
    return result


def _finite_number(value: Any, field: str) -> int | float:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(float(value))
    ):
        raise _error(f"{field}_invalid")
    return value


def _string_tuple(value: Any, field: str, *, required: bool = False) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _error(f"{field}_invalid")
    result = tuple(_identifier(item, field) for item in value)
    if len(set(result)) != len(result):
        raise _error(f"{field}_duplicate")
    if required and not result:
        raise _error(f"{field}_required")
    return result


def _digest_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def _receipt_digest(value: Mapping[str, Any]) -> str:
    without_digest = {
        key: item for key, item in value.items() if key != "receipt_digest"
    }
    return "sha256:" + hashlib.sha256(
        canonical_json(without_digest).encode("utf-8")
    ).hexdigest()


def _observation_id(
    *,
    requested_url: str,
    final_url: str,
    payload_digest: str,
    retrieval_order: int,
    attempt_id: str,
    run_id: str,
    work_id: str,
    harness: str,
    agent: str,
) -> str:
    identity = canonical_json(
        {
            "requested_url": requested_url,
            "final_url": final_url,
            "payload_digest": payload_digest,
            "retrieval_order": retrieval_order,
            "attempt_id": attempt_id,
            "run_id": run_id,
            "work_id": work_id,
            "harness": harness,
            "agent": agent,
        }
    )
    return "source_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _excerpt_id(
    *,
    source_id: str,
    excerpt_digest: str,
    selector: str | None,
    offset_start: int | None,
    offset_end: int | None,
) -> str:
    identity = canonical_json(
        {
            "source_id": source_id,
            "excerpt_digest": excerpt_digest,
            "selector": selector,
            "offset_start": offset_start,
            "offset_end": offset_end,
        }
    )
    return "excerpt_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()


def _validate_redirect_chain(value: Any) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise _error("source_redirect_chain_invalid")
    result = tuple(
        _https_url(item, "source_redirect_url")
        for item in value
    )
    if len(set(result)) != len(result):
        raise _error("source_redirect_chain_duplicate")
    return result


@dataclass(frozen=True, slots=True)
class SourceReceipt:
    """One immutable observation of a readable public HTTPS response."""

    source_id: str
    requested_url: str
    final_url: str
    redirect_chain: tuple[str, ...]
    method: str
    retrieval_order: int
    retrieved_at: int | float
    outcome: str
    status: int
    content_type: str
    payload_digest: str
    encoding: str
    normalization_version: int
    role: str
    run_id: str
    work_id: str
    attempt_id: str
    fencing_token: str
    harness: str
    worker: str
    agent: str
    pane: str | None
    schema_version: int = RESEARCH_EVIDENCE_SCHEMA_VERSION

    EXACT_KEYS = frozenset(
        {
            "schema_version",
            "source_id",
            "requested_url",
            "final_url",
            "redirect_chain",
            "method",
            "retrieval_order",
            "retrieved_at",
            "outcome",
            "status",
            "content_type",
            "payload_digest",
            "encoding",
            "normalization_version",
            "role",
            "run_id",
            "work_id",
            "attempt_id",
            "fencing_token",
            "harness",
            "worker",
            "agent",
            "pane",
            "receipt_digest",
        }
    )
    REQUIRED_KEYS = EXACT_KEYS - {"receipt_digest"}

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _https_url(self.requested_url, "source_requested_url")
        _https_url(self.final_url, "source_final_url")
        if (
            isinstance(self.redirect_chain, (str, bytes))
            or not isinstance(self.redirect_chain, tuple)
        ):
            raise _error("source_redirect_chain_invalid")
        _validate_redirect_chain(self.redirect_chain)
        if self.method != "GET":
            raise _error("source_method_must_be_get")
        if (
            not isinstance(self.retrieval_order, int)
            or isinstance(self.retrieval_order, bool)
            or self.retrieval_order < 1
        ):
            raise _error("source_retrieval_order_invalid")
        _finite_number(self.retrieved_at, "source_retrieved_at")
        if self.outcome not in SUPPORTED_SOURCE_OUTCOMES:
            raise _error("source_outcome_unsupported")
        if (
            not isinstance(self.status, int)
            or isinstance(self.status, bool)
            or not 0 <= self.status <= 599
        ):
            raise _error("source_status_invalid")
        if self.outcome == "retrieved" and not 200 <= self.status < 300:
            raise _error("source_retrieved_status_invalid")
        _non_empty_text(self.content_type, "source_content_type", max_bytes=256)
        _digest(self.payload_digest, "source_payload_digest")
        _non_empty_text(self.encoding, "source_encoding", max_bytes=64)
        if (
            not isinstance(self.normalization_version, int)
            or isinstance(self.normalization_version, bool)
            or self.normalization_version != EVIDENCE_NORMALIZATION_VERSION
        ):
            raise _error("source_normalization_version_unsupported")
        _identifier(self.role, "source_role")
        for field, value in (
            ("run_id", self.run_id),
            ("work_id", self.work_id),
            ("attempt_id", self.attempt_id),
            ("fencing_token", self.fencing_token),
            ("harness", self.harness),
            ("worker", self.worker),
            ("agent", self.agent),
        ):
            _identifier(value, field)
        if self.pane is not None:
            _identity(self.pane, "pane")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != RESEARCH_EVIDENCE_SCHEMA_VERSION
        ):
            raise _error("source_schema_version_unsupported")

    @classmethod
    def from_retrieval(
        cls,
        *,
        requested_url: str,
        final_url: str,
        redirect_chain: tuple[str, ...] | list[str],
        retrieved_content: bytes | None,
        retrieval_order: int,
        retrieved_at: int | float,
        content_type: str,
        role: str,
        run_id: str,
        work_id: str,
        attempt_id: str,
        fencing_token: str,
        harness: str,
        worker: str,
        agent: str,
        pane: str | None,
        source_id: str | None = None,
        status: int = 200,
        encoding: str = "utf-8",
        normalization_version: int = EVIDENCE_NORMALIZATION_VERSION,
        outcome: str = "retrieved",
    ) -> SourceReceipt:
        if retrieved_content is None:
            if outcome == "retrieved":
                raise _error("source_payload_bytes_required")
            retrieved_content = b""
        if not isinstance(retrieved_content, bytes):
            raise _error("source_payload_bytes_required")
        payload_digest = _digest_bytes(retrieved_content)
        requested = _https_url(requested_url, "source_requested_url")
        final = _https_url(final_url, "source_final_url")
        redirects = _validate_redirect_chain(redirect_chain)
        attempt = _identifier(attempt_id, "attempt_id")
        actual_source_id = source_id or _observation_id(
            requested_url=requested,
            final_url=final,
            payload_digest=payload_digest,
            retrieval_order=retrieval_order,
            attempt_id=attempt,
            run_id=_identifier(run_id, "run_id"),
            work_id=_identifier(work_id, "work_id"),
            harness=_identifier(harness, "harness"),
            agent=_identifier(agent, "agent"),
        )
        return cls(
            source_id=actual_source_id,
            requested_url=requested,
            final_url=final,
            redirect_chain=redirects,
            method="GET",
            retrieval_order=retrieval_order,
            retrieved_at=retrieved_at,
            outcome=outcome,
            status=status,
            content_type=content_type,
            payload_digest=payload_digest,
            encoding=encoding,
            normalization_version=normalization_version,
            role=role,
            run_id=run_id,
            work_id=work_id,
            attempt_id=attempt,
            fencing_token=fencing_token,
            harness=harness,
            worker=worker,
            agent=agent,
            pane=pane,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SourceReceipt:
        if not isinstance(value, Mapping):
            raise _error("source_receipt_object_required")
        unknown = set(value) - cls.EXACT_KEYS
        missing = cls.REQUIRED_KEYS - set(value)
        if unknown:
            raise _error(f"source_receipt_unknown_field:{sorted(unknown)[0]}")
        if missing:
            raise _error(f"source_receipt_missing_field:{sorted(missing)[0]}")
        schema_version = value["schema_version"]
        if schema_version != RESEARCH_EVIDENCE_SCHEMA_VERSION:
            raise _error("source_schema_version_unsupported")
        receipt = cls(
            source_id=_identifier(value["source_id"], "source_id"),
            requested_url=_https_url(value["requested_url"], "source_requested_url"),
            final_url=_https_url(value["final_url"], "source_final_url"),
            redirect_chain=_validate_redirect_chain(value["redirect_chain"]),
            method=value["method"],
            retrieval_order=value["retrieval_order"],
            retrieved_at=value["retrieved_at"],
            outcome=value["outcome"],
            status=value["status"],
            content_type=value["content_type"],
            payload_digest=value["payload_digest"],
            encoding=value["encoding"],
            normalization_version=value["normalization_version"],
            role=value["role"],
            run_id=value["run_id"],
            work_id=value["work_id"],
            attempt_id=value["attempt_id"],
            fencing_token=value["fencing_token"],
            harness=value["harness"],
            worker=value["worker"],
            agent=value["agent"],
            pane=value["pane"],
            schema_version=schema_version,
        )
        supplied_digest = value.get("receipt_digest")
        if supplied_digest is not None:
            if supplied_digest != receipt.receipt_digest:
                raise _error("source_receipt_digest_mismatch")
        return receipt

    @property
    def payload_digest_alias(self) -> str:
        return self.payload_digest

    @property
    def content_digest(self) -> str:
        return self.payload_digest

    @property
    def source_digest(self) -> str:
        return self.payload_digest

    @property
    def digest(self) -> str:
        return self.payload_digest

    @property
    def status_code(self) -> int:
        return self.status

    @property
    def attribution(self) -> dict[str, str | None]:
        return {
            "role": self.role,
            "run_id": self.run_id,
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "harness": self.harness,
            "worker": self.worker,
            "agent": self.agent,
            "pane": self.pane,
        }

    @property
    def receipt_digest(self) -> str:
        return _receipt_digest(self.to_dict(include_receipt_digest=False))

    def to_dict(self, *, include_receipt_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "source_id": self.source_id,
            "requested_url": self.requested_url,
            "final_url": self.final_url,
            "redirect_chain": list(self.redirect_chain),
            "method": self.method,
            "retrieval_order": self.retrieval_order,
            "retrieved_at": self.retrieved_at,
            "outcome": self.outcome,
            "status": self.status,
            "content_type": self.content_type,
            "payload_digest": self.payload_digest,
            "encoding": self.encoding,
            "normalization_version": self.normalization_version,
            "role": self.role,
            "run_id": self.run_id,
            "work_id": self.work_id,
            "attempt_id": self.attempt_id,
            "fencing_token": self.fencing_token,
            "harness": self.harness,
            "worker": self.worker,
            "agent": self.agent,
            "pane": self.pane,
        }
        if include_receipt_digest:
            result["receipt_digest"] = self.receipt_digest
        return result


@dataclass(frozen=True, slots=True)
class ExcerptReceipt:
    """A bounded excerpt captured from one admitted source observation."""

    excerpt_id: str
    source_id: str
    source_digest: str
    capture_kind: str
    text: str
    selector: str | None
    offset_start: int | None
    offset_end: int | None
    encoding: str
    normalization_version: int
    excerpt_digest: str
    schema_version: int = RESEARCH_EVIDENCE_SCHEMA_VERSION

    EXACT_KEYS = frozenset(
        {
            "schema_version",
            "excerpt_id",
            "source_id",
            "source_digest",
            "capture_kind",
            "text",
            "selector",
            "offset_start",
            "offset_end",
            "encoding",
            "normalization_version",
            "excerpt_digest",
            "receipt_digest",
        }
    )
    REQUIRED_KEYS = EXACT_KEYS - {"receipt_digest"}

    def __post_init__(self) -> None:
        _identifier(self.excerpt_id, "excerpt_id")
        _identifier(self.source_id, "excerpt_source_id")
        _digest(self.source_digest, "excerpt_source_digest")
        if self.capture_kind != "document":
            raise _error("excerpt_search_snippet_not_evidence")
        normalized = _non_empty_text(
            self.text,
            "excerpt_text",
            max_bytes=MAX_EXCERPT_BYTES,
        )
        if normalized != self.text:
            raise _error("excerpt_text_not_normalized")
        try:
            parsed_text = urlsplit(self.text)
        except ValueError:
            parsed_text = None
        if parsed_text is not None and parsed_text.scheme == "https" and parsed_text.netloc:
            raise _error("excerpt_bare_url_not_evidence")
        if self.selector is None and (
            self.offset_start is None or self.offset_end is None
        ):
            raise _error("excerpt_selector_or_offset_required")
        if self.selector is not None:
            _non_empty_text(self.selector, "excerpt_selector", max_bytes=1024)
        if self.offset_start is not None or self.offset_end is not None:
            if (
                not isinstance(self.offset_start, int)
                or isinstance(self.offset_start, bool)
                or not isinstance(self.offset_end, int)
                or isinstance(self.offset_end, bool)
                or self.offset_start < 0
                or self.offset_end <= self.offset_start
                or self.offset_end > len(self.text)
            ):
                if (
                    isinstance(self.offset_start, int)
                    and not isinstance(self.offset_start, bool)
                    and isinstance(self.offset_end, int)
                    and not isinstance(self.offset_end, bool)
                    and self.offset_start >= 0
                    and self.offset_end > self.offset_start
                    and self.offset_end > len(self.text)
                ):
                    raise _error("excerpt_offsets_out_of_bounds")
                raise _error("excerpt_offsets_invalid")
        _non_empty_text(self.encoding, "excerpt_encoding", max_bytes=64)
        if (
            not isinstance(self.normalization_version, int)
            or isinstance(self.normalization_version, bool)
            or self.normalization_version != EVIDENCE_NORMALIZATION_VERSION
        ):
            raise _error("excerpt_normalization_version_unsupported")
        _digest(self.excerpt_digest, "excerpt_digest")
        if _digest_text(self.text) != self.excerpt_digest:
            raise _error("excerpt_digest_mismatch")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != RESEARCH_EVIDENCE_SCHEMA_VERSION
        ):
            raise _error("excerpt_schema_version_unsupported")

    @classmethod
    def from_text(
        cls,
        source: SourceReceipt,
        *,
        text: str,
        selector: str | None = None,
        offset_start: int | None = None,
        offset_end: int | None = None,
        encoding: str | None = None,
        normalization_version: int = EVIDENCE_NORMALIZATION_VERSION,
        excerpt_id: str | None = None,
    ) -> ExcerptReceipt:
        if not isinstance(source, SourceReceipt):
            raise _error("excerpt_source_required")
        normalized = _non_empty_text(text, "excerpt_text", max_bytes=MAX_EXCERPT_BYTES)
        digest = _digest_text(normalized)
        actual_id = excerpt_id or _excerpt_id(
            source_id=source.source_id,
            excerpt_digest=digest,
            selector=selector,
            offset_start=offset_start,
            offset_end=offset_end,
        )
        return cls(
            excerpt_id=actual_id,
            source_id=source.source_id,
            source_digest=source.payload_digest,
            capture_kind="document",
            text=normalized,
            selector=selector,
            offset_start=offset_start,
            offset_end=offset_end,
            encoding=encoding or source.encoding,
            normalization_version=normalization_version,
            excerpt_digest=digest,
        )

    @classmethod
    def from_content(
        cls,
        source: SourceReceipt,
        *,
        source_content: bytes,
        text: str,
        selector: str | None = None,
        offset_start: int | None = None,
        offset_end: int | None = None,
    ) -> ExcerptReceipt:
        if not isinstance(source, SourceReceipt):
            raise _error("excerpt_source_required")
        if not isinstance(source_content, bytes):
            raise _error("excerpt_source_payload_bytes_required")
        content_digest = _digest_bytes(source_content)
        if content_digest != source.payload_digest:
            raise _error("excerpt_source_payload_digest_mismatch")
        try:
            normalized_content = unicodedata.normalize(
                "NFC",
                source_content.decode(source.encoding),
            )
        except (LookupError, UnicodeDecodeError) as exc:
            raise _error("excerpt_source_decode_failed") from exc
        normalized_text = _non_empty_text(
            text,
            "excerpt_text",
            max_bytes=MAX_EXCERPT_BYTES,
        )
        if normalized_text not in normalized_content:
            raise _error("excerpt_text_not_in_source")
        return cls.from_text(
            source,
            text=normalized_text,
            selector=selector,
            offset_start=offset_start,
            offset_end=offset_end,
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ExcerptReceipt:
        if not isinstance(value, Mapping):
            raise _error("excerpt_receipt_object_required")
        if value.get("capture_kind") == "search_snippet":
            raise _error("excerpt_search_snippet_not_evidence")
        unknown = set(value) - cls.EXACT_KEYS
        missing = cls.REQUIRED_KEYS - set(value)
        if unknown:
            raise _error(f"excerpt_receipt_unknown_field:{sorted(unknown)[0]}")
        if missing:
            raise _error(f"excerpt_receipt_missing_field:{sorted(missing)[0]}")
        receipt = cls(
            excerpt_id=value["excerpt_id"],
            source_id=value["source_id"],
            source_digest=value["source_digest"],
            capture_kind=value["capture_kind"],
            text=value["text"],
            selector=value["selector"],
            offset_start=value["offset_start"],
            offset_end=value["offset_end"],
            encoding=value["encoding"],
            normalization_version=value["normalization_version"],
            excerpt_digest=value["excerpt_digest"],
            schema_version=value["schema_version"],
        )
        supplied_digest = value.get("receipt_digest")
        if supplied_digest is not None:
            if supplied_digest != receipt.receipt_digest:
                raise _error("excerpt_receipt_digest_mismatch")
        return receipt

    @property
    def receipt_digest(self) -> str:
        return _receipt_digest(self.to_dict(include_receipt_digest=False))

    @property
    def digest(self) -> str:
        return self.excerpt_digest

    @property
    def source_payload_digest(self) -> str:
        return self.source_digest

    def to_dict(self, *, include_receipt_digest: bool = True) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "excerpt_id": self.excerpt_id,
            "source_id": self.source_id,
            "source_digest": self.source_digest,
            "capture_kind": self.capture_kind,
            "text": self.text,
            "selector": self.selector,
            "offset_start": self.offset_start,
            "offset_end": self.offset_end,
            "encoding": self.encoding,
            "normalization_version": self.normalization_version,
            "excerpt_digest": self.excerpt_digest,
        }
        if include_receipt_digest:
            result["receipt_digest"] = self.receipt_digest
        return result


@dataclass(frozen=True, slots=True)
class TypedClaim:
    """One canonical claim with a closed, machine-checkable type contract."""

    claim_id: str
    claim_type: str
    text: str
    quantity: int | float | None = None
    unit: str | None = None
    value_range: Mapping[str, int | float] | None = None
    time: str | None = None
    population: str | None = None
    causal_direction: str | None = None
    premise_ids: tuple[str, ...] = ()
    evidence_ids: tuple[str, ...] = ()
    critical: bool = False
    facet_ids: tuple[str, ...] = ()
    perspective_ids: tuple[str, ...] = ()
    schema_version: int = RESEARCH_EVIDENCE_SCHEMA_VERSION

    EXACT_KEYS = frozenset(
        {
            "schema_version",
            "claim_id",
            "claim_type",
            "text",
            "quantity",
            "unit",
            "value_range",
            "range",
            "time",
            "population",
            "causal_direction",
            "premise_ids",
            "evidence_ids",
            "critical",
            "facet_ids",
            "perspective_ids",
        }
    )
    REQUIRED_KEYS = frozenset({"schema_version", "claim_id", "claim_type", "text"})

    def __post_init__(self) -> None:
        _identifier(self.claim_id, "claim_id")
        if self.claim_type not in SUPPORTED_CLAIM_TYPES:
            raise _error("claim_type_unsupported")
        normalized_text = _non_empty_text(
            self.text,
            "claim_text",
            max_bytes=MAX_CLAIM_BYTES,
        )
        if normalized_text != self.text:
            object.__setattr__(self, "text", normalized_text)
        object.__setattr__(self, "premise_ids", _string_tuple(self.premise_ids, "claim_premise_ids"))
        object.__setattr__(self, "evidence_ids", _string_tuple(self.evidence_ids, "claim_evidence_ids"))
        object.__setattr__(self, "facet_ids", _string_tuple(self.facet_ids, "claim_facet_ids"))
        object.__setattr__(
            self,
            "perspective_ids",
            _string_tuple(self.perspective_ids, "claim_perspective_ids"),
        )
        if not isinstance(self.critical, bool):
            raise _error("claim_critical_invalid")
        if self.quantity is not None:
            _finite_number(self.quantity, "claim_quantity")
        if self.unit is not None:
            _non_empty_text(self.unit, "claim_unit", max_bytes=256)
        if self.value_range is not None:
            if not isinstance(self.value_range, Mapping):
                raise _error("claim_value_range_invalid")
            if set(self.value_range) != {"min", "max"}:
                raise _error("claim_value_range_invalid")
            minimum = _finite_number(self.value_range["min"], "claim_value_range_min")
            maximum = _finite_number(self.value_range["max"], "claim_value_range_max")
            if minimum > maximum:
                raise _error("claim_value_range_invalid")
            object.__setattr__(
                self,
                "value_range",
                {"min": minimum, "max": maximum},
            )
        if self.time is not None:
            _non_empty_text(self.time, "claim_time", max_bytes=256)
        if self.population is not None:
            _non_empty_text(self.population, "claim_population", max_bytes=1024)
        if self.causal_direction is not None:
            _non_empty_text(
                self.causal_direction,
                "claim_causal_direction",
                max_bytes=1024,
            )
        if self.claim_type == "quantitative":
            if (
                self.quantity is None
                or self.unit is None
                or self.value_range is None
                or self.time is None
                or self.population is None
            ):
                raise _error("claim_quantitative_fields_required")
        elif self.claim_type == "causal":
            if self.causal_direction is None:
                raise _error("claim_causal_direction_required")
        elif self.claim_type in {"inference", "recommendation"}:
            if not self.premise_ids:
                raise _error("claim_premise_ids_required")
            if not self.evidence_ids:
                raise _error("claim_evidence_ids_required")
        if self.claim_id in self.premise_ids:
            raise _error("claim_premise_self_reference")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != RESEARCH_EVIDENCE_SCHEMA_VERSION
        ):
            raise _error("claim_schema_version_unsupported")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TypedClaim:
        if not isinstance(value, Mapping):
            raise _error("claim_object_required")
        unknown = set(value) - cls.EXACT_KEYS
        missing = cls.REQUIRED_KEYS - set(value)
        if unknown:
            raise _error(f"claim_unknown_field:{sorted(unknown)[0]}")
        if missing:
            raise _error(f"claim_missing_field:{sorted(missing)[0]}")
        if "value_range" in value and "range" in value:
            raise _error("claim_range_duplicate")
        return cls(
            claim_id=value["claim_id"],
            claim_type=value["claim_type"],
            text=value["text"],
            quantity=value.get("quantity"),
            unit=value.get("unit"),
            value_range=value.get("value_range", value.get("range")),
            time=value.get("time"),
            population=value.get("population"),
            causal_direction=value.get("causal_direction"),
            premise_ids=value.get("premise_ids", ()),
            evidence_ids=value.get("evidence_ids", ()),
            critical=value.get("critical", False),
            facet_ids=value.get("facet_ids", ()),
            perspective_ids=value.get("perspective_ids", ()),
            schema_version=value["schema_version"],
        )

    @property
    def type(self) -> str:
        return self.claim_type

    @property
    def kind(self) -> str:
        return self.claim_type

    @property
    def id(self) -> str:
        return self.claim_id

    @property
    def range(self) -> Mapping[str, int | float] | None:
        return self.value_range

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "claim_id": self.claim_id,
            "claim_type": self.claim_type,
            "text": self.text,
            "quantity": self.quantity,
            "unit": self.unit,
            "value_range": (
                None if self.value_range is None else dict(self.value_range)
            ),
            "time": self.time,
            "population": self.population,
            "causal_direction": self.causal_direction,
            "premise_ids": list(self.premise_ids),
            "evidence_ids": list(self.evidence_ids),
            "critical": self.critical,
            "facet_ids": list(self.facet_ids),
            "perspective_ids": list(self.perspective_ids),
        }


@dataclass(frozen=True, slots=True)
class EvidenceRelation:
    """One closed-enum relation between a claim and an admitted excerpt."""

    relation_id: str
    claim_id: str
    relation: str
    source_id: str
    excerpt_id: str
    schema_version: int = RESEARCH_EVIDENCE_SCHEMA_VERSION

    EXACT_KEYS = frozenset(
        {
            "schema_version",
            "relation_id",
            "claim_id",
            "relation",
            "source_id",
            "excerpt_id",
        }
    )

    def __post_init__(self) -> None:
        _identifier(self.relation_id, "evidence_relation_id")
        _identifier(self.claim_id, "evidence_relation_claim_id")
        if self.relation not in SUPPORTED_EVIDENCE_RELATIONS:
            raise _error("evidence_relation_unknown_relation")
        _identifier(self.source_id, "evidence_relation_source_id")
        _identifier(self.excerpt_id, "evidence_relation_excerpt_id")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != RESEARCH_EVIDENCE_SCHEMA_VERSION
        ):
            raise _error("evidence_relation_schema_version_unsupported")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> EvidenceRelation:
        if not isinstance(value, Mapping):
            raise _error("evidence_relation_object_required")
        unknown = set(value) - cls.EXACT_KEYS
        missing = cls.EXACT_KEYS - set(value)
        if unknown:
            raise _error(f"evidence_relation_unknown_field:{sorted(unknown)[0]}")
        if missing:
            raise _error(f"evidence_relation_missing_field:{sorted(missing)[0]}")
        return cls(
            relation_id=value["relation_id"],
            claim_id=value["claim_id"],
            relation=value["relation"],
            source_id=value["source_id"],
            excerpt_id=value["excerpt_id"],
            schema_version=value["schema_version"],
        )

    @property
    def evidence_id(self) -> str:
        return self.relation_id

    @property
    def kind(self) -> str:
        return self.relation

    @property
    def id(self) -> str:
        return self.relation_id

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "relation_id": self.relation_id,
            "claim_id": self.claim_id,
            "relation": self.relation,
            "source_id": self.source_id,
            "excerpt_id": self.excerpt_id,
        }


class ResearchEvidenceRegister:
    """Append-only admission register for immutable research evidence."""

    def __init__(self) -> None:
        self._sources: dict[str, SourceReceipt] = {}
        self._excerpts: dict[str, ExcerptReceipt] = {}
        self._claims: dict[str, TypedClaim] = {}
        self._relations: dict[str, EvidenceRelation] = {}

    @property
    def sources(self) -> tuple[SourceReceipt, ...]:
        return tuple(self._sources[key] for key in sorted(self._sources))

    @property
    def excerpts(self) -> tuple[ExcerptReceipt, ...]:
        return tuple(self._excerpts[key] for key in sorted(self._excerpts))

    @property
    def claims(self) -> tuple[TypedClaim, ...]:
        return tuple(self._claims[key] for key in sorted(self._claims))

    @property
    def relations(self) -> tuple[EvidenceRelation, ...]:
        return tuple(self._relations[key] for key in sorted(self._relations))

    @property
    def creditable_relation_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._relations))

    def admit_source(
        self,
        value: SourceReceipt | Mapping[str, Any],
    ) -> SourceReceipt:
        source = value if isinstance(value, SourceReceipt) else SourceReceipt.from_mapping(value)
        existing = self._sources.get(source.source_id)
        if existing is not None:
            if existing != source:
                raise _error("source_receipt_immutable_conflict")
            return existing
        self._sources[source.source_id] = source
        return source

    def admit_excerpt(
        self,
        value: ExcerptReceipt | Mapping[str, Any],
    ) -> ExcerptReceipt:
        excerpt = value if isinstance(value, ExcerptReceipt) else ExcerptReceipt.from_mapping(value)
        source = self._sources.get(excerpt.source_id)
        if source is None:
            raise _error("excerpt_source_not_admitted")
        if source.outcome in {"unavailable", "denied"}:
            raise _error("excerpt_source_not_retrieved")
        if source.payload_digest != excerpt.source_digest:
            raise _error("excerpt_source_digest_mismatch")
        existing = self._excerpts.get(excerpt.excerpt_id)
        if existing is not None:
            if existing != excerpt:
                raise _error("excerpt_receipt_immutable_conflict")
            return existing
        self._excerpts[excerpt.excerpt_id] = excerpt
        return excerpt

    def admit_claim(
        self,
        value: TypedClaim | Mapping[str, Any],
    ) -> TypedClaim:
        claim = value if isinstance(value, TypedClaim) else TypedClaim.from_mapping(value)
        for premise_id in claim.premise_ids:
            if premise_id not in self._claims:
                raise _error(f"claim_premise_not_admitted:{premise_id}")
        for evidence_id in claim.evidence_ids:
            if evidence_id not in self._relations:
                raise _error(f"claim_evidence_not_admitted:{evidence_id}")
        existing = self._claims.get(claim.claim_id)
        if existing is not None:
            if existing != claim:
                raise _error("claim_immutable_conflict")
            return existing
        self._claims[claim.claim_id] = claim
        return claim

    def admit_relation(
        self,
        value: EvidenceRelation | Mapping[str, Any],
    ) -> EvidenceRelation:
        relation = value if isinstance(value, EvidenceRelation) else EvidenceRelation.from_mapping(value)
        if relation.claim_id not in self._claims:
            raise _error(f"evidence_relation_claim_not_admitted:{relation.claim_id}")
        source = self._sources.get(relation.source_id)
        if source is None:
            raise _error(f"evidence_relation_source_not_admitted:{relation.source_id}")
        excerpt = self._excerpts.get(relation.excerpt_id)
        if excerpt is None:
            raise _error(f"evidence_relation_excerpt_not_admitted:{relation.excerpt_id}")
        if excerpt.source_id != source.source_id or excerpt.source_digest != source.payload_digest:
            raise _error("evidence_relation_lineage_mismatch")
        existing = self._relations.get(relation.relation_id)
        if existing is not None:
            if existing != relation:
                raise _error("evidence_relation_immutable_conflict")
            return existing
        self._relations[relation.relation_id] = relation
        return relation

    def admit_bundle(self, value: Mapping[str, Any]) -> ResearchEvidenceRegister:
        """Atomically admit a complete source/excerpt/claim/relation bundle."""

        candidate = type(self).from_mapping(value)
        for current, incoming, conflict in (
            (self._sources, candidate._sources, "source_receipt_immutable_conflict"),
            (self._excerpts, candidate._excerpts, "excerpt_receipt_immutable_conflict"),
            (self._claims, candidate._claims, "claim_immutable_conflict"),
            (self._relations, candidate._relations, "evidence_relation_immutable_conflict"),
        ):
            for key, item in incoming.items():
                if key in current and current[key] != item:
                    raise _error(conflict)
        self._sources.update(candidate._sources)
        self._excerpts.update(candidate._excerpts)
        self._claims.update(candidate._claims)
        self._relations.update(candidate._relations)
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESEARCH_EVIDENCE_SCHEMA_VERSION,
            "sources": [source.to_dict() for source in self.sources],
            "excerpts": [excerpt.to_dict() for excerpt in self.excerpts],
            "claims": [claim.to_dict() for claim in self.claims],
            "relations": [relation.to_dict() for relation in self.relations],
            "creditable_relation_ids": list(self.creditable_relation_ids),
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ResearchEvidenceRegister:
        if not isinstance(value, Mapping):
            raise _error("research_evidence_register_object_required")
        expected = {
            "schema_version",
            "sources",
            "excerpts",
            "claims",
            "relations",
            "creditable_relation_ids",
        }
        required = expected - {"creditable_relation_ids"}
        unknown = set(value) - expected
        missing = required - set(value)
        if unknown:
            raise _error(f"research_evidence_register_unknown_field:{sorted(unknown)[0]}")
        if missing:
            raise _error(f"research_evidence_register_missing_field:{sorted(missing)[0]}")
        if value["schema_version"] != RESEARCH_EVIDENCE_SCHEMA_VERSION:
            raise _error("research_evidence_schema_version_unsupported")
        register = cls()
        sources = value["sources"]
        excerpts = value["excerpts"]
        claims = value["claims"]
        relations = value["relations"]
        for field, items in (
            ("sources", sources),
            ("excerpts", excerpts),
            ("claims", claims),
            ("relations", relations),
        ):
            if isinstance(items, (str, bytes)) or not isinstance(items, list):
                raise _error(f"research_evidence_{field}_must_be_array")
        for source in sources:
            register.admit_source(source)
        for excerpt in excerpts:
            register.admit_excerpt(excerpt)
        # Relations can be admitted before claims are finalized so inference
        # and recommendation evidence IDs can point at another already
        # receipted claim in the same bundle.  The final pass below enforces
        # every claim's premise/evidence references.
        parsed_claims = [TypedClaim.from_mapping(claim) for claim in claims]
        all_claim_ids = {item.claim_id for item in parsed_claims}
        if len(all_claim_ids) != len(parsed_claims):
            raise _error("claim_duplicate_id")
        for claim in parsed_claims:
            for premise_id in claim.premise_ids:
                if premise_id not in all_claim_ids:
                    raise _error(f"claim_premise_not_admitted:{premise_id}")
            existing = register._claims.get(claim.claim_id)
            if existing is not None and existing != claim:
                raise _error("claim_immutable_conflict")
            register._claims[claim.claim_id] = claim
        for relation in relations:
            register.admit_relation(relation)
        relation_ids = set(register._relations)
        for claim in register._claims.values():
            for evidence_id in claim.evidence_ids:
                if evidence_id not in relation_ids:
                    raise _error(f"claim_evidence_not_admitted:{evidence_id}")
        declared_credit = value.get("creditable_relation_ids")
        if declared_credit is not None:
            if (
                isinstance(declared_credit, (str, bytes))
                or not isinstance(declared_credit, list)
                or tuple(declared_credit) != register.creditable_relation_ids
            ):
                raise _error("creditable_relation_ids_mismatch")
        return register


def _digest_bytes(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def parse_source_receipt(value: Mapping[str, Any]) -> SourceReceipt:
    return SourceReceipt.from_mapping(value)


def parse_excerpt_receipt(value: Mapping[str, Any]) -> ExcerptReceipt:
    return ExcerptReceipt.from_mapping(value)


def parse_typed_claim(value: Mapping[str, Any]) -> TypedClaim:
    return TypedClaim.from_mapping(value)


def parse_evidence_relation(value: Mapping[str, Any]) -> EvidenceRelation:
    return EvidenceRelation.from_mapping(value)


def parse_evidence_register(value: Mapping[str, Any]) -> ResearchEvidenceRegister:
    return ResearchEvidenceRegister.from_mapping(value)


validate_source_receipt = parse_source_receipt
validate_excerpt_receipt = parse_excerpt_receipt
validate_claim = parse_typed_claim
validate_evidence_relation = parse_evidence_relation
validate_evidence_register = parse_evidence_register


# Names used by the research executor and by public artifact producers.
SourceExcerpt = ExcerptReceipt
SourceExcerptReceipt = ExcerptReceipt
Claim = TypedClaim
ClaimRecord = TypedClaim
EvidenceRelationRecord = EvidenceRelation


__all__ = [
    "Claim",
    "ClaimRecord",
    "EVIDENCE_NORMALIZATION_VERSION",
    "EvidenceRelation",
    "EvidenceRelationRecord",
    "ExcerptReceipt",
    "MAX_CLAIM_BYTES",
    "MAX_EXCERPT_BYTES",
    "MAX_SOURCE_URL_BYTES",
    "parse_evidence_relation",
    "parse_evidence_register",
    "parse_excerpt_receipt",
    "parse_source_receipt",
    "parse_typed_claim",
    "RESEARCH_EVIDENCE_SCHEMA_VERSION",
    "ResearchEvidenceError",
    "ResearchEvidenceRegister",
    "SUPPORTED_CLAIM_TYPES",
    "SUPPORTED_EVIDENCE_RELATIONS",
    "SUPPORTED_SOURCE_OUTCOMES",
    "SourceExcerpt",
    "SourceExcerptReceipt",
    "SourceReceipt",
    "TypedClaim",
    "validate_claim",
    "validate_evidence_relation",
    "validate_evidence_register",
    "validate_excerpt_receipt",
    "validate_source_receipt",
]
