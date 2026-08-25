"""Privacy-safe local telemetry with optional external exporters."""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from herdr_orchestrator.feature_flags import FeatureFlag, enabled

MAX_TEXT = 300
SENSITIVE_KEY = re.compile(
    r"(?i)(authorization|cookie|credential|password|prompt|secret|session|terminal|token)"
)
TOKEN_SHAPE = re.compile(
    r"(?i)\b(?:gh[oprsu]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"bearer\s+[A-Za-z0-9._~+/=-]{12,})\b"
)
ASSIGNMENT_SHAPE = re.compile(r"(?i)\b(authorization|password|secret|token)\s*[:=]\s*\S+")


def sanitize(value: object, *, key: str = "") -> object:
    if SENSITIVE_KEY.search(key):
        return "[REDACTED]"
    if isinstance(value, Mapping):
        return {
            str(item_key): sanitize(item_value, key=str(item_key))
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        scrubbed = TOKEN_SHAPE.sub("[REDACTED]", value)
        scrubbed = ASSIGNMENT_SHAPE.sub(r"\1=[REDACTED]", scrubbed)
        return " ".join(scrubbed.split())[:MAX_TEXT]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_TEXT]


def anonymous_install_id(workspace: Path) -> str:
    return hashlib.sha256(str(workspace.resolve()).encode()).hexdigest()[:16]


class Observability:
    def __init__(
        self,
        root: Path,
        workflow: str,
        *,
        environ: Mapping[str, str] | None = None,
        clock: Any = time.time,
    ) -> None:
        self.root = root
        self.workflow = workflow
        self.environ = os.environ if environ is None else environ
        self.clock = clock

    def event(
        self,
        name: str,
        *,
        correlation_id: str,
        fields: Mapping[str, object] | None = None,
    ) -> None:
        payload = self._payload(name, correlation_id, fields)
        self._append("events.jsonl", payload)
        self._sentry(payload)
        self._posthog(payload)

    def metric(
        self,
        name: str,
        value: float,
        *,
        correlation_id: str,
        fields: Mapping[str, object] | None = None,
    ) -> None:
        payload = self._payload(name, correlation_id, fields)
        payload["value"] = value
        self._append("metrics.jsonl", payload)

    def alert(
        self,
        name: str,
        *,
        correlation_id: str,
        fields: Mapping[str, object] | None = None,
    ) -> None:
        payload = self._payload(name, correlation_id, fields)
        self._append("alerts.jsonl", payload)
        if not enabled(FeatureFlag.WEBHOOK_ALERTS, self.environ):
            return
        url = self.environ.get("HERDR_ALERT_WEBHOOK_URL", "")
        if not url.startswith("https://"):
            return
        self._post_json(url, payload)

    def _payload(
        self,
        name: str,
        correlation_id: str,
        fields: Mapping[str, object] | None,
    ) -> dict[str, object]:
        payload: dict[str, object] = {
            "correlation_id": correlation_id,
            "event": name,
            "observed_at": self.clock(),
            "schema_version": 1,
            "workflow": self.workflow,
        }
        if fields:
            payload["fields"] = sanitize(fields)
        return payload

    def _append(self, filename: str, payload: Mapping[str, object]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with (self.root / filename).open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
        except OSError:
            return

    def _sentry(self, payload: Mapping[str, object]) -> None:
        if not enabled(FeatureFlag.SENTRY_EXPORT, self.environ):
            return
        dsn = self.environ.get("SENTRY_DSN", "")
        match = re.fullmatch(r"https://([^@/]+)@([^/]+)/(.+)", dsn)
        if match is None:
            return
        public_key, host, project_id = match.groups()
        envelope = {
            **payload,
            "install_id": anonymous_install_id(self.root.parent),
            "level": "error",
            "platform": "python",
            "release": self.environ.get("HERDR_RELEASE", "development"),
        }
        self._post_json(
            f"https://{host}/api/{project_id}/store/?sentry_key={public_key}" "&sentry_version=7",
            envelope,
        )

    def _posthog(self, payload: Mapping[str, object]) -> None:
        if not enabled(FeatureFlag.POSTHOG_ANALYTICS, self.environ):
            return
        api_key = self.environ.get("POSTHOG_API_KEY", "")
        host = self.environ.get("POSTHOG_HOST", "https://us.i.posthog.com")
        if not api_key or not host.startswith("https://"):
            return
        properties: dict[str, object] = {
            "distinct_id": anonymous_install_id(self.root.parent),
            "workflow": self.workflow,
        }
        fields = payload.get("fields")
        if isinstance(fields, dict):
            properties.update(fields)
        self._post_json(
            f"{host.rstrip('/')}/capture/",
            {
                "api_key": api_key,
                "event": payload["event"],
                "properties": properties,
            },
        )

    def _post_json(self, url: str, payload: Mapping[str, object]) -> None:
        request = urllib.request.Request(
            url,
            data=json.dumps(sanitize(payload)).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            # URL construction and callers enforce HTTPS before this bounded request.
            with urllib.request.urlopen(request, timeout=2):  # nosec B310
                pass
        except OSError:
            return
