from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from typing import Any, Callable
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
VERIFICATION_SCHEMA_VERSION = 1
SUPPORTED_VERIFICATION_DISPOSITIONS = frozenset(
    {"contested", "verified", "unverified", "rejected"}
)
_IDENTIFIER = re.compile(r"[a-z][a-z0-9_-]{0,127}\Z")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}\Z")
_POLICY_UNSET = object()


class ResearchEvidenceError(ValueError):
    """Raised when a research source, excerpt, claim, or relation is invalid."""

    code = "research_evidence_invalid"


def _error(code: str) -> ResearchEvidenceError:
    return ResearchEvidenceError(code)


@dataclass(frozen=True, slots=True)
class CriticalityPolicy:
    """Pinned policy for deriving which claims require independent verification.

    ``TypedClaim.critical`` is intentionally only producer metadata.  A claim
    is critical when its type is in ``critical_claim_types`` and it is used by
    a required conclusion.  An empty ``required_conclusion_ids`` policy means
    that any explicit required-conclusion use is in scope; a non-empty policy
    restricts that use to the pinned conclusion IDs.
    """

    critical_claim_types: tuple[str, ...] = ("factual", "quantitative", "causal")
    required_conclusion_ids: tuple[str, ...] = ()
    require_independent_logical_agent: bool = True
    require_harness_separation: bool = True
    forbid_source_reuse: bool = True
    policy_version: int = VERIFICATION_SCHEMA_VERSION

    EXACT_KEYS = frozenset(
        {
            "policy_version",
            "critical_claim_types",
            "required_conclusion_ids",
            "required_conclusions",
            "require_independent_logical_agent",
            "require_harness_separation",
            "forbid_source_reuse",
        }
    )

    def __post_init__(self) -> None:
        types = _string_tuple(
            self.critical_claim_types,
            "criticality_claim_types",
            required=True,
        )
        if not set(types).issubset(SUPPORTED_CLAIM_TYPES):
            unsupported = sorted(set(types) - SUPPORTED_CLAIM_TYPES)[0]
            raise _error(f"criticality_claim_type_unsupported:{unsupported}")
        conclusion_ids = _string_tuple(
            self.required_conclusion_ids,
            "criticality_required_conclusion_ids",
        )
        if (
            not isinstance(self.require_independent_logical_agent, bool)
            or not isinstance(self.require_harness_separation, bool)
            or not isinstance(self.forbid_source_reuse, bool)
        ):
            raise _error("criticality_policy_boolean_invalid")
        if (
            not isinstance(self.policy_version, int)
            or isinstance(self.policy_version, bool)
            or self.policy_version != VERIFICATION_SCHEMA_VERSION
        ):
            raise _error("criticality_policy_version_unsupported")
        object.__setattr__(self, "critical_claim_types", types)
        object.__setattr__(self, "required_conclusion_ids", conclusion_ids)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any] | None) -> CriticalityPolicy:
        if value is None:
            return cls()
        if not isinstance(value, Mapping):
            raise _error("criticality_policy_object_required")
        unknown = set(value) - cls.EXACT_KEYS
        if unknown:
            raise _error(f"criticality_policy_unknown_field:{sorted(unknown)[0]}")
        if "required_conclusion_ids" in value and "required_conclusions" in value:
            if value["required_conclusion_ids"] != value["required_conclusions"]:
                raise _error("criticality_policy_duplicate_conclusions")
        raw_conclusions = value.get(
            "required_conclusion_ids",
            value.get("required_conclusions", ()),
        )
        return cls(
            critical_claim_types=value.get(
                "critical_claim_types",
                ("factual", "quantitative", "causal"),
            ),
            required_conclusion_ids=raw_conclusions,
            require_independent_logical_agent=value.get(
                "require_independent_logical_agent",
                True,
            ),
            require_harness_separation=value.get(
                "require_harness_separation",
                True,
            ),
            forbid_source_reuse=value.get("forbid_source_reuse", True),
            policy_version=value.get("policy_version", VERIFICATION_SCHEMA_VERSION),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "critical_claim_types": list(self.critical_claim_types),
            "required_conclusion_ids": list(self.required_conclusion_ids),
            "require_independent_logical_agent": self.require_independent_logical_agent,
            "require_harness_separation": self.require_harness_separation,
            "forbid_source_reuse": self.forbid_source_reuse,
        }


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
    payload_bytes: bytes | None = field(
        default=None,
        repr=False,
        compare=False,
    )

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
            payload_bytes=retrieved_content,
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
            payload_bytes=None,
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
    required_conclusion_ids: tuple[str, ...] = ()
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
            "required_conclusion_ids",
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
        object.__setattr__(
            self,
            "required_conclusion_ids",
            _string_tuple(
                self.required_conclusion_ids,
                "claim_required_conclusion_ids",
            ),
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
            required_conclusion_ids=value.get("required_conclusion_ids", ()),
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

    @property
    def derived_critical(self) -> bool:
        return derive_criticality(self, None)

    def is_critical(
        self,
        policy: CriticalityPolicy | Mapping[str, Any] | None = None,
    ) -> bool:
        return derive_criticality(self, policy)

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
            "required_conclusion_ids": list(self.required_conclusion_ids),
        }


