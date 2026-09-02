"use strict";

function record(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function records(value) {
  return Array.isArray(value) ? value.filter(record) : [];
}

function values(value) {
  return Array.isArray(value) ? value : [];
}

function textValue(value, fallback = "") {
  if (typeof topologyText === "function") return topologyText(value, fallback);
  if (value === null || value === undefined) return fallback;
  if (typeof value === "object" || typeof value === "function" || typeof value === "symbol") {
    return fallback;
  }
  try {
    return String(value);
  } catch (_error) {
    return fallback;
  }
}

function finiteNumber(value) {
  try {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  } catch (_error) {
    return null;
  }
}

function epochDate(epoch) {
  const value = finiteNumber(epoch);
  if (value === null) return null;
  const date = new Date(value * 1000);
  return Number.isFinite(date.getTime()) ? date : null;
}

function escapeHtml(value) {
  return textValue(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(epoch) {
  const date = epochDate(epoch);
  if (!date) return "unknown";
  try {
    return new Intl.DateTimeFormat(undefined, {
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  } catch (_error) {
    return "unknown";
  }
}

function formatDateTime(epoch) {
  const date = epochDate(epoch);
  if (!date) return "unknown";
  try {
    return new Intl.DateTimeFormat(undefined, {
      month: "short",
      day: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
    }).format(date);
  } catch (_error) {
    return "unknown";
  }
}

function formatAge(epoch, now = Date.now() / 1000) {
  const value = finiteNumber(epoch);
  const current = finiteNumber(now);
  if (value === null || current === null) return "unknown";
  const seconds = Math.max(0, Math.round(current - value));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function isoTime(epoch) {
  const date = epochDate(epoch);
  if (!date) return "";
  try {
    return date.toISOString();
  } catch (_error) {
    return "";
  }
}

function reduceRecoveryState(state, event) {
  if (event.type === "transport-error") {
    return {
      browserTransport: { kind: "error", warning: textValue(event.warning) },
      awaitingFreshSnapshot: true,
    };
  }
  if (event.type === "transport-open") {
    return {
      browserTransport: { kind: "open" },
      awaitingFreshSnapshot: state.awaitingFreshSnapshot,
    };
  }
  if (event.type === "snapshot-accepted") {
    return {
      browserTransport: { kind: "open" },
      awaitingFreshSnapshot: false,
    };
  }
  throw new Error("recovery_event_invalid");
}

function sourceWarningMessage(browserTransport, snapshot) {
  if (record(browserTransport) && browserTransport.kind === "error") {
    return textValue(
      browserTransport.warning,
      "Live snapshot stream unavailable; reconnecting",
    );
  }
  if (!record(snapshot)) return null;

  const health = record(snapshot.source_health) ? snapshot.source_health : {};
  const parts = [];
  if (health.queue !== "ok") parts.push("Queue observation unavailable");
  if (health.herdr !== "ok") {
    parts.push(`Herdr observation unavailable: ${textValue(health.herdr_error, "unknown")}`);
  }
  return parts.length ? parts.join(" · ") : null;
}
