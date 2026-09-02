"""Node-driven contracts for responsive main-grid order continuity."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DASHBOARD_JS = REPO_ROOT / "src/herdr_orchestrator/dashboard/static/dashboard.js"

_DRIVER = r"""
const fs = require("fs");
const { isDeepStrictEqual } = require("util");
const source = fs.readFileSync(process.argv[1], "utf8");

function sourceBetween(startMarker, endMarker) {
  const start = source.indexOf(startMarker);
  const end = source.indexOf(endMarker, start);
  if (start < 0 || end < 0) throw new Error(`missing source markers: ${startMarker}`);
  return source.slice(start, end);
}

function classes(initial) {
  const values = new Set(initial);
  return {
    add(...names) {
      names.forEach((name) => values.add(name));
    },
    remove(...names) {
      names.forEach((name) => values.delete(name));
    },
    contains(name) {
      return values.has(name);
    },
  };
}

let grid;
let scrollOffset = 0;
function panel(name, initialClasses) {
  return {
    name,
    classList: classes(initialClasses),
    contains(target) {
      return target === this;
    },
    getBoundingClientRect() {
      const top = 100 + grid.children.indexOf(this) * 300 - scrollOffset;
      return { top, bottom: top + 200 };
    },
    focus() {},
  };
}

const board = panel("board", ["panel", "board-panel"]);
const rail = panel("rail", ["right-rail"]);
grid = {
  children: [board, rail],
  dataset: { attention: "active" },
  get firstElementChild() {
    return this.children[0];
  },
  insertBefore(element, before) {
    this.children = this.children.filter((candidate) => candidate !== element);
    const index = this.children.indexOf(before);
    this.children.splice(index < 0 ? 0 : index, 0, element);
  },
};

const animationSource = sourceBetween("function motionAllowed()", "function setMetric(");
const orderSource = sourceBetween(
  "function captureMainGridContinuity(",
  "function currentTopologyTouchMode(",
);
const createRuntime = new Function(
  "board",
  "rail",
  "grid",
  `
    let mainGridOrderKey = "";
    let reducedMotion = false;
    const compactViewport = { matches: false };
    const document = { activeElement: null };
    const innerHeight = 844;
    const frameCallbacks = [];
    const requestAnimationFrame = (callback) => {
      frameCallbacks.push(callback);
      return frameCallbacks.length;
    };
    const window = {
      matchMedia(query) {
        return { matches: query.includes("prefers-reduced-motion") && reducedMotion };
      },
      scrollBy() {},
    };
    const byId = (id) => {
      if (id !== "main-grid") throw new Error("unexpected id: " + id);
      return grid;
    };
    ${animationSource}
    ${orderSource}
    return {
      syncMainGridOrder,
      setCompact(value) {
        compactViewport.matches = value;
      },
      setReducedMotion(value) {
        reducedMotion = value;
      },
      flushFrame() {
        frameCallbacks.splice(0).forEach((callback) => callback(16));
      },
      state() {
        return {
          order: grid.children.map((element) => element.name),
          boardAnimated: board.classList.contains("is-order-changing"),
          railAnimated: rail.classList.contains("is-order-changing"),
        };
      },
    };
  `,
);
const runtime = createRuntime(board, rail, grid);

const results = [];
function check(name, actual, expected) {
  results.push({ name, ok: isDeepStrictEqual(actual, expected), actual, expected });
}

runtime.syncMainGridOrder();
runtime.setCompact(true);
runtime.syncMainGridOrder();
runtime.setCompact(false);
runtime.syncMainGridOrder();
check("pre-frame reversal clears every animation owner", runtime.state(), {
  order: ["board", "rail"],
  boardAnimated: false,
  railAnimated: false,
});
runtime.flushFrame();
check("stale pre-frame callbacks cannot restore the old owner", runtime.state(), {
  order: ["board", "rail"],
  boardAnimated: true,
  railAnimated: false,
});

runtime.setCompact(true);
runtime.syncMainGridOrder();
check("active reversal clears the previous animation before the next frame", runtime.state(), {
  order: ["rail", "board"],
  boardAnimated: false,
  railAnimated: false,
});
runtime.flushFrame();
check("compact handoff animates only the rail", runtime.state(), {
  order: ["rail", "board"],
  boardAnimated: false,
  railAnimated: true,
});

runtime.setCompact(false);
runtime.syncMainGridOrder();
runtime.flushFrame();
check("desktop handoff animates only the board", runtime.state(), {
  order: ["board", "rail"],
  boardAnimated: true,
  railAnimated: false,
});

runtime.setReducedMotion(true);
runtime.setCompact(true);
runtime.syncMainGridOrder();
runtime.flushFrame();
check("reduced motion leaves no order animation owner", runtime.state(), {
  order: ["rail", "board"],
  boardAnimated: false,
  railAnimated: false,
});

process.stdout.write(`${JSON.stringify(results)}\n`);
if (results.some((result) => !result.ok)) process.exitCode = 1;
"""


def test_main_grid_order_animation_is_exclusive_during_rapid_reversal() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for Dashboard JavaScript contract tests")
    result = subprocess.run(
        [node, "-e", _DRIVER, str(DASHBOARD_JS)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 0
    ), f"Node main-grid matrix failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    checks = json.loads(result.stdout)
    assert len(checks) == 6
    assert [check for check in checks if not check["ok"]] == []