def derive_criticality(
    claim: TypedClaim,
    policy: CriticalityPolicy | Mapping[str, Any] | None = None,
) -> bool:
    """Derive criticality from the pinned policy and conclusion use.

    Collector-provided ``critical`` metadata is deliberately ignored.  This
    keeps a producer from downgrading a claim simply by changing a flag and
    makes the required-conclusion relationship the durable source of truth.
    """

    if not isinstance(claim, TypedClaim):
        raise _error("criticality_claim_required")
    resolved_policy = (
        policy
        if isinstance(policy, CriticalityPolicy)
        else CriticalityPolicy.from_mapping(policy)
    )
    if claim.claim_type not in resolved_policy.critical_claim_types:
        return False
    if not claim.required_conclusion_ids:
        return False
    if not resolved_policy.required_conclusion_ids:
        return True
    return bool(
        set(claim.required_conclusion_ids)
        & set(resolved_policy.required_conclusion_ids)
    )


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


@dataclass(frozen=True, slots=True)
class VerificationAssignment:
    """Pinned assignment for an independent verification work item."""

    assignment_id: str
    claim_id: str
    verification_work_id: str
    verifier_logical_agent_id: str
    verifier_harness: str
    collector_logical_agent_ids: tuple[str, ...]
    collector_harnesses: tuple[str, ...]
    source_ids: tuple[str, ...] = ()
    excerpt_ids: tuple[str, ...] = ()
    current: bool = True
    schema_version: int = VERIFICATION_SCHEMA_VERSION
    collector_work_ids: tuple[str, ...] = ()
    run_id: str | None = None
    verification_attempt_id: str | None = None
    verification_fencing_token: str | None = None

    EXACT_KEYS = frozenset(
        {
            "schema_version",
            "assignment_id",
            "claim_id",
            "verification_work_id",
            "verifier_logical_agent_id",
            "verifier_agent_id",
            "verifier_harness",
            "collector_logical_agent_ids",
            "collector_agent_ids",
            "collector_harnesses",
            "source_ids",
            "excerpt_ids",
            "current",
            "collector_work_ids",
            "run_id",
            "verification_attempt_id",
            "verification_fencing_token",
        }
    )

    def __post_init__(self) -> None:
        _identifier(self.assignment_id, "verification_assignment_id")
        _identifier(self.claim_id, "verification_assignment_claim_id")
        _identifier(self.verification_work_id, "verification_work_id")
        verifier = _identifier(
            self.verifier_logical_agent_id,
            "verification_verifier_logical_agent_id",
        )
        _non_empty_text(
            self.verifier_harness,
            "verification_verifier_harness",
            max_bytes=128,
        )
        collectors = _string_tuple(
            self.collector_logical_agent_ids,
            "verification_collector_logical_agent_ids",
            required=True,
        )
        collector_harnesses = _string_tuple(
            self.collector_harnesses,
            "verification_collector_harnesses",
            required=True,
        )
        source_ids = _string_tuple(
            self.source_ids,
            "verification_source_ids",
        )
        excerpt_ids = _string_tuple(
            self.excerpt_ids,
            "verification_excerpt_ids",
        )
        collector_work_ids = _string_tuple(
            self.collector_work_ids,
            "verification_collector_work_ids",
        )
        for field, value in (
            ("verification_run_id", self.run_id),
            ("verification_attempt_id", self.verification_attempt_id),
            ("verification_fencing_token", self.verification_fencing_token),
        ):
            if value is not None:
                _identifier(value, field)
        authority = (
            self.run_id,
            self.verification_attempt_id,
            self.verification_fencing_token,
        )
        if any(item is not None for item in authority) and not all(
            item is not None for item in authority
        ):
            raise _error("verification_assignment_authority_incomplete")
        if not isinstance(self.current, bool):
            raise _error("verification_assignment_current_invalid")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != VERIFICATION_SCHEMA_VERSION
        ):
            raise _error("verification_assignment_schema_version_unsupported")
        object.__setattr__(self, "verifier_logical_agent_id", verifier)
        object.__setattr__(self, "collector_logical_agent_ids", collectors)
        object.__setattr__(self, "collector_harnesses", collector_harnesses)
        object.__setattr__(self, "source_ids", source_ids)
        object.__setattr__(self, "excerpt_ids", excerpt_ids)
        object.__setattr__(self, "collector_work_ids", collector_work_ids)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VerificationAssignment:
        if not isinstance(value, Mapping):
            raise _error("verification_assignment_object_required")
        unknown = set(value) - cls.EXACT_KEYS
        required = {
            "schema_version",
            "assignment_id",
            "claim_id",
            "verification_work_id",
            "verifier_harness",
            "collector_harnesses",
        }
        missing = required - set(value)
        if "verifier_logical_agent_id" not in value and "verifier_agent_id" not in value:
            missing.add("verifier_logical_agent_id")
        if (
            "collector_logical_agent_ids" not in value
            and "collector_agent_ids" not in value
        ):
            missing.add("collector_logical_agent_ids")
        if unknown:
            raise _error(
                f"verification_assignment_unknown_field:{sorted(unknown)[0]}"
            )
        if (
            "verifier_logical_agent_id" in value
            and "verifier_agent_id" in value
        ):
            raise _error("verification_assignment_duplicate_verifier_agent")
        if (
            "collector_logical_agent_ids" in value
            and "collector_agent_ids" in value
        ):
            raise _error("verification_assignment_duplicate_collector_agents")
        if missing:
            raise _error(
                f"verification_assignment_missing_field:{sorted(missing)[0]}"
            )
        return cls(
            assignment_id=value["assignment_id"],
            claim_id=value["claim_id"],
            verification_work_id=value["verification_work_id"],
            verifier_logical_agent_id=value.get(
                "verifier_logical_agent_id",
                value.get("verifier_agent_id"),
            ),
            verifier_harness=value["verifier_harness"],
            collector_logical_agent_ids=value.get(
                "collector_logical_agent_ids",
                value.get("collector_agent_ids"),
            ),
            collector_harnesses=value["collector_harnesses"],
            source_ids=value.get("source_ids", ()),
            excerpt_ids=value.get("excerpt_ids", ()),
            current=value.get("current", True),
            schema_version=value["schema_version"],
            collector_work_ids=value.get("collector_work_ids", ()),
            run_id=value.get("run_id"),
            verification_attempt_id=value.get("verification_attempt_id"),
            verification_fencing_token=value.get("verification_fencing_token"),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "assignment_id": self.assignment_id,
            "claim_id": self.claim_id,
            "verification_work_id": self.verification_work_id,
            "verifier_logical_agent_id": self.verifier_logical_agent_id,
            "verifier_harness": self.verifier_harness,
            "collector_logical_agent_ids": list(self.collector_logical_agent_ids),
            "collector_harnesses": list(self.collector_harnesses),
            "source_ids": list(self.source_ids),
            "excerpt_ids": list(self.excerpt_ids),
            "current": self.current,
        }
        if self.collector_work_ids:
            result["collector_work_ids"] = list(self.collector_work_ids)
        if self.run_id is not None:
            result["run_id"] = self.run_id
        if self.verification_attempt_id is not None:
            result["verification_attempt_id"] = self.verification_attempt_id
        if self.verification_fencing_token is not None:
            result["verification_fencing_token"] = self.verification_fencing_token
        return result


