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


class FeatureFlagError(ValueError):
    pass


def enabled(
    flag: FeatureFlag,
    environ: Mapping[str, str] | None = None,
) -> bool:
    values = os.environ if environ is None else environ
    variable = _ENVIRONMENT_VARIABLES[flag]
    raw = values.get(variable, "false").strip().casefold()
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off", ""}:
        return False
    raise FeatureFlagError(f"feature_flag_invalid: {variable}")


def declared_flags() -> Mapping[FeatureFlag, str]:
    return dict(_ENVIRONMENT_VARIABLES)
