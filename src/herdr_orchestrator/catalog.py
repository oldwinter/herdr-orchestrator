from __future__ import annotations

import json
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from herdr_orchestrator.model import Harness, HarnessProfile, WorkerConfig

PROFILE_CONTEXT_MAX_CHARS = 50_000
PROFILE_KEYS = {
    "schema_version",
    "harness",
    "display_name",
    "summary",
    "strengths",
    "best_for",
    "avoid_for",
    "traits",
    "context_file",
}


class CatalogError(ValueError):
    pass


def load_harness_profiles(directory: Path) -> tuple[HarnessProfile, ...]:
    if not directory.is_dir():
        raise CatalogError(f"profiles_dir_not_found: {directory}")
    profiles: list[HarnessProfile] = []
    seen: set[Harness] = set()
    for path in sorted(directory.glob("*.toml")):
        try:
            raw = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise CatalogError(f"profile_invalid_toml: {path}: {exc}") from exc
        profile = _load_profile(path, raw)
        if profile.harness in seen:
            raise CatalogError(f"profile_harness_duplicate: {profile.harness.value}")
        profiles.append(profile)
        seen.add(profile.harness)
    if not profiles:
        raise CatalogError("profiles_empty")
    return tuple(profiles)


def profiles_for_workers(
    profiles: Iterable[HarnessProfile],
    workers: Iterable[WorkerConfig],
) -> tuple[HarnessProfile, ...]:
    by_harness = {profile.harness: profile for profile in profiles}
    selected: list[HarnessProfile] = []
    for worker in workers:
        profile = by_harness.get(worker.harness)
        if profile is None:
            raise CatalogError(f"worker_profile_missing: {worker.harness.value}")
        selected.append(profile)
    return tuple(selected)


def profile_for_harness(
    profiles: Iterable[HarnessProfile],
    harness: Harness,
) -> HarnessProfile:
    for profile in profiles:
        if profile.harness is harness:
            return profile
    raise CatalogError(f"harness_profile_not_found: {harness.value}")


def compact_catalog_payload(
    profiles: Iterable[HarnessProfile],
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "harnesses": [
            {
                "harness": profile.harness.value,
                "display_name": profile.display_name,
                "summary": profile.summary,
                "strengths": list(profile.strengths),
                "best_for": list(profile.best_for),
                "avoid_for": list(profile.avoid_for),
                "traits": list(profile.traits),
                "profile_ref": f"harness:{profile.harness.value}",
            }
            for profile in profiles
        ],
    }


def render_compact_catalog(profiles: Iterable[HarnessProfile]) -> str:
    payload = compact_catalog_payload(profiles)
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def load_profile_context(profile: HarnessProfile) -> str:
    context = profile.context_file.read_text(encoding="utf-8").strip()
    if not context:
        raise CatalogError(f"profile_context_empty: {profile.harness.value}")
    if len(context) > PROFILE_CONTEXT_MAX_CHARS:
        raise CatalogError(f"profile_context_too_large: {profile.harness.value}")
    return context


def full_profile_payload(profile: HarnessProfile) -> dict[str, object]:
    compact = compact_catalog_payload((profile,))["harnesses"]
    assert isinstance(compact, list)
    payload = dict(compact[0])
    payload["context"] = load_profile_context(profile)
    return {"schema_version": 1, "profile": payload}


def execution_prompt(profile: HarnessProfile, task_prompt: str) -> str:
    return (
        "# Dynamically loaded harness profile\n\n"
        f"Selected harness: {profile.harness.value} ({profile.display_name})\n\n"
        f"{load_profile_context(profile)}\n\n"
        "# Task packet\n\n"
        f"{task_prompt.strip()}"
    )


def _load_profile(path: Path, raw: Mapping[str, Any]) -> HarnessProfile:
    unknown_keys = set(raw) - PROFILE_KEYS
    if unknown_keys:
        raise CatalogError(f"profile_unknown_keys: {','.join(sorted(unknown_keys))}")
    schema_version = _integer(raw, "schema_version", minimum=1, maximum=1)
    harness_value = _string(raw, "harness", maximum=32)
    try:
        harness = Harness(harness_value)
    except ValueError as exc:
        raise CatalogError(f"profile_harness_unsupported: {harness_value}") from exc
    context_file = _resolve_context_file(path.parent, raw)
    return HarnessProfile(
        schema_version=schema_version,
        harness=harness,
        display_name=_string(raw, "display_name", maximum=80),
        summary=_string(raw, "summary", maximum=300),
        strengths=_string_list(raw, "strengths", maximum_items=12, maximum_length=120),
        best_for=_string_list(raw, "best_for", maximum_items=12, maximum_length=160),
        avoid_for=_string_list(raw, "avoid_for", maximum_items=12, maximum_length=160),
        traits=_string_list(raw, "traits", maximum_items=12, maximum_length=160),
        context_file=context_file,
    )


def _resolve_context_file(base: Path, raw: Mapping[str, Any]) -> Path:
    value = _string(raw, "context_file", maximum=255)
    candidate = Path(value)
    if candidate.is_absolute() or ".." in candidate.parts:
        raise CatalogError("profile_context_path_invalid")
    path = (base / candidate).resolve()
    if not path.is_relative_to(base.resolve()):
        raise CatalogError("profile_context_path_invalid")
    if not path.is_file():
        raise CatalogError(f"profile_context_not_found: {path}")
    return path


def _string(data: Mapping[str, Any], key: str, *, maximum: int) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise CatalogError(f"profile_{key}_invalid")
    return value.strip()


def _string_list(
    data: Mapping[str, Any],
    key: str,
    *,
    maximum_items: int,
    maximum_length: int,
) -> tuple[str, ...]:
    value = data.get(key)
    if (
        not isinstance(value, list)
        or not value
        or len(value) > maximum_items
        or not all(
            isinstance(item, str) and item.strip() and len(item) <= maximum_length for item in value
        )
    ):
        raise CatalogError(f"profile_{key}_invalid")
    return tuple(item.strip() for item in value)


def _integer(
    data: Mapping[str, Any],
    key: str,
    *,
    minimum: int,
    maximum: int,
) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise CatalogError(f"profile_{key}_invalid")
    return value