@dataclass(frozen=True, slots=True)
class VerificationDisposition:
    """Evidence-bound, append-only verification disposition."""

    disposition_id: str
    claim_id: str
    assignment_id: str
    disposition: str
    reason: str
    evidence_ids: tuple[str, ...]
    contradiction_ids: tuple[str, ...]
    verifier_logical_agent_id: str
    verifier_harness: str
    source_ids: tuple[str, ...]
    excerpt_ids: tuple[str, ...]
    current: bool = True
    schema_version: int = VERIFICATION_SCHEMA_VERSION

    EXACT_KEYS = frozenset(
        {
            "schema_version",
            "disposition_id",
            "claim_id",
            "assignment_id",
            "disposition",
            "reason",
            "evidence_ids",
            "contradiction_ids",
            "verifier_logical_agent_id",
            "verifier_agent_id",
            "verifier_harness",
            "source_ids",
            "excerpt_ids",
            "current",
        }
    )

    def __post_init__(self) -> None:
        _identifier(self.disposition_id, "verification_disposition_id")
        _identifier(self.claim_id, "verification_disposition_claim_id")
        _identifier(self.assignment_id, "verification_disposition_assignment_id")
        if self.disposition not in SUPPORTED_VERIFICATION_DISPOSITIONS:
            raise _error("verification_disposition_unsupported")
        _non_empty_text(
            self.reason,
            "verification_disposition_reason",
            max_bytes=MAX_CLAIM_BYTES,
        )
        evidence_ids = _string_tuple(
            self.evidence_ids,
            "verification_disposition_evidence_ids",
        )
        contradiction_ids = _string_tuple(
            self.contradiction_ids,
            "verification_disposition_contradiction_ids",
        )
        verifier = _identifier(
            self.verifier_logical_agent_id,
            "verification_disposition_verifier_logical_agent_id",
        )
        _non_empty_text(
            self.verifier_harness,
            "verification_disposition_verifier_harness",
            max_bytes=128,
        )
        source_ids = _string_tuple(
            self.source_ids,
            "verification_disposition_source_ids",
        )
        excerpt_ids = _string_tuple(
            self.excerpt_ids,
            "verification_disposition_excerpt_ids",
        )
        if not isinstance(self.current, bool):
            raise _error("verification_disposition_current_invalid")
        if (
            not isinstance(self.schema_version, int)
            or isinstance(self.schema_version, bool)
            or self.schema_version != VERIFICATION_SCHEMA_VERSION
        ):
            raise _error("verification_disposition_schema_version_unsupported")
        object.__setattr__(self, "evidence_ids", evidence_ids)
        object.__setattr__(self, "contradiction_ids", contradiction_ids)
        object.__setattr__(self, "verifier_logical_agent_id", verifier)
        object.__setattr__(self, "source_ids", source_ids)
        object.__setattr__(self, "excerpt_ids", excerpt_ids)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> VerificationDisposition:
        if not isinstance(value, Mapping):
            raise _error("verification_disposition_object_required")
        unknown = set(value) - cls.EXACT_KEYS
        required = cls.EXACT_KEYS - {
            "verifier_agent_id",
            "current",
        }
        missing = required - set(value)
        if "verifier_logical_agent_id" not in value and "verifier_agent_id" not in value:
            missing.add("verifier_logical_agent_id")
        if unknown:
            raise _error(
                f"verification_disposition_unknown_field:{sorted(unknown)[0]}"
            )
        if (
            "verifier_logical_agent_id" in value
            and "verifier_agent_id" in value
        ):
            raise _error("verification_disposition_duplicate_verifier_agent")
        if missing:
            raise _error(
                f"verification_disposition_missing_field:{sorted(missing)[0]}"
            )
        return cls(
            disposition_id=value["disposition_id"],
            claim_id=value["claim_id"],
            assignment_id=value["assignment_id"],
            disposition=value["disposition"],
            reason=value["reason"],
            evidence_ids=value["evidence_ids"],
            contradiction_ids=value["contradiction_ids"],
            verifier_logical_agent_id=value.get(
                "verifier_logical_agent_id",
                value.get("verifier_agent_id"),
            ),
            verifier_harness=value["verifier_harness"],
            source_ids=value["source_ids"],
            excerpt_ids=value["excerpt_ids"],
            current=value.get("current", True),
            schema_version=value["schema_version"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "disposition_id": self.disposition_id,
            "claim_id": self.claim_id,
            "assignment_id": self.assignment_id,
            "disposition": self.disposition,
            "reason": self.reason,
            "evidence_ids": list(self.evidence_ids),
            "contradiction_ids": list(self.contradiction_ids),
            "verifier_logical_agent_id": self.verifier_logical_agent_id,
            "verifier_harness": self.verifier_harness,
            "source_ids": list(self.source_ids),
            "excerpt_ids": list(self.excerpt_ids),
            "current": self.current,
        }


class ResearchEvidenceRegister:
    """Append-only admission register for immutable research evidence."""

    def __init__(
        self,
        *,
        verification_policy: CriticalityPolicy | Mapping[str, Any] | None = None,
        verification_authorizer: Callable[
            [VerificationAssignment], None
        ] | None = None,
    ) -> None:
        self._sources: dict[str, SourceReceipt] = {}
        self._excerpts: dict[str, ExcerptReceipt] = {}
        self._claims: dict[str, TypedClaim] = {}
        self._relations: dict[str, EvidenceRelation] = {}
        self._verification_policy = (
            verification_policy
            if isinstance(verification_policy, CriticalityPolicy)
            else CriticalityPolicy.from_mapping(verification_policy)
        )
        self._verification_authorizer = verification_authorizer
        self._verification_assignments: dict[str, VerificationAssignment] = {}
        self._verification_dispositions: dict[str, VerificationDisposition] = {}
        self._verification_disposition_order: list[str] = []

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
    def verification_policy(self) -> CriticalityPolicy:
        return self._verification_policy

    @property
    def verification_assignments(self) -> tuple[VerificationAssignment, ...]:
        return tuple(
            self._verification_assignments[key]
            for key in sorted(self._verification_assignments)
        )

    @property
    def verification_dispositions(self) -> tuple[VerificationDisposition, ...]:
        return tuple(
            self._verification_dispositions[key]
            for key in self._verification_disposition_order
        )

    @property
    def contested_claim_ids(self) -> tuple[str, ...]:
        return tuple(
            claim.claim_id
            for claim in self.claims
            if self.claim_status(claim.claim_id)["state"] == "contested"
        )

    @property
    def creditable_relation_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._relations))

    @property
    def verification_credit_claim_ids(self) -> tuple[str, ...]:
        return tuple(
            claim.claim_id
            for claim in self.claims
            if self.claim_status(claim.claim_id)["gate_credit"]
        )

    @property
    def gate_credit_relation_ids(self) -> tuple[str, ...]:
        """Relations eligible for settled factual gate credit.

        Admission and gate credit are intentionally separate.  Contradictory
        observations remain in ``creditable_relation_ids`` for auditability,
        but only support relations on a claim whose current disposition is
        settled/verified are eligible for a factual conclusion.
        """

        result: list[str] = []
        for claim in self.claims:
            status = self.claim_status(claim.claim_id)
            if not status["gate_credit"]:
                continue
            result.extend(status["support_relation_ids"])
        return tuple(sorted(result))

    @property
    def settled_claim_ids(self) -> tuple[str, ...]:
        return self.verification_credit_claim_ids

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
        if source.payload_bytes is not None:
            try:
                normalized_content = unicodedata.normalize(
                    "NFC",
                    source.payload_bytes.decode(source.encoding),
                )
            except (LookupError, UnicodeDecodeError) as exc:
                raise _error("excerpt_source_decode_failed") from exc
            if excerpt.text not in normalized_content:
                raise _error("excerpt_text_not_in_source")
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
        if relation.relation == "contradiction":
            # A newly admitted opposing observation invalidates a previously
            # current verification disposition.  The old disposition remains
            # in history, but cannot continue to grant gate credit until a
            # later disposition accounts for this contradiction.
            for disposition in tuple(self._verification_dispositions.values()):
                if (
                    disposition.claim_id == relation.claim_id
                    and disposition.current
                ):
                    self._verification_dispositions[
                        disposition.disposition_id
                    ] = replace(disposition, current=False)
        self._relations[relation.relation_id] = relation
        return relation

    def admit_verification_assignment(
        self,
        value: VerificationAssignment | Mapping[str, Any],
    ) -> VerificationAssignment:
        assignment = (
            value
            if isinstance(value, VerificationAssignment)
            else VerificationAssignment.from_mapping(value)
        )
        if assignment.claim_id not in self._claims:
            raise _error(
                f"verification_assignment_claim_not_admitted:{assignment.claim_id}"
            )
        claim = self._claims[assignment.claim_id]
        if (
            self._verification_policy.require_independent_logical_agent
            and assignment.verifier_logical_agent_id
            in assignment.collector_logical_agent_ids
        ):
            raise _error("verification_assignment_self_verification")
        if (
            self._verification_policy.require_harness_separation
            and assignment.verifier_harness in assignment.collector_harnesses
        ):
            raise _error("verification_assignment_harness_not_independent")
        if (
            derive_criticality(claim, self._verification_policy)
            and (
                assignment.run_id is None
                or assignment.verification_attempt_id is None
                or assignment.verification_fencing_token is None
            )
        ):
            raise _error("verification_assignment_kernel_authority_required")
        if any(source_id not in self._sources for source_id in assignment.source_ids):
            raise _error("verification_assignment_source_not_admitted")
        if any(
            excerpt_id not in self._excerpts
            for excerpt_id in assignment.excerpt_ids
        ):
            raise _error("verification_assignment_excerpt_not_admitted")
        for source_id in assignment.source_ids:
            source = self._sources[source_id]
            if (
                source.agent != assignment.verifier_logical_agent_id
                or source.harness != assignment.verifier_harness
            ):
                raise _error("verification_assignment_source_attribution_mismatch")
            if source.role not in {"verifier", "verification", "independent-verifier"}:
                raise _error("verification_assignment_source_role_mismatch")
            if assignment.run_id is not None:
                if source.run_id != assignment.run_id:
                    raise _error("verification_assignment_run_mismatch")
                if source.work_id != assignment.verification_work_id:
                    raise _error("verification_assignment_work_mismatch")
                if (
                    source.attempt_id != assignment.verification_attempt_id
                    or source.fencing_token != assignment.verification_fencing_token
                ):
                    raise _error("verification_assignment_authority_mismatch")
        for excerpt_id in assignment.excerpt_ids:
            excerpt = self._excerpts[excerpt_id]
            if excerpt.source_id not in assignment.source_ids:
                raise _error("verification_assignment_excerpt_source_mismatch")
        existing = self._verification_assignments.get(assignment.assignment_id)
        if existing is not None:
            if existing != assignment:
                raise _error("verification_assignment_immutable_conflict")
            return existing
        current_for_claim = [
            item
            for item in self._verification_assignments.values()
            if item.claim_id == assignment.claim_id and item.current
        ]
        if current_for_claim:
            raise _error("verification_assignment_current_conflict")
        self._verification_assignments[assignment.assignment_id] = assignment
        return assignment

    admit_assignment = admit_verification_assignment

    def supersede_verification_assignment(
        self,
        assignment_id: str,
    ) -> VerificationAssignment:
        """Fence a coordinator-owned assignment so a retry may replace it."""

        assignment = self._verification_assignments.get(assignment_id)
        if assignment is None:
            raise _error(f"verification_assignment_not_admitted:{assignment_id}")
        if not assignment.current:
            return assignment
        superseded = replace(assignment, current=False)
        self._verification_assignments[assignment_id] = superseded
        for disposition in tuple(self._verification_dispositions.values()):
            if (
                disposition.assignment_id == assignment_id
                and disposition.current
            ):
                self._verification_dispositions[
                    disposition.disposition_id
                ] = replace(disposition, current=False)
        return superseded

    supersede_assignment = supersede_verification_assignment

    def admit_verification_disposition(
        self,
        value: VerificationDisposition | Mapping[str, Any],
    ) -> VerificationDisposition:
        disposition = (
            value
            if isinstance(value, VerificationDisposition)
            else VerificationDisposition.from_mapping(value)
        )
        claim = self._claims.get(disposition.claim_id)
        if claim is None:
            raise _error(
                f"verification_disposition_claim_not_admitted:{disposition.claim_id}"
            )
        assignment = self._verification_assignments.get(disposition.assignment_id)
        if assignment is None:
            raise _error(
                "verification_disposition_assignment_not_admitted:"
                f"{disposition.assignment_id}"
            )
        if assignment.claim_id != disposition.claim_id:
            raise _error("verification_disposition_assignment_claim_mismatch")
        if not assignment.current:
            raise _error("verification_disposition_assignment_not_current")
        if (
            disposition.verifier_logical_agent_id
            != assignment.verifier_logical_agent_id
        ):
            raise _error("verification_disposition_verifier_mismatch")
        if disposition.verifier_harness != assignment.verifier_harness:
            raise _error("verification_disposition_harness_mismatch")
        if self._verification_policy.require_independent_logical_agent:
            if (
                disposition.verifier_logical_agent_id
                in assignment.collector_logical_agent_ids
            ):
                raise _error("verification_disposition_not_independent")
        if (
            self._verification_policy.require_harness_separation
            and disposition.verifier_harness in assignment.collector_harnesses
        ):
            raise _error("verification_disposition_harness_not_independent")

        evidence = self._validate_disposition_evidence(
            disposition,
            claim=claim,
            assignment=assignment,
        )
        contradiction_ids = {
            relation.relation_id
            for relation in self._relations.values()
            if relation.claim_id == claim.claim_id
            and relation.relation == "contradiction"
        }
        supplied_contradictions = set(disposition.contradiction_ids)
        if not supplied_contradictions.issubset(contradiction_ids):
            raise _error("verification_disposition_contradiction_not_admitted")
        if disposition.disposition == "verified":
            if not evidence:
                raise _error("verification_disposition_evidence_required")
            if supplied_contradictions != contradiction_ids:
                raise _error("verification_disposition_contradictions_unaccounted")
            if (
                derive_criticality(claim, self._verification_policy)
                and (
                    assignment.run_id is None
                    or assignment.verification_attempt_id is None
                    or assignment.verification_fencing_token is None
                )
            ):
                raise _error("verification_disposition_kernel_authority_required")
            if (
                derive_criticality(claim, self._verification_policy)
                and self._verification_authorizer is None
            ):
                raise _error("verification_disposition_kernel_authority_unbound")
            if (
                derive_criticality(claim, self._verification_policy)
                and self._verification_authorizer is not None
            ):
                self._verification_authorizer(assignment)
        elif disposition.disposition == "contested":
            if supplied_contradictions != contradiction_ids:
                raise _error("verification_disposition_contradictions_unaccounted")

        existing = self._verification_dispositions.get(disposition.disposition_id)
        if existing is not None:
            if existing != disposition:
                raise _error("verification_disposition_immutable_conflict")
            return existing
        current_for_claim = [
            item
            for item in self._verification_dispositions.values()
            if item.claim_id == disposition.claim_id and item.current
        ]
        if current_for_claim:
            # History is retained, but only one disposition is current.  The
            # replacement is deterministic and does not rewrite its payload.
            for previous in current_for_claim:
                self._verification_dispositions[previous.disposition_id] = replace(
                    previous,
                    current=False,
                )
        self._verification_dispositions[disposition.disposition_id] = disposition
        self._verification_disposition_order.append(disposition.disposition_id)
        return disposition

    admit_disposition = admit_verification_disposition

    def claim_status(self, claim_id: str) -> dict[str, Any]:
        claim = self._claims.get(claim_id)
        if claim is None:
            raise _error(f"claim_not_admitted:{claim_id}")
        critical = derive_criticality(claim, self._verification_policy)
        relations = [
            relation
            for relation in self._relations.values()
            if relation.claim_id == claim_id
        ]
        support_ids = sorted(
            relation.relation_id
            for relation in relations
            if relation.relation == "support"
        )
        contradiction_ids = sorted(
            relation.relation_id
            for relation in relations
            if relation.relation == "contradiction"
        )
        context_ids = sorted(
            relation.relation_id
            for relation in relations
            if relation.relation == "context"
        )
        history = [
            disposition.to_dict()
            for disposition in self.verification_dispositions
            if disposition.claim_id == claim_id
        ]
        current = next(
            (
                disposition
                for disposition in self._verification_dispositions.values()
                if (
                    disposition.claim_id == claim_id
                    and disposition.current
                    and self._verification_assignments.get(
                        disposition.assignment_id
                    ) is not None
                    and self._verification_assignments[
                        disposition.assignment_id
                    ].current
                )
            ),
            None,
        )
        authority_current = True
        if current is not None and current.disposition == "verified" and critical:
            authority_current = self._verification_assignment_is_authorized(
                current.assignment_id,
            )
        if (
            current is not None
            and current.disposition == "verified"
            and authority_current
        ):
            state = "verified"
            gate_credit = True
        elif current is not None and current.disposition == "verified":
            state = "contested" if contradiction_ids else "unverified"
            gate_credit = False
        elif current is not None and current.disposition in {
            "unverified",
            "rejected",
        }:
            state = "unverified"
            gate_credit = False
        elif contradiction_ids:
            state = "contested"
            gate_credit = False
        elif critical:
            state = "unverified"
            gate_credit = False
        else:
            state = "settled"
            gate_credit = bool(support_ids)
        return {
            "claim_id": claim_id,
            "claim_type": claim.claim_type,
            "critical": critical,
            "state": state,
            "gate_credit": gate_credit,
            "support_relation_ids": support_ids,
            "contradiction_relation_ids": contradiction_ids,
            "context_relation_ids": context_ids,
            "disposition_id": None if current is None else current.disposition_id,
            "authority_current": authority_current,
            "disposition_history": history,
        }

    def verification_status(self) -> tuple[dict[str, Any], ...]:
        return tuple(self.claim_status(claim.claim_id) for claim in self.claims)

    def _verification_assignment_is_authorized(
        self,
        assignment_id: str,
    ) -> bool:
        assignment = self._verification_assignments.get(assignment_id)
        if assignment is None or not assignment.current:
            return False
        if self._verification_authorizer is None:
            return False
        try:
            self._verification_authorizer(assignment)
        except Exception:
            return False
        return True

    def _validate_disposition_evidence(
        self,
        disposition: VerificationDisposition,
        *,
        claim: TypedClaim,
        assignment: VerificationAssignment,
    ) -> tuple[EvidenceRelation, ...]:
        declared_source_ids = set(disposition.source_ids)
        declared_excerpt_ids = set(disposition.excerpt_ids)
        if any(source_id not in self._sources for source_id in declared_source_ids):
            raise _error("verification_disposition_source_not_admitted")
        if any(
            excerpt_id not in self._excerpts
            for excerpt_id in declared_excerpt_ids
        ):
            raise _error("verification_disposition_excerpt_not_admitted")
        evidence: list[EvidenceRelation] = []
        for relation_id in disposition.evidence_ids:
            relation = self._relations.get(relation_id)
            if relation is None:
                raise _error(
                    f"verification_disposition_evidence_not_admitted:{relation_id}"
                )
            if relation.claim_id != claim.claim_id:
                raise _error("verification_disposition_evidence_claim_mismatch")
            if relation.relation != "support":
                raise _error("verification_disposition_evidence_must_support")
            source = self._sources.get(relation.source_id)
            excerpt = self._excerpts.get(relation.excerpt_id)
            if source is None or excerpt is None:
                raise _error("verification_disposition_evidence_lineage_missing")
            if self._verification_policy.forbid_source_reuse:
                reused_source_ids = {
                    prior.source_id
                    for prior in self._relations.values()
                    if prior.claim_id == claim.claim_id
                    and (
                        self._sources.get(prior.source_id) is None
                        or self._sources[prior.source_id].agent
                        != disposition.verifier_logical_agent_id
                        or self._sources[prior.source_id].harness
                        != disposition.verifier_harness
                    )
                }
                if relation.source_id in reused_source_ids:
                    raise _error("verification_disposition_source_reuse")
            if (
                source.agent != disposition.verifier_logical_agent_id
                or source.harness != disposition.verifier_harness
            ):
                raise _error("verification_disposition_source_attribution_mismatch")
            if source.role not in {"verifier", "verification", "independent-verifier"}:
                raise _error("verification_disposition_source_role_mismatch")
            if source.work_id != assignment.verification_work_id:
                raise _error("verification_disposition_work_mismatch")
            if assignment.run_id is not None:
                if source.run_id != assignment.run_id:
                    raise _error("verification_disposition_run_mismatch")
                if (
                    source.attempt_id != assignment.verification_attempt_id
                    or source.fencing_token != assignment.verification_fencing_token
                ):
                    raise _error("verification_disposition_authority_mismatch")
            if source.source_id not in disposition.source_ids:
                raise _error("verification_disposition_source_not_declared")
            if excerpt.excerpt_id not in disposition.excerpt_ids:
                raise _error("verification_disposition_excerpt_not_declared")
            if assignment.source_ids and source.source_id not in assignment.source_ids:
                raise _error("verification_disposition_source_not_assigned")
            if assignment.excerpt_ids and excerpt.excerpt_id not in assignment.excerpt_ids:
                raise _error("verification_disposition_excerpt_not_assigned")
            evidence.append(relation)
        actual_source_ids = {relation.source_id for relation in evidence}
        actual_excerpt_ids = {relation.excerpt_id for relation in evidence}
        if actual_source_ids != declared_source_ids:
            raise _error("verification_disposition_source_ids_mismatch")
        if actual_excerpt_ids != declared_excerpt_ids:
            raise _error("verification_disposition_excerpt_ids_mismatch")
        return tuple(evidence)

    def admit_bundle(self, value: Mapping[str, Any]) -> ResearchEvidenceRegister:
        """Atomically admit a complete source/excerpt/claim/relation bundle."""

        candidate = type(self).from_mapping(
            value,
            verification_policy=self._verification_policy,
            verification_authorizer=self._verification_authorizer,
        )
        snapshot = (
            dict(self._sources),
            dict(self._excerpts),
            dict(self._claims),
            dict(self._relations),
            dict(self._verification_assignments),
            dict(self._verification_dispositions),
            list(self._verification_disposition_order),
            self._verification_policy,
        )
        try:
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
            for item in candidate._relations.values():
                self.admit_relation(item)
            for key, item in candidate._verification_assignments.items():
                if key in self._verification_assignments:
                    if self._verification_assignments[key] != item:
                        raise _error("verification_assignment_immutable_conflict")
                else:
                    self.admit_verification_assignment(item)
            for key, item in candidate._verification_dispositions.items():
                if key in self._verification_dispositions:
                    if self._verification_dispositions[key] != item:
                        raise _error("verification_disposition_immutable_conflict")
                else:
                    self.admit_verification_disposition(item)
        except BaseException:
            (
                self._sources,
                self._excerpts,
                self._claims,
                self._relations,
                self._verification_assignments,
                self._verification_dispositions,
                self._verification_disposition_order,
                self._verification_policy,
            ) = snapshot
            raise
        return self

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": RESEARCH_EVIDENCE_SCHEMA_VERSION,
            "sources": [source.to_dict() for source in self.sources],
            "excerpts": [excerpt.to_dict() for excerpt in self.excerpts],
            "claims": [claim.to_dict() for claim in self.claims],
            "relations": [relation.to_dict() for relation in self.relations],
            "creditable_relation_ids": list(self.creditable_relation_ids),
            "verification_policy": self.verification_policy.to_dict(),
            "verification_assignments": [
                assignment.to_dict()
                for assignment in self.verification_assignments
            ],
            "verification_dispositions": [
                disposition.to_dict()
                for disposition in self.verification_dispositions
            ],
            "claim_statuses": list(self.verification_status()),
            "contested_claim_ids": list(self.contested_claim_ids),
            "verification_credit_claim_ids": list(self.verification_credit_claim_ids),
            "settled_claim_ids": list(self.settled_claim_ids),
            "gate_credit_relation_ids": list(self.gate_credit_relation_ids),
        }

    @classmethod
    def from_mapping(
        cls,
        value: Mapping[str, Any],
        *,
        verification_policy: CriticalityPolicy
        | Mapping[str, Any]
        | object = _POLICY_UNSET,
        verification_authorizer: Callable[
            [VerificationAssignment], None
        ] | None = None,
    ) -> ResearchEvidenceRegister:
        if not isinstance(value, Mapping):
            raise _error("research_evidence_register_object_required")
        expected = {
            "schema_version",
            "sources",
            "excerpts",
            "claims",
            "relations",
            "creditable_relation_ids",
            "verification_policy",
            "verification_assignments",
            "verification_dispositions",
            "claim_statuses",
            "contested_claim_ids",
            "verification_credit_claim_ids",
            "settled_claim_ids",
            "gate_credit_relation_ids",
        }
        # Verification fields were added after the source/excerpt/claim
        # register contract.  They are optional when reading a schema-v1
        # register so prior admitted evidence remains durable and readable.
        required = {
            "schema_version",
            "sources",
            "excerpts",
            "claims",
            "relations",
        }
        unknown = set(value) - expected
        missing = required - set(value)
        if unknown:
            raise _error(f"research_evidence_register_unknown_field:{sorted(unknown)[0]}")
        if missing:
            raise _error(f"research_evidence_register_missing_field:{sorted(missing)[0]}")
        if value["schema_version"] != RESEARCH_EVIDENCE_SCHEMA_VERSION:
            raise _error("research_evidence_schema_version_unsupported")
        register = cls(
            verification_policy=(
                verification_policy
                if isinstance(verification_policy, CriticalityPolicy)
                else None
            ),
            verification_authorizer=verification_authorizer,
        )
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
        expected_policy: CriticalityPolicy | None = None
        if verification_policy is not _POLICY_UNSET:
            expected_policy = (
                verification_policy
                if isinstance(verification_policy, CriticalityPolicy)
                else CriticalityPolicy.from_mapping(verification_policy)
            )
        raw_policy = value.get("verification_policy", _POLICY_UNSET)
        if raw_policy is _POLICY_UNSET:
            payload_policy = expected_policy or CriticalityPolicy()
        else:
            payload_policy = CriticalityPolicy.from_mapping(raw_policy)
        if expected_policy is not None:
            if raw_policy is not _POLICY_UNSET and payload_policy != expected_policy:
                raise _error("verification_policy_pinned_mismatch")
            register._verification_policy = expected_policy
        else:
            register._verification_policy = payload_policy
        assignments = value.get("verification_assignments", [])
        dispositions = value.get("verification_dispositions", [])
        if (
            isinstance(assignments, (str, bytes))
            or not isinstance(assignments, list)
            or isinstance(dispositions, (str, bytes))
            or not isinstance(dispositions, list)
        ):
            raise _error("verification_history_must_be_array")
        for assignment in assignments:
            register.admit_verification_assignment(assignment)
        for disposition in dispositions:
            register.admit_verification_disposition(disposition)
        declared_credit = value.get("creditable_relation_ids")
        if declared_credit is not None:
            if (
                isinstance(declared_credit, (str, bytes))
                or not isinstance(declared_credit, list)
                or tuple(declared_credit) != register.creditable_relation_ids
            ):
                raise _error("creditable_relation_ids_mismatch")
        declared_contested = value.get("contested_claim_ids")
        if declared_contested is not None and tuple(declared_contested) != register.contested_claim_ids:
            raise _error("contested_claim_ids_mismatch")
        declared_credit_claims = value.get("verification_credit_claim_ids")
        if (
            declared_credit_claims is not None
            and tuple(declared_credit_claims) != register.verification_credit_claim_ids
        ):
            raise _error("verification_credit_claim_ids_mismatch")
        declared_settled = value.get("settled_claim_ids")
        if declared_settled is not None and tuple(declared_settled) != register.settled_claim_ids:
            raise _error("settled_claim_ids_mismatch")
        declared_gate_relations = value.get("gate_credit_relation_ids")
        if (
            declared_gate_relations is not None
            and tuple(declared_gate_relations) != register.gate_credit_relation_ids
        ):
            raise _error("gate_credit_relation_ids_mismatch")
        declared_statuses = value.get("claim_statuses")
        if declared_statuses is not None and tuple(declared_statuses) != register.verification_status():
            raise _error("claim_statuses_mismatch")
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


