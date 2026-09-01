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
from urllib.parse import quote, urlsplit

from herdr_orchestrator.feature_flags import FeatureFlag, enabled

MAX_TEXT = 300
MAX_INPUT_TEXT = 4096
MAX_NESTING = 16
MAX_PAYLOAD_BYTES = 64 * 1024
MAX_URL_LENGTH = 2048
EXPORT_TIMEOUT_SECONDS = 2
REDACTED = "[REDACTED]"
SENSITIVE_KEY = re.compile(
    r"(?i)(api[_-]?key|authorization|cookie|credential|password|prompt|secret|session|terminal|token|"
    r"(?:^|[_-])(?:path|pane)(?:[_-]id)?(?:$|[_-]))"
)
TOKEN_SHAPE = re.compile(
    r"(?i)\b(?:gh[oprsu]_[A-Za-z0-9_]{20,}|sk-[A-Za-z0-9_-]{20,}|"
    r"bearer\s+[A-Za-z0-9._~+/=-]{12,})\b"
)
ASSIGNMENT_SHAPE = re.compile(
    r"(?i)\b(api[_-]?key|authorization|cookie|credential|password|prompt|secret|session|terminal|token|"
    r"path|pane(?:[_ -]?id)?)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)"
)
PATH_SHAPE = re.compile(
    r"(?<![\w:/])(?:"
    r"(?:~|\.\.?)/[^\s,;]+|"
    r"/(?:[^\s/,;]+/)*[^\s,;]+|"
    r"[A-Za-z]:\\(?:[^\s\\,;]+\\)*[^\s,;]+"
    r")"
)
PANE_ID_SHAPE = re.compile(r"(?i)\b(?:w[\w.-]+:p[\w.-]+|pane[-:][\w.:-]+)\b")


def sanitize(value: object, *, key: str = "") -> object:
    try:
        return _sanitize(value, key=key, depth=0, seen=set())
    except Exception:
        return REDACTED


def _sanitize(value: object, *, key: str, depth: int, seen: set[int]) -> object:
    if SENSITIVE_KEY.search(key):
        return REDACTED
    if depth >= MAX_NESTING:
        return REDACTED
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in seen:
            return REDACTED
        seen.add(marker)
        try:
            sanitized: dict[str, object] = {}
            for item_key, item_value in value.items():
                key_text = _sanitize_key(item_key)
                sanitized[key_text] = _sanitize(
                    item_value,
                    key=key_text,
                    depth=depth + 1,
                    seen=seen,
                )
            return sanitized
        finally:
            seen.remove(marker)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in seen:
            return REDACTED
        seen.add(marker)
        try:
            return [_sanitize(item, key="", depth=depth + 1, seen=seen) for item in value]
        finally:
            seen.remove(marker)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    try:
        text = value if isinstance(value, str) else str(value)
    except Exception:
        return REDACTED
    return _sanitize_text(text)


def _sanitize_key(value: object) -> str:
    try:
        return str(value)[:MAX_TEXT]
    except Exception:
        return REDACTED


def _sanitize_text(value: str) -> str:
    text = value[:MAX_INPUT_TEXT]
    scrubbed = TOKEN_SHAPE.sub(REDACTED, text)
    scrubbed = ASSIGNMENT_SHAPE.sub(r"\1=" + REDACTED, scrubbed)
    scrubbed = PATH_SHAPE.sub(REDACTED, scrubbed)
    scrubbed = PANE_ID_SHAPE.sub(REDACTED, scrubbed)
    return " ".join(scrubbed.split())[:MAX_TEXT]


def _https_url(value: object) -> str | None:
    if not isinstance(value, str) or not value or len(value) > MAX_URL_LENGTH:
        return None
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.hostname is None:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    return value


