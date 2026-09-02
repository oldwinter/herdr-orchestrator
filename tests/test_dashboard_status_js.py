"""Node-driven contracts for Dashboard recovery state and warning projection."""

from __future__ import annotations

import json
import shutil
import subprocess
from importlib.resources import files

import pytest

DASHBOARD_UTILS_JS = files("herdr_orchestrator.dashboard.static").joinpath("dashboard-utils.js")

_DRIVER = r"""
const fs = require("fs");
const { isDeepStrictEqual } = require("util");
const src = fs.readFileSync(process.argv[1], "utf8");
const phase = process.argv[2];
const moduleLike = { exports: {} };
const evaluate = new Function(
  "module",
  "exports",
  "require",
  src
    + "\n;module.exports = { "
    + "reduceRecoveryState: typeof reduceRecoveryState === 'function' "
    + "? reduceRecoveryState : null, "
    + "sourceWarningMessage: typeof sourceWarningMessage === 'function' "
    + "? sourceWarningMessage : null };",
);
evaluate(moduleLike, moduleLike.exports, require);
const dashboard = moduleLike.exports;

function deepFreeze(value) {
  if (value && typeof value === "object" && !Object.isFrozen(value)) {
    Object.freeze(value);
    Object.values(value).forEach(deepFreeze);
  }
  return value;
}

const results = [];
function check(name, actual, expected) {
  results.push({ name, ok: isDeepStrictEqual(actual, expected), actual, expected });
}

if (phase === "reducer") {
  if (typeof dashboard.reduceRecoveryState !== "function") {
    throw new Error("missing reduceRecoveryState");
  }
  const initial = deepFreeze({
    browserTransport: { kind: "connecting" },
    awaitingFreshSnapshot: false,
  });
  const initialBefore = JSON.stringify(initial);
  const firstErrorEvent = deepFreeze({
    type: "transport-error",
    warning: "Live snapshot stream unavailable; reconnecting",
  });
  const firstErrorEventBefore = JSON.stringify(firstErrorEvent);
  const error = dashboard.reduceRecoveryState(initial, firstErrorEvent);
  check("transport error starts recovery freshness", error, {
    browserTransport: {
      kind: "error",
      warning: "Live snapshot stream unavailable; reconnecting",
    },
    awaitingFreshSnapshot: true,
  });
  check("transport error leaves state input immutable", JSON.stringify(initial), initialBefore);
  check(
    "transport error leaves event input immutable",
    JSON.stringify(firstErrorEvent),
    firstErrorEventBefore,
  );

  const repeatedError = dashboard.reduceRecoveryState(
    deepFreeze(error),
    deepFreeze({
      type: "transport-error",
      warning: "Live snapshot stream unavailable; reconnecting",
    }),
  );
  check("repeated identical error is idempotent", repeatedError, error);

  const open = dashboard.reduceRecoveryState(
    deepFreeze(error),
    deepFreeze({ type: "transport-open" }),
  );
  check("open clears only transport ownership", open, {
    browserTransport: { kind: "open" },
    awaitingFreshSnapshot: true,
  });
  check(
    "repeated open preserves recovery freshness",
    dashboard.reduceRecoveryState(
      deepFreeze(open),
      deepFreeze({ type: "transport-open" }),
    ),
    open,
  );

  const secondError = dashboard.reduceRecoveryState(
    deepFreeze(open),
    deepFreeze({
      type: "transport-error",
      warning: "Snapshot stream unavailable; reconnecting",
    }),
  );
  check("error then open then error ends in the newest error", secondError, {
    browserTransport: {
      kind: "error",
      warning: "Snapshot stream unavailable; reconnecting",
    },
    awaitingFreshSnapshot: true,
  });

  const accepted = dashboard.reduceRecoveryState(
    deepFreeze(open),
    deepFreeze({ type: "snapshot-accepted" }),
  );
  check("only an accepted SSE snapshot clears recovery freshness", accepted, {
    browserTransport: { kind: "open" },
    awaitingFreshSnapshot: false,
  });
} else if (phase === "warning") {
  if (typeof dashboard.sourceWarningMessage !== "function") {
    throw new Error("missing sourceWarningMessage");
  }
  const open = deepFreeze({ kind: "open" });
  const transportError = deepFreeze({
    kind: "error",
    warning: "Live snapshot stream unavailable; reconnecting",
  });
  const fixtures = deepFreeze({
    healthy: { source_health: { queue: "ok", herdr: "ok" } },
    queue: { source_health: { queue: "unavailable", herdr: "ok" } },
    herdr: {
      source_health: {
        queue: "ok",
        herdr: "unavailable",
        herdr_error: "observer_timeout",
      },
    },
    both: {
      source_health: {
        queue: "unavailable",
        herdr: "unavailable",
        herdr_error: "observer_timeout",
      },
    },
    malformed: { source_health: "not-an-object" },
  });
  const fixturesBefore = JSON.stringify(fixtures);
  const openBefore = JSON.stringify(open);
  const errorBefore = JSON.stringify(transportError);

  check(
    "healthy sources are quiet",
    dashboard.sourceWarningMessage(open, fixtures.healthy),
    null,
  );
  check(
    "queue degradation uses existing copy",
    dashboard.sourceWarningMessage(open, fixtures.queue),
    "Queue observation unavailable",
  );
  check(
    "Herdr degradation uses existing copy",
    dashboard.sourceWarningMessage(open, fixtures.herdr),
    "Herdr observation unavailable: observer_timeout",
  );
  check(
    "both degradations retain queue then Herdr order",
    dashboard.sourceWarningMessage(open, fixtures.both),
    "Queue observation unavailable \u00b7 Herdr observation unavailable: observer_timeout",
  );
  check(
    "malformed source health fails visible",
    dashboard.sourceWarningMessage(open, fixtures.malformed),
    "Queue observation unavailable \u00b7 Herdr observation unavailable: unknown",
  );
  check("no snapshot has no source warning", dashboard.sourceWarningMessage(open, null), null);
  check(
    "transport warning takes priority over degraded sources",
    dashboard.sourceWarningMessage(transportError, fixtures.both),
    "Live snapshot stream unavailable; reconnecting",
  );
  check("warning projection leaves snapshots immutable", JSON.stringify(fixtures), fixturesBefore);
  check("warning projection leaves open transport immutable", JSON.stringify(open), openBefore);
  check(
    "warning projection leaves error transport immutable",
    JSON.stringify(transportError),
    errorBefore,
  );
} else {
  throw new Error(`unknown phase: ${phase}`);
}

process.stdout.write(`${JSON.stringify(results)}\n`);
if (results.some((result) => !result.ok)) process.exitCode = 1;
"""


def _run_node_matrix(phase: str) -> list[dict[str, object]]:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for Dashboard JavaScript contract tests")
    result = subprocess.run(
        [node, "-e", _DRIVER, str(DASHBOARD_UTILS_JS), phase],
        check=False,
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 0
    ), f"Node {phase} matrix failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    return json.loads(result.stdout)


def test_recovery_state_reducer_matrix() -> None:
    results = _run_node_matrix("reducer")
    failures = [result for result in results if not result["ok"]]
    assert len(results) == 8
    assert failures == []


def test_source_warning_message_matrix() -> None:
    results = _run_node_matrix("warning")
    failures = [result for result in results if not result["ok"]]
    assert len(results) == 10
    assert failures == []
