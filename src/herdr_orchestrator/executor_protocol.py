from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any


MANIFEST_VERSION = 1
PINNED_MANIFEST_VERSION = MANIFEST_VERSION
CANONICALIZATION_VERSION = 1
DEFINITION_CANONICALIZATION_VERSION = CANONICALIZATION_VERSION
_DIGEST_PREFIX = "sha256:"
_DIGEST_LENGTH = 64
EMPTY_DIGEST = f"{_DIGEST_PREFIX}{hashlib.sha256(b'null').hexdigest()}"

MANIFEST_DIGEST_FIELDS = (
    "workflow_digest",
    "config_digest",
    "input_digest",
    "source_digest",
    "route_digest",
    "profile_digest",
    "prompt_digest",
    "static_check_digest",
    "contract_digest",
    "executor_digest",
    "artifact_contract_digest",
)

__all__ = [
    "CANONICALIZATION_VERSION",
    "DEFINITION_CANONICALIZATION_VERSION",
    "DefinitionError",
    "DefinitionIdentity",
    "EMPTY_DIGEST",
    "MANIFEST_DIGEST_FIELDS",
    "MANIFEST_VERSION",
    "ManifestValidationError",
    "PINNED_MANIFEST_VERSION",
    "PinnedManifest",
    "PinnedRunManifest",
    "RunManifest",
    "UNSET",
    "canonical_definition",
    "canonical_json",
    "canonicalize_definition",
    "definition_digest",
    "digest_definition",
    "hash_definition",
    "normalize_definition",
    "normalized_definition_identity",
    "run_identity_digest",
]


class ManifestValidationError(ValueError):
    """Raised when a pinned manifest is not a supported exact contract."""

    code = "manifest_invalid"


class DefinitionError(ValueError):
    """Raised when a definition cannot be canonicalized as data."""

    code = "definition_invalid"


class _Unset:
    pass


UNSET = _Unset()


