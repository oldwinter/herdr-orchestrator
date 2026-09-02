"""Node-driven contracts for lifecycle reading and grid continuity."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
TIMELINE_JS = REPO_ROOT / "src/herdr_orchestrator/dashboard/static/timeline-continuity.js"

_DRIVER = r"""
const fs = require("fs");
const { isDeepStrictEqual } = require("util");
const source = fs.readFileSync(process.argv[1], "utf8");
const continuity = new Function(
  source + "\n;return { captureTimelineContinuity, restoreTimelineContinuity };",
)();

let scrollOffset = 0;
const scrollCalls = [];
global.innerHeight = 844;
global.window = {
  scrollBy(options) {
    scrollCalls.push(options);
    scrollOffset += options.top;
  },
};

function element(id, rectangle, animations) {
  return {
    dataset: { eventId: id },
    getBoundingClientRect() {
      const top = rectangle.top - scrollOffset;
      return {
        left: rectangle.left,
        right: rectangle.left + rectangle.width,
        top,
        bottom: top + rectangle.height,
        width: rectangle.width,
        height: rectangle.height,
      };
    },
    animate(keyframes, options) {
      const animation = { id: "" };
      animations.push({ id, keyframes, options, animation });
      return animation;
    },
  };
}

function timeline(elements) {
  return {
    querySelectorAll(selector) {
      if (selector !== "[data-event-id]") throw new Error(`unexpected selector: ${selector}`);
      return elements;
    },
  };
}

const results = [];
function check(name, actual, expected) {
  results.push({ name, ok: isDeepStrictEqual(actual, expected), actual, expected });
}

const captureAnimations = [];
const historicalItems = [
  element("newest", { left: 17, top: -176, width: 356, height: 88 }, captureAnimations),
  element("anchor", { left: 17, top: -0.3125, width: 356, height: 88 }, captureAnimations),
  element("next", { left: 17, top: 87.6875, width: 356, height: 88 }, captureAnimations),
  element("offscreen", { left: 17, top: 900, width: 356, height: 88 }, captureAnimations),
];
const historical = continuity.captureTimelineContinuity(timeline(historicalItems));
check("history chooses first visible event as reading anchor", historical.readingAnchor, {
  eventId: "anchor",
  top: -0.3125,
});
check("capture stores only visible card positions", [...historical.positions.keys()], [
  "anchor",
  "next",
]);

scrollOffset = 0;
const latestItems = [
  element("newest", { left: 17, top: 36, width: 356, height: 88 }, []),
  element("next", { left: 17, top: 124, width: 356, height: 88 }, []),
];
const latest = continuity.captureTimelineContinuity(timeline(latestItems));
check("latest view stays uncompensated", latest.readingAnchor, null);

scrollOffset = 0;
scrollCalls.length = 0;
const restoredAnimations = [];
const restoredItems = [
  element("anchor", { left: 17, top: 87.6875, width: 356, height: 88 }, restoredAnimations),
  element("next", { left: 17, top: 175.6875, width: 356, height: 88 }, restoredAnimations),
];
continuity.restoreTimelineContinuity(timeline(restoredItems), historical, { animate: true });
check("reading anchor receives exact instant compensation", scrollCalls, [{
  top: 88,
  left: 0,
  behavior: "instant",
}]);
check("compensated anchor returns to its original viewport top", {
  top: restoredItems[0].getBoundingClientRect().top,
  animations: restoredAnimations.length,
}, { top: -0.3125, animations: 0 });

scrollOffset = 0;
scrollCalls.length = 0;
const desktopBeforeAnimations = [];
const desktopBefore = continuity.captureTimelineContinuity(timeline([
  element("a", { left: 25, top: 36, width: 347.5, height: 88 }, desktopBeforeAnimations),
  element("b", { left: 372.5, top: 36, width: 347.5, height: 88 }, desktopBeforeAnimations),
]));
const desktopAnimations = [];
const desktopAfter = [
  element("a", { left: 372.5, top: 36, width: 347.5, height: 88 }, desktopAnimations),
  element("b", { left: 720, top: 36, width: 347.5, height: 88 }, desktopAnimations),
];
continuity.restoreTimelineContinuity(timeline(desktopAfter), desktopBefore, { animate: true });
check("desktop cards FLIP from their prior visual position", desktopAnimations.map((entry) => ({
  id: entry.id,
  from: entry.keyframes[0].transform,
  to: entry.keyframes[1].transform,
  duration: entry.options.duration,
  easing: entry.options.easing,
  animationId: entry.animation.id,
})), [
  {
    id: "a",
    from: "translate(-347.5px, 0px)",
    to: "translate(0px, 0px)",
    duration: 272,
    easing: "cubic-bezier(0.16, 1, 0.3, 1)",
    animationId: "timeline-continuity",
  },
  {
    id: "b",
    from: "translate(-347.5px, 0px)",
    to: "translate(0px, 0px)",
    duration: 272,
    easing: "cubic-bezier(0.16, 1, 0.3, 1)",
    animationId: "timeline-continuity",
  },
]);

const reducedAnimations = [];
continuity.restoreTimelineContinuity(timeline([
  element("a", { left: 720, top: 36, width: 347.5, height: 88 }, reducedAnimations),
]), desktopBefore, { animate: false });
check("reduced motion skips FLIP", reducedAnimations.length, 0);

process.stdout.write(`${JSON.stringify(results)}\n`);
if (results.some((result) => !result.ok)) process.exitCode = 1;
"""


def test_timeline_continuity_module_contracts() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node is required for timeline continuity tests")
    result = subprocess.run(
        [node, "-e", _DRIVER, str(TIMELINE_JS)],
        check=False,
        capture_output=True,
        text=True,
    )
    assert (
        result.returncode == 0
    ), f"Node timeline matrix failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    checks = json.loads(result.stdout)
    assert len(checks) == 7
    assert [check for check in checks if not check["ok"]] == []


def test_dashboard_loads_and_uses_timeline_continuity() -> None:
    static = REPO_ROOT / "src/herdr_orchestrator/dashboard/static"
    index = (static / "index.html").read_text()
    dashboard = (static / "dashboard.js").read_text()

    continuity_asset = "/assets/timeline-continuity.js"
    dashboard_asset = "/assets/dashboard.js"
    assert continuity_asset in index
    assert index.index(continuity_asset) < index.index(dashboard_asset)

    render = dashboard[
        dashboard.index("function renderTimeline") : dashboard.index("function timelineVisualId")
    ]
    assert "captureTimelineContinuity" in render
    assert "restoreTimelineContinuity" in render
    assert "animate: motionAllowed()" in render
