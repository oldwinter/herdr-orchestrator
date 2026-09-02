"""Typed, fail-closed feature flags for optional external integrations."""

from __future__ import annotations

import os
from collections.abc import Mapping
from enum import StrEnum


class FeatureFlag(StrEnum):
    SENTRY_EXPORT = "sentry_export"
    POSTHOG_ANALYTICS = "posthog_analytics"
    WEBHOOK_ALERTS = "webhook_alerts"


_ENVIRONMENT_VARIABLES = {
    FeatureFlag.SENTRY_EXPORT: "HERDR_FEATURE_SENTRY_EXPORT",
    FeatureFlag.POSTHOG_ANALYTICS: "HERDR_FEATURE_POSTHOG_ANALYTICS",
    FeatureFlag.WEBHOOK_ALERTS: "HERDR_FEATURE_WEBHOOK_ALERTS",
}
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


class FeatureFlagError(ValueError):
    pass


def _environment_variable(flag: FeatureFlag) -> str:
    if not isinstance(flag, FeatureFlag):
        raise FeatureFlagError(f"feature_flag_unknown: {flag!r}")
    try:
        return _ENVIRONMENT_VARIABLES[flag]
    except KeyError as exc:
        raise FeatureFlagError(f"feature_flag_undeclared: {flag.value}") from exc


def enabled(
    flag: FeatureFlag,
    environ: Mapping[str, str] | None = None,
) -> bool:
    if environ is not None and not isinstance(environ, Mapping):
        raise FeatureFlagError("feature_flag_environment_invalid")
    values = os.environ if environ is None else environ
    variable = _environment_variable(flag)
    raw_value = values.get(variable, "false")
    if not isinstance(raw_value, str):
        raise FeatureFlagError(f"feature_flag_invalid: {variable}")
    raw = raw_value.strip().casefold()
    if raw in _TRUE_VALUES:
        return True
    if raw in _FALSE_VALUES:
        return False
    raise FeatureFlagError(f"feature_flag_invalid: {variable}")


def declared_flags() -> Mapping[FeatureFlag, str]:
    return dict(_ENVIRONMENT_VARIABLES)