def parse_verification_assignment(
    value: Mapping[str, Any],
) -> VerificationAssignment:
    return VerificationAssignment.from_mapping(value)


def parse_verification_disposition(
    value: Mapping[str, Any],
) -> VerificationDisposition:
    return VerificationDisposition.from_mapping(value)


def parse_evidence_register(value: Mapping[str, Any]) -> ResearchEvidenceRegister:
    return ResearchEvidenceRegister.from_mapping(value)


validate_source_receipt = parse_source_receipt
validate_excerpt_receipt = parse_excerpt_receipt
validate_claim = parse_typed_claim
validate_evidence_relation = parse_evidence_relation
validate_verification_assignment = parse_verification_assignment
validate_verification_disposition = parse_verification_disposition
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
    "CriticalityPolicy",
    "EVIDENCE_NORMALIZATION_VERSION",
    "EvidenceRelation",
    "EvidenceRelationRecord",
    "ExcerptReceipt",
    "MAX_CLAIM_BYTES",
    "MAX_EXCERPT_BYTES",
    "MAX_SOURCE_URL_BYTES",
    "derive_criticality",
    "parse_evidence_relation",
    "parse_evidence_register",
    "parse_excerpt_receipt",
    "parse_source_receipt",
    "parse_typed_claim",
    "parse_verification_assignment",
    "parse_verification_disposition",
    "RESEARCH_EVIDENCE_SCHEMA_VERSION",
    "ResearchEvidenceError",
    "ResearchEvidenceRegister",
    "SUPPORTED_CLAIM_TYPES",
    "SUPPORTED_EVIDENCE_RELATIONS",
    "SUPPORTED_SOURCE_OUTCOMES",
    "SUPPORTED_VERIFICATION_DISPOSITIONS",
    "SourceExcerpt",
    "SourceExcerptReceipt",
    "SourceReceipt",
    "TypedClaim",
    "VERIFICATION_SCHEMA_VERSION",
    "VerificationAssignment",
    "VerificationDisposition",
    "validate_claim",
    "validate_evidence_relation",
    "validate_evidence_register",
    "validate_excerpt_receipt",
    "validate_source_receipt",
    "validate_verification_assignment",
    "validate_verification_disposition",
]