def normalize_definition(value: Any) -> Any:
    """Normalize supported definition data into deterministic JSON values.

    Mapping keys are strings and are sorted by ``canonical_json``.  List order
    is meaningful.  Unicode strings use NFC so equivalent source text has one
    identity.  Definitions never execute callbacks, commands, or arbitrary
    object serialization.
    """

    if isinstance(value, Enum):
        return normalize_definition(value.value)
    if isinstance(value, Path):
        return normalize_definition(str(value))
    if is_dataclass(value) and not isinstance(value, type):
        return normalize_definition(asdict(value))
    if value is None or isinstance(value, (str, bool, int)):
        return unicodedata.normalize("NFC", value) if isinstance(value, str) else value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DefinitionError("definition_non_finite_number")
        return value
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise DefinitionError("definition_mapping_key_must_be_string")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise DefinitionError("definition_duplicate_normalized_key")
            normalized[normalized_key] = normalize_definition(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [normalize_definition(item) for item in value]
    raise DefinitionError(f"definition_unsupported_type: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize a definition using canonicalization version 1."""

    return json.dumps(
        normalize_definition(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def definition_digest(value: Any) -> str:
    """Return a self-describing SHA-256 digest of canonical definition data."""

    return _DIGEST_PREFIX + hashlib.sha256(
        canonical_json(value).encode("utf-8")
    ).hexdigest()


# Clear aliases make the primitive usable by callers that use either term.
canonicalize_definition = canonical_json
canonical_definition = canonical_json
digest_definition = definition_digest
hash_definition = definition_digest


@dataclass(frozen=True, slots=True)
class DefinitionIdentity:
    canonicalization_version: int
    normalized: str
    digest: str

    @classmethod
    def from_value(cls, value: Any) -> DefinitionIdentity:
        normalized = canonical_json(value)
        return cls(
            canonicalization_version=CANONICALIZATION_VERSION,
            normalized=normalized,
            digest=_DIGEST_PREFIX
            + hashlib.sha256(normalized.encode("utf-8")).hexdigest(),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "canonicalization_version": self.canonicalization_version,
            "normalized": self.normalized,
            "digest": self.digest,
        }


def normalized_definition_identity(value: Any) -> DefinitionIdentity:
    return DefinitionIdentity.from_value(value)


@dataclass(frozen=True, slots=True)
class PinnedRunManifest:
    """Immutable identities captured at schema-v2 run creation."""

    workflow_digest: str = EMPTY_DIGEST
    config_digest: str = EMPTY_DIGEST
    input_digest: str = EMPTY_DIGEST
    source_digest: str = EMPTY_DIGEST
    route_digest: str = EMPTY_DIGEST
    profile_digest: str = EMPTY_DIGEST
    prompt_digest: str = EMPTY_DIGEST
    static_check_digest: str = EMPTY_DIGEST
    contract_digest: str = EMPTY_DIGEST
    executor_digest: str = EMPTY_DIGEST
    artifact_contract_digest: str = EMPTY_DIGEST
    manifest_version: int = MANIFEST_VERSION
    canonicalization_version: int = CANONICALIZATION_VERSION

    def __post_init__(self) -> None:
        for field in MANIFEST_DIGEST_FIELDS:
            object.__setattr__(
                self,
                field,
                _validate_digest(getattr(self, field), field),
            )
        self.validate()

    @classmethod
    def from_digests(
        cls,
        *,
        workflow_digest: str = EMPTY_DIGEST,
        config_digest: str = EMPTY_DIGEST,
        input_digest: str = EMPTY_DIGEST,
        source_digest: str = EMPTY_DIGEST,
        route_digest: str = EMPTY_DIGEST,
        profile_digest: str = EMPTY_DIGEST,
        prompt_digest: str = EMPTY_DIGEST,
        static_check_digest: str = EMPTY_DIGEST,
        contract_digest: str = EMPTY_DIGEST,
        executor_digest: str = EMPTY_DIGEST,
        artifact_contract_digest: str = EMPTY_DIGEST,
        manifest_version: int = MANIFEST_VERSION,
        canonicalization_version: int = CANONICALIZATION_VERSION,
        prompt_rubric_digest: str | None = None,
        source_revision_digest: str | None = None,
        executor_contract_digest: str | None = None,
        executor_implementation_digest: str | None = None,
        artifact_digest: str | None = None,
    ) -> PinnedRunManifest:
        prompt_digest = _coalesce_digest_alias(
            prompt_digest,
            prompt_rubric_digest,
            "prompt_digest",
        )
        source_digest = _coalesce_digest_alias(
            source_digest,
            source_revision_digest,
            "source_digest",
        )
        contract_digest = _coalesce_digest_alias(
            contract_digest,
            executor_contract_digest,
            "contract_digest",
        )
        executor_digest = _coalesce_digest_alias(
            executor_digest,
            executor_implementation_digest,
            "executor_digest",
        )
        artifact_contract_digest = _coalesce_digest_alias(
            artifact_contract_digest,
            artifact_digest,
            "artifact_contract_digest",
        )
        manifest = cls(
            workflow_digest=_validate_digest(workflow_digest, "workflow_digest"),
            config_digest=_validate_digest(config_digest, "config_digest"),
            input_digest=_validate_digest(input_digest, "input_digest"),
            source_digest=_validate_digest(source_digest, "source_digest"),
            route_digest=_validate_digest(route_digest, "route_digest"),
            profile_digest=_validate_digest(profile_digest, "profile_digest"),
            prompt_digest=_validate_digest(prompt_digest, "prompt_digest"),
            static_check_digest=_validate_digest(static_check_digest, "static_check_digest"),
            contract_digest=_validate_digest(contract_digest, "contract_digest"),
            executor_digest=_validate_digest(executor_digest, "executor_digest"),
            artifact_contract_digest=_validate_digest(
                artifact_contract_digest,
                "artifact_contract_digest",
            ),
            manifest_version=manifest_version,
            canonicalization_version=canonicalization_version,
        )
        manifest.validate()
        return manifest

    @classmethod
    def from_values(
        cls,
        *,
        workflow_name: str | None = None,
        workflow: Any = UNSET,
        config: Any = UNSET,
        input_value: Any = UNSET,
        source: Any = UNSET,
        route: Any = UNSET,
        profile: Any = UNSET,
        prompt: Any = UNSET,
        static_checks: Any = UNSET,
        contract: Any = UNSET,
        executor: Any = UNSET,
        artifact_contract: Any = UNSET,
        workflow_digest: str | None = None,
        config_digest: str | None = None,
        input_digest: str | None = None,
        source_digest: str | None = None,
        route_digest: str | None = None,
        profile_digest: str | None = None,
        prompt_digest: str | None = None,
        static_check_digest: str | None = None,
        contract_digest: str | None = None,
        executor_digest: str | None = None,
        artifact_contract_digest: str | None = None,
        prompt_rubric_digest: str | None = None,
        source_revision_digest: str | None = None,
        executor_contract_digest: str | None = None,
        executor_implementation_digest: str | None = None,
        artifact_digest: str | None = None,
    ) -> PinnedRunManifest:
        workflow_value = (
            {"name": workflow_name}
            if workflow is UNSET and workflow_name is not None
            else None
            if workflow is UNSET
            else workflow
        )
        contract_value = (
            {
                "contract": None if contract is UNSET else contract,
                "executor": None if executor is UNSET else executor,
                "artifact_contract": None
                if artifact_contract is UNSET
                else artifact_contract,
            }
            if contract is UNSET
            else contract
        )
        return cls.from_digests(
            workflow_digest=_digest_or_value(workflow_digest, workflow_value),
            config_digest=_digest_or_value(
                config_digest,
                None if config is UNSET else config,
            ),
            input_digest=_digest_or_value(
                input_digest,
                None if input_value is UNSET else input_value,
            ),
            source_digest=_digest_or_value(
                source_digest,
                None if source is UNSET else source,
            ),
            route_digest=_digest_or_value(
                route_digest,
                None if route is UNSET else route,
            ),
            profile_digest=_digest_or_value(
                profile_digest,
                None if profile is UNSET else profile,
            ),
            prompt_digest=_digest_or_value(
                _coalesce_digest_alias(
                    prompt_digest,
                    prompt_rubric_digest,
                    "prompt_digest",
                ),
                None if prompt is UNSET else prompt,
            ),
            static_check_digest=_digest_or_value(
                static_check_digest,
                None if static_checks is UNSET else static_checks,
            ),
            contract_digest=_digest_or_value(
                _coalesce_digest_alias(
                    contract_digest,
                    executor_contract_digest,
                    "contract_digest",
                ),
                contract_value,
            ),
            executor_digest=_digest_or_value(
                _coalesce_digest_alias(
                    executor_digest,
                    executor_implementation_digest,
                    "executor_digest",
                ),
                None if executor is UNSET else executor,
            ),
            artifact_contract_digest=_digest_or_value(
                _coalesce_digest_alias(
                    artifact_contract_digest,
                    artifact_digest,
                    "artifact_contract_digest",
                ),
                None if artifact_contract is UNSET else artifact_contract,
            ),
        )

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> PinnedRunManifest:
        if not isinstance(value, Mapping):
            raise ManifestValidationError(
                f"{ManifestValidationError.code}: object_required"
            )
        expected = set(MANIFEST_DIGEST_FIELDS) | {
            "manifest_version",
            "canonicalization_version",
        }
        unknown = set(value) - expected
        missing = expected - set(value)
        if unknown:
            raise ManifestValidationError(
                f"{ManifestValidationError.code}: unknown_field:{sorted(unknown)[0]}"
            )
        if missing:
            raise ManifestValidationError(
                f"{ManifestValidationError.code}: missing_field:{sorted(missing)[0]}"
            )
        return cls.from_digests(
            **{field: value[field] for field in MANIFEST_DIGEST_FIELDS},
            manifest_version=value["manifest_version"],
            canonicalization_version=value["canonicalization_version"],
        )

    @classmethod
    def from_json(cls, value: str) -> PinnedRunManifest:
        try:
            decoded = json.loads(value)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ManifestValidationError(
                f"{ManifestValidationError.code}: invalid_json"
            ) from exc
        if not isinstance(decoded, dict):
            raise ManifestValidationError(f"{ManifestValidationError.code}: object_required")
        return cls.from_mapping(decoded)

    def validate(self) -> None:
        if (
            not isinstance(self.manifest_version, int)
            or isinstance(self.manifest_version, bool)
            or self.manifest_version != MANIFEST_VERSION
        ):
            raise ManifestValidationError(
                f"{ManifestValidationError.code}: unsupported_manifest_version"
            )
        if (
            not isinstance(self.canonicalization_version, int)
            or isinstance(self.canonicalization_version, bool)
            or self.canonicalization_version != CANONICALIZATION_VERSION
        ):
            raise ManifestValidationError(
                f"{ManifestValidationError.code}: unsupported_canonicalization_version"
            )
        for field in MANIFEST_DIGEST_FIELDS:
            _validate_digest(getattr(self, field), field)

    def identity_dict(self) -> dict[str, object]:
        return {
            "manifest_version": self.manifest_version,
            "canonicalization_version": self.canonicalization_version,
            **{field: getattr(self, field) for field in MANIFEST_DIGEST_FIELDS},
        }

    @property
    def identity_digest(self) -> str:
        return definition_digest(self.identity_dict())

    @property
    def manifest_digest(self) -> str:
        return definition_digest(self.to_dict())

    def to_dict(self) -> dict[str, object]:
        return {
            **{field: getattr(self, field) for field in MANIFEST_DIGEST_FIELDS},
            "manifest_version": self.manifest_version,
            "canonicalization_version": self.canonicalization_version,
        }

    def to_json(self) -> str:
        return canonical_json(self.to_dict())

    def __getitem__(self, key: str) -> object:
        return self.to_dict()[key]

    @property
    def prompt_rubric_digest(self) -> str:
        return self.prompt_digest

    @property
    def executor_contract_digest(self) -> str:
        return self.contract_digest

    @property
    def executor_implementation_digest(self) -> str:
        return self.executor_digest

    @property
    def artifact_digest(self) -> str:
        return self.artifact_contract_digest

    @property
    def source_revision_digest(self) -> str:
        return self.source_digest


def run_identity_digest(
    workflow: str,
    executor_kind: str | Enum,
    manifest: PinnedRunManifest,
) -> str:
    """Return the aggregate normalized identity used by run dedupe."""

    if not isinstance(workflow, str) or not workflow.strip():
        raise ValueError("workflow_must_be_non_empty_string")
    executor_value = executor_kind.value if isinstance(executor_kind, Enum) else executor_kind
    if not isinstance(executor_value, str) or not executor_value.strip():
        raise ValueError("executor_kind_must_be_non_empty_string")
    if not isinstance(manifest, PinnedRunManifest):
        raise TypeError("manifest_must_be_pinned_run_manifest")
    manifest.validate()
    return definition_digest(
        {
            "workflow": workflow.strip(),
            "executor_kind": executor_value.strip(),
            "manifest": manifest.identity_dict(),
        }
    )


def _validate_digest(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ManifestValidationError(f"{ManifestValidationError.code}: {field}_invalid")
    digest = value.removeprefix(_DIGEST_PREFIX)
    if len(digest) != _DIGEST_LENGTH or any(
        character not in "0123456789abcdef" for character in digest
    ):
        raise ManifestValidationError(f"{ManifestValidationError.code}: {field}_invalid")
    return _DIGEST_PREFIX + digest


def _digest_or_value(explicit: str | None, value: Any) -> str:
    if explicit is not None:
        return _validate_digest(explicit, "digest")
    return definition_digest(value)


def _coalesce_digest_alias(
    primary: str | None,
    alias: str | None,
    field: str,
) -> str | None:
    if alias is None:
        return primary
    if primary not in (None, EMPTY_DIGEST):
        raise ManifestValidationError(
            f"{ManifestValidationError.code}: duplicate_digest:{field}"
        )
    return alias


# Names retained for callers that prefer the shorter vocabulary.
PinnedManifest = PinnedRunManifest
RunManifest = PinnedRunManifest
