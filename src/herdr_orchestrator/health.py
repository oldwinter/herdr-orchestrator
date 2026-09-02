"""Compatibility import surface for the harness health policy."""

from herdr_orchestrator.harness_health import (
    HEALTH_COOLDOWN,
    HEALTH_UNKNOWN,
    READINESS_EXPIRED,
    EligibilitySnapshot,
    HarnessHealth,
    HarnessHealthError,
    HarnessHealthRecord,
    HarnessHealthStatus,
    HealthProbe,
    HealthSource,
    HealthStatus,
)

__all__ = [
    "EligibilitySnapshot",
    "HarnessHealth",
    "HarnessHealthError",
    "HarnessHealthRecord",
    "HarnessHealthStatus",
    "HealthProbe",
    "HealthStatus",
    "HealthSource",
    "HEALTH_COOLDOWN",
    "HEALTH_UNKNOWN",
    "READINESS_EXPIRED",
]