def _serialize(payload: object, *, already_sanitized: bool = False) -> str | None:
    try:
        safe_payload = payload if already_sanitized else sanitize(payload)
        if not isinstance(safe_payload, Mapping):
            return None
        serialized = json.dumps(
            safe_payload,
            allow_nan=False,
            ensure_ascii=False,
            sort_keys=True,
        )
        if len(serialized.encode("utf-8")) > MAX_PAYLOAD_BYTES:
            return None
        return serialized
    except Exception:
        return None


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
        try:
            payload = self._payload(name, correlation_id, fields)
        except Exception:
            return
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
        try:
            payload = self._payload(name, correlation_id, fields)
            payload["value"] = sanitize(value, key="value")
        except Exception:
            return
        self._append("metrics.jsonl", payload)

    def alert(
        self,
        name: str,
        *,
        correlation_id: str,
        fields: Mapping[str, object] | None = None,
    ) -> None:
        try:
            payload = self._payload(name, correlation_id, fields)
        except Exception:
            return
        self._append("alerts.jsonl", payload)
        try:
            if not self._flag_enabled(FeatureFlag.WEBHOOK_ALERTS):
                return
            self._post_json(self.environ.get("HERDR_ALERT_WEBHOOK_URL", ""), payload)
        except Exception:
            return

    def _payload(
        self,
        name: str,
        correlation_id: str,
        fields: Mapping[str, object] | None,
    ) -> dict[str, object]:
        observed_at = self.clock()
        payload: dict[str, object] = {
            "correlation_id": correlation_id,
            "event": name,
            "observed_at": observed_at,
            "schema_version": 1,
            "workflow": self.workflow,
        }
        if fields is not None:
            payload["fields"] = fields
        sanitized = sanitize(payload)
        if isinstance(sanitized, dict):
            return sanitized
        return {
            "correlation_id": sanitize(correlation_id),
            "event": sanitize(name),
            "observed_at": sanitize(observed_at),
            "schema_version": 1,
            "workflow": sanitize(self.workflow),
        }

    def _append(self, filename: str, payload: Mapping[str, object]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            serialized = _serialize(payload)
            if serialized is None:
                return
            with (self.root / filename).open("a", encoding="utf-8") as handle:
                handle.write(serialized + "\n")
        except Exception:
            return

    def _flag_enabled(self, flag: FeatureFlag) -> bool:
        try:
            return enabled(flag, self.environ)
        except Exception:
            return False

    def _sentry(self, payload: Mapping[str, object]) -> None:
        try:
            if not self._flag_enabled(FeatureFlag.SENTRY_EXPORT):
                return
            dsn: object = self.environ.get("SENTRY_DSN", "")
            if not isinstance(dsn, str):
                return
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
                "https://"
                f"{host}/api/{quote(project_id, safe='')}/store/?sentry_key="
                f"{quote(public_key, safe='')}&sentry_version=7",
                envelope,
            )
        except Exception:
            return

    def _posthog(self, payload: Mapping[str, object]) -> None:
        try:
            if not self._flag_enabled(FeatureFlag.POSTHOG_ANALYTICS):
                return
            api_key = self.environ.get("POSTHOG_API_KEY", "")
            host = _https_url(self.environ.get("POSTHOG_HOST", "https://us.i.posthog.com"))
            if not isinstance(api_key, str) or not api_key or host is None:
                return
            properties: dict[str, object] = {
                "distinct_id": anonymous_install_id(self.root.parent),
                "workflow": self.workflow,
            }
            fields = payload.get("fields")
            if isinstance(fields, Mapping):
                properties.update(fields)
            self._post_json(
                f"{host.rstrip('/')}/capture/",
                {
                    "event": payload.get("event", REDACTED),
                    "properties": properties,
                },
                api_key=api_key,
            )
        except Exception:
            return

    def _post_json(
        self,
        url: object,
        payload: Mapping[str, object],
        *,
        api_key: str | None = None,
    ) -> None:
        try:
            endpoint = _https_url(url)
            if endpoint is None:
                return
            safe_payload = sanitize(payload)
            if not isinstance(safe_payload, Mapping):
                return
            safe_payload = dict(safe_payload)
            if api_key is not None:
                if not isinstance(api_key, str) or not api_key:
                    return
                safe_payload["api_key"] = api_key
            serialized = _serialize(safe_payload, already_sanitized=True)
            if serialized is None:
                return
            request = urllib.request.Request(
                endpoint,
                data=serialized.encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=EXPORT_TIMEOUT_SECONDS):  # nosec B310
                pass
        except Exception:
            return
