"""Node-driven contract tests for the dashboard topology graph module.

The pure, DOM-free topology projection (topology.js) is evaluated inside Node
with a small CommonJS harness and exercised against fixtures. These tests
pin the contracts the browser renderer relies on:

- compound nesting project -> worktree -> tab -> pane and status classes;
- deterministic preset layout and stable structure signatures so status-only
  SSE updates never move nodes (the incremental-update contract in dashboard.js);
- the v1 workspaces -> projects fallback projection;
- stable node identity encoding and status class mapping.

Requires a `node` binary; skipped when it is not available.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from importlib.resources import files
from typing import cast

import pytest

TOPOLOGY_JS = files("herdr_orchestrator.dashboard.static").joinpath("topology.js")
TOPOLOGY_STYLE_JS = files("herdr_orchestrator.dashboard.static").joinpath("topology-style.js")
INDEX_HTML = files("herdr_orchestrator.dashboard.static").joinpath("index.html")
DASHBOARD_CSS = files("herdr_orchestrator.dashboard.static").joinpath("dashboard.css")
DASHBOARD_JS = files("herdr_orchestrator.dashboard.static").joinpath("dashboard.js")

_DRIVER = r"""
const fs = require("fs");
const src = fs.readFileSync(process.argv[1], "utf8");
const moduleLike = { exports: {} };
const evaluate = new Function(
  "module",
  "exports",
  "require",
  src
    + "\n;module.exports = { stateClass, normalizedProjects, topologyGraph, "
    + "topologyPresetPositions, topologyNavigationOrder, topologySelectionDirection, "
    + "topologyFocusViewport, topologyRebaseViewportCapture, topologyId, "
    + "topologyZoomViewport: typeof topologyZoomViewport === 'function' "
    + "? topologyZoomViewport : null, "
    + "topologyViewportMotionDuration: typeof topologyViewportMotionDuration === "
    + "'function' ? topologyViewportMotionDuration : null };",
);
evaluate(moduleLike, moduleLike.exports, require);
const t = moduleLike.exports;

// Compound identity constants (encodeURIComponent output for ":" and "|").
const PID = "project:workflow%3Aexample";
const WT1 = `${PID}|worktree:w1`;
const WT2 = `${PID}|worktree:w2`;
const TAB1 = `${WT1}|tab:w1%3At1`;
const TAB2 = `${WT2}|tab:w2%3At1`;
const P1 = `${TAB1}|pane:w1%3Ap1`;
const P2 = `${TAB1}|pane:w1%3Ap2`;
const P3 = `${TAB2}|pane:w2%3Ap1`;
const P3ADD = `${TAB1}|pane:w1%3Ap3`;
const EPID = "project:workflow%3Aempty";
const EWT1 = `${EPID}|worktree:we`;
const EWT2 = `${EPID}|worktree:we2`;
const ETAB1 = `${EWT1}|tab:we%3At1`;

const results = [];
function check(name, ok, detail) {
  results.push({ name, ok: Boolean(ok), detail: String(detail) });
}
function findElement(elements, id) {
  return elements.find((element) => element.data.id === id);
}
function classesOf(elements, id) {
  const element = findElement(elements, id);
  return element ? element.classes : "";
}
function deepClone(value) {
  return JSON.parse(JSON.stringify(value));
}

const baseProjects = [{
  project_id: "workflow:example",
  label: "example",
  worktrees: [{
    worktree_id: "w1",
    workspace_id: "w1",
    label: "Main",
    branch: "main",
    path: "/repo",
    is_linked_worktree: false,
    tabs: [{
      tab_id: "w1:t1",
      label: "Tab 1",
      focused: false,
      panes: [
        {
          pane_id: "w1:p1",
          agent: { name: "worker-one", agent_status: "working" },
          agent_status: "working",
          focused: false,
        },
        {
          pane_id: "w1:p2",
          agent: { name: "worker-two", agent_status: "idle" },
          agent_status: "idle",
          focused: true,
        },
      ],
    }],
  }, {
    worktree_id: "w2",
    workspace_id: "w2",
    label: "Linked",
    branch: "orchestrator/task-2",
    path: "/repo/.orchestrator/worktrees/task-2",
    is_linked_worktree: true,
    tabs: [{
      tab_id: "w2:t1",
      label: "Task 2",
      panes: [{
        pane_id: "w2:p1",
        agent: null,
        agent_status: "blocked",
        focused: false,
      }],
    }],
  }],
}];

// ---- counts and compound structure --------------------------------------
const graph = t.topologyGraph(deepClone(baseProjects));
const g = graph.elements;
check("counts", graph.counts.projects === 1 && graph.counts.worktrees === 2
  && graph.counts.tabs === 2 && graph.counts.panes === 3,
  JSON.stringify(graph.counts));

const projectNode = findElement(g, PID);
check("project node kind", projectNode && projectNode.data.kind === "project"
  && projectNode.data.parent === undefined && projectNode.classes === "kind-project",
  JSON.stringify(projectNode && projectNode.data));

const worktree1 = findElement(g, WT1);
const worktree2 = findElement(g, WT2);
check("worktree nesting", worktree1 && worktree1.data.parent === PID
  && worktree1.data.kind === "worktree" && worktree1.classes === "kind-worktree",
  JSON.stringify(worktree1 && [worktree1.data, worktree1.classes]));
check("linked worktree class", worktree2 && worktree2.classes.includes("is-linked"),
  worktree2 && worktree2.classes);

const tab1 = findElement(g, TAB1);
check("tab nesting", tab1 && tab1.data.parent === WT1 && tab1.data.kind === "tab",
  JSON.stringify(tab1 && tab1.data));

const pane1 = findElement(g, P1);
const pane2 = findElement(g, P2);
const pane3 = findElement(g, P3);
check("pane nesting", pane1 && pane1.data.parent === TAB1 && pane1.data.kind === "pane",
  JSON.stringify(pane1 && pane1.data));

check("pane status classes", pane1 && pane1.classes.includes("status-working")
  && pane2 && pane2.classes.includes("status-idle") && pane2.classes.includes("is-focused")
  && pane3 && pane3.classes.includes("status-blocked"),
  [pane1 && pane1.classes, pane2 && pane2.classes, pane3 && pane3.classes].join(" | "));

check("agent detail and shell fallback",
  pane1 && pane1.data.detail === "worker-one · working" && pane1.data.agent === "worker-one"
  && pane3 && pane3.data.agent === "shell" && pane3.data.detail === "shell · blocked"
  && pane3.data.status === "blocked",
  JSON.stringify([pane1 && pane1.data.detail, pane3 && pane3.data]));

const navigationOrder = t.topologyNavigationOrder(graph);
check("navigation order follows deterministic graph order",
  JSON.stringify(navigationOrder) === JSON.stringify(g.map((element) => element.data.id))
  && navigationOrder[0] === PID && navigationOrder.at(-1) === P3,
  JSON.stringify(navigationOrder));
check("navigation order is stable and empty-safe",
  JSON.stringify(navigationOrder) === JSON.stringify(t.topologyNavigationOrder(graph))
  && t.topologyNavigationOrder({ elements: [] }).length === 0
  && t.topologyNavigationOrder({}).length === 0,
  JSON.stringify(t.topologyNavigationOrder({ elements: [] })));
const direction = (key, modifiers = {}) => t.topologySelectionDirection({
  ctrlKey: true,
  altKey: false,
  metaKey: false,
  key,
  ...modifiers,
});
check("ctrl arrow selection direction is isolated from pan keys",
  direction("ArrowRight") === 1
  && direction("ArrowDown") === 1
  && direction("ArrowLeft") === -1
  && direction("ArrowUp") === -1
  && direction("ArrowRight", { ctrlKey: false }) === null
  && direction("ArrowRight", { altKey: true }) === null
  && direction("ArrowRight", { metaKey: true }) === null,
  "unexpected direction mapping");

// ---- signatures: status-only updates must not change structure -----------
const changedStatuses = deepClone(baseProjects);
changedStatuses[0].worktrees[0].tabs[0].panes[0].agent.agent_status = "idle";
changedStatuses[0].worktrees[0].tabs[0].panes[0].agent_status = "idle";
const graphChanged = t.topologyGraph(changedStatuses);
check("structure signature stable on status-only change",
  graph.structureSignature === graphChanged.structureSignature,
  `${graph.structureSignature} vs ${graphChanged.structureSignature}`);
check("content signature changes on status-only change",
  graph.contentSignature !== graphChanged.contentSignature,
  "content signatures identical");

const addedPane = deepClone(baseProjects);
addedPane[0].worktrees[0].tabs[0].panes.push({
  pane_id: "w1:p3",
  agent: { name: "worker-three", agent_status: "unknown" },
  agent_status: "unknown",
  focused: false,
});
const graphAdded = t.topologyGraph(addedPane);
check("structure signature changes when a pane is added",
  graph.structureSignature !== graphAdded.structureSignature,
  "structure signatures identical");

// ---- deterministic preset layout ----------------------------------------
const positionsA = JSON.stringify(graph.positions);
const positionsB = JSON.stringify(t.topologyGraph(deepClone(baseProjects)).positions);
check("layout is deterministic", positionsA === positionsB, "position maps differ");

const allPanesPositioned = g.every((element) => {
  if (element.data.kind !== "pane") return true;
  const position = graph.positions[element.data.id];
  return position && Number.isFinite(position.x) && Number.isFinite(position.y);
});
check("every pane has a numeric position", allPanesPositioned,
  JSON.stringify(graph.positions));

// Compound parents (project/worktree/tab with children) get no explicit
// position: Cytoscape wraps descendants. Only childless nodes are pinned.
const childlessProjects = [{
  project_id: "workflow:empty",
  label: "empty",
  worktrees: [{
    worktree_id: "we",
    workspace_id: "we",
    label: "Empty wt",
    tabs: [{ tab_id: "we:t1", label: "Empty tab", panes: [] }],
  }, {
    worktree_id: "we2",
    workspace_id: "we2",
    label: "No tabs",
    tabs: [],
  }],
}];
const emptyGraph = t.topologyGraph(childlessProjects);
const emptyTabPos = emptyGraph.positions[ETAB1];
const emptyWtPos = emptyGraph.positions[EWT2];
check("childless tab and worktree get explicit positions",
  emptyTabPos && Number.isFinite(emptyTabPos.x) && Number.isFinite(emptyTabPos.y)
  && emptyWtPos && Number.isFinite(emptyWtPos.x) && Number.isFinite(emptyWtPos.y),
  JSON.stringify({ emptyTabPos, emptyWtPos }));

const p1Pos = graph.positions[P1];
const p2Pos = graph.positions[P2];
const wt2PanePos = graph.positions[P3];
const addedP3Pos = graphAdded.positions[P3ADD];
check("two panes sit side by side in a 2-column grid",
  p1Pos && p2Pos && p1Pos.y === p2Pos.y && p2Pos.x > p1Pos.x,
  JSON.stringify([p1Pos, p2Pos]));
check("a third pane starts a second row",
  addedP3Pos && p1Pos && addedP3Pos.y > p1Pos.y && addedP3Pos.x === p1Pos.x,
  JSON.stringify([p1Pos, addedP3Pos]));
check("second worktree renders as a column to the right",
  p1Pos && wt2PanePos && wt2PanePos.x > p1Pos.x && wt2PanePos.x > p2Pos.x,
  JSON.stringify([p1Pos, p2Pos, wt2PanePos]));

// ---- v1 fallback projection ---------------------------------------------
const passthrough = t.normalizedProjects({ projects: baseProjects }, "example");
check("projects passthrough", passthrough === baseProjects, JSON.stringify(passthrough));

const v1Workspaces = [{
  workspace_id: "w1",
  label: "Main",
  worktree: { label: "Main", branch: "main", path: "/repo", is_linked_worktree: false },
  tabs: [{ tab_id: "w1:t1", panes: [{ pane_id: "w1:p1" }] }],
}];
const fallback = t.normalizedProjects({ workspaces: v1Workspaces }, "multi-harness");
check("v1 workspaces fallback",
  fallback.length === 1 && fallback[0].label === "multi-harness"
  && fallback[0].worktrees[0].workspace_id === "w1"
  && fallback[0].worktrees[0].branch === "main"
  && fallback[0].worktrees[0].tabs[0].panes[0].pane_id === "w1:p1",
  JSON.stringify(fallback));

check("empty topology yields no projects",
  t.normalizedProjects({ workspaces: [] }, "x").length === 0
  && t.normalizedProjects({}, "x").length === 0, "fallback not empty");

// ---- identity and status helpers ----------------------------------------
check("topologyId encodes identity",
  t.topologyId("pane", "w1:p1", 0) === "pane:w1%3Ap1"
  && t.topologyId("project", "", "fallback") === "project:fallback",
  [t.topologyId("pane", "w1:p1", 0), t.topologyId("project", "", "fallback")].join(" | "));

check("stateClass mapping",
  t.stateClass("working") === "working"
  && t.stateClass("in_progress") === "in-progress"
  && t.stateClass("") === "unknown"
  && t.stateClass(null) === "unknown"
  && t.stateClass("Working!") === "working-",
  [t.stateClass("working"), t.stateClass("in_progress"), t.stateClass(""),
    t.stateClass(null), t.stateClass("Working!")].join(" | "));

// ---- malformed snapshots and identity collisions ------------------------
check("null topology is treated as empty",
  Array.isArray(t.normalizedProjects(null, "x"))
  && t.normalizedProjects(null, "x").length === 0
  && t.topologyGraph(null).elements.length === 0
  && t.topologyGraph([null]).elements.length === 0,
  "null topology raised or produced projects");

let incompleteGraph = null;
try {
  incompleteGraph = t.topologyGraph(t.normalizedProjects({
    workspaces: [
      null,
      {
        workspace_id: "w1",
        tabs: [null, { tab_id: "w1:t1", panes: [null, { pane_id: "w1:p1" }] }],
      },
    ],
  }, "incomplete"));
} catch (error) {
  incompleteGraph = { error: String(error) };
}
check("incomplete records do not abort graph construction",
  incompleteGraph && !incompleteGraph.error
  && incompleteGraph.counts.worktrees === 1
  && incompleteGraph.counts.tabs === 1
  && incompleteGraph.counts.panes === 1,
  JSON.stringify(incompleteGraph));

let duplicateGraph = null;
try {
  duplicateGraph = t.topologyGraph([
    {
      project_id: "same",
      worktrees: [
        {
          worktree_id: "same",
          tabs: [
            { tab_id: "same", panes: [{ pane_id: "same" }, { pane_id: "same" }] },
            { tab_id: "same", panes: [] },
          ],
        },
        { worktree_id: "same", tabs: [] },
      ],
    },
    { project_id: "same", worktrees: [] },
  ]);
} catch (error) {
  duplicateGraph = { error: String(error) };
}
const duplicateIds = duplicateGraph && duplicateGraph.elements
  ? duplicateGraph.elements.map((element) => element.data.id)
  : [];
check("duplicate identities remain uniquely addressable",
  duplicateGraph && !duplicateGraph.error
  && duplicateIds.length === 8
  && new Set(duplicateIds).size === duplicateIds.length
  && duplicateIds[0] === "project:same",
  JSON.stringify(duplicateGraph));

let escapedIdentity = null;
try {
  escapedIdentity = t.topologyId(
    "pane",
    "unsafe!'()*" + String.fromCharCode(0xD800),
    0,
  );
} catch (error) {
  escapedIdentity = `error:${String(error)}`;
}
check("identity encoding is well formed and strict",
  escapedIdentity === "pane:unsafe%21%27%28%29%2A%EF%BF%BD",
  String(escapedIdentity));

const falseyGraph = t.topologyGraph([
  { project_id: "first", worktrees: [] },
  {
    project_id: 0,
    worktrees: [{ worktree_id: 0, tabs: [{ tab_id: 0, panes: [{ pane_id: 0 }] }] }],
  },
]);
const falseyIds = falseyGraph.elements.map((element) => element.data.id);
check("falsey identities remain distinct from fallbacks",
  t.topologyId("node", 0, 7) === "node:0"
  && t.topologyId("node", false, 7) === "node:false"
  && falseyIds.includes("project:0")
  && falseyIds.some((id) => id.endsWith("|worktree:0"))
  && falseyIds.some((id) => id.endsWith("|tab:0"))
  && falseyIds.some((id) => id.endsWith("|pane:0")),
  falseyIds.join(" | "));

let malformedPositions = null;
try {
  malformedPositions = t.topologyPresetPositions(null);
} catch (error) {
  malformedPositions = `error:${String(error)}`;
}
check("malformed layout input is empty",
  JSON.stringify(malformedPositions) === "{}",
  JSON.stringify(malformedPositions));

const prototypePositions = t.topologyPresetPositions([{ id: "__proto__", worktrees: [] }]);
check("layout positions do not mutate object prototypes",
  Object.prototype.hasOwnProperty.call(prototypePositions, "__proto__")
  && Number.isFinite(prototypePositions["__proto__"].x),
  JSON.stringify(prototypePositions));

let hostileGraph = null;
try {
  hostileGraph = t.topologyGraph([{
    project_id: { toString: null },
    label: { toString: null },
    worktrees: [{
      worktree_id: { toString: null },
      tabs: [{
        tab_id: { toString: null },
        panes: [{ pane_id: { toString: null }, agent: { name: { toString: null } } }],
      }],
    }],
  }]);
} catch (error) {
  hostileGraph = { error: String(error) };
}
check("hostile scalar objects do not abort graph construction",
  hostileGraph && !hostileGraph.error
  && hostileGraph.counts.projects === 1
  && hostileGraph.counts.worktrees === 1
  && hostileGraph.counts.tabs === 1
  && hostileGraph.counts.panes === 1,
  JSON.stringify(hostileGraph));
const focusViewport = t.topologyFocusViewport({
  viewport: { zoom: 0.2493428464, pan: { x: 172, y: 220 } },
  subject: {
    kind: "leaf",
    modelCenter: { x: 400, y: 300 },
    modelLabelPx: 14,
  },
  visibleRect: { x1: 18, y1: 18, x2: 344, y2: 428 },
  minZoom: 0.08,
  maxZoom: 2.6,
});
check("focus viewport makes the selected leaf readable without zooming out",
  focusViewport.zoom === 0.72
  && focusViewport.zoom * 14 >= 9
  && focusViewport.pan.x === -107
  && focusViewport.pan.y === 7,
  JSON.stringify(focusViewport));

const fittingLeafBounds = { x1: 360, y1: 270, x2: 440, y2: 320 };
const fittingContextBounds = { x1: 300, y1: 220, x2: 500, y2: 320 };
const contextLeafFocus = t.topologyFocusViewport({
  viewport: { zoom: 0.25, pan: { x: 0, y: 0 } },
  subject: {
    kind: "leaf",
    modelCenter: { x: 400, y: 300 },
    modelLabelPx: 14,
    context: {
      leafBounds: fittingLeafBounds,
      contextBounds: fittingContextBounds,
    },
  },
  visibleRect: { x1: 18, y1: 18, x2: 344, y2: 428 },
  minZoom: 0.08,
  maxZoom: 2.6,
});
check("fitting leaf context centers the optional bounds at readable zoom",
  contextLeafFocus.zoom === 0.72
  && contextLeafFocus.pan.x === -107
  && Math.abs(contextLeafFocus.pan.y - 28.6) < 1e-12,
  JSON.stringify(contextLeafFocus));

const frozenVisibleRect = { x1: 18, y1: 18, x2: 268, y2: 422 };
const frozenLeafBounds = { x1: 44.5, y1: 5.5, x2: 205.5, y2: 72.5 };
const frozenContextBounds = { x1: -212.5, y1: -81.5, x2: 205.5, y2: 72.5 };
const frozenCompactFocus = t.topologyFocusViewport({
  viewport: {
    zoom: 0.21697722567287786,
    pan: { x: 87.07412008281574, y: 200.58053830227743 },
  },
  subject: {
    kind: "leaf",
    modelCenter: { x: 125, y: 39 },
    modelLabelPx: 14,
    context: {
      leafBounds: frozenLeafBounds,
      contextBounds: frozenContextBounds,
    },
  },
  visibleRect: frozenVisibleRect,
  minZoom: 0.08,
  maxZoom: 2.6,
});
const frozenRenderedLeaf = {
  x1: frozenLeafBounds.x1 * frozenCompactFocus.zoom + frozenCompactFocus.pan.x,
  y1: frozenLeafBounds.y1 * frozenCompactFocus.zoom + frozenCompactFocus.pan.y,
  x2: frozenLeafBounds.x2 * frozenCompactFocus.zoom + frozenCompactFocus.pan.x,
  y2: frozenLeafBounds.y2 * frozenCompactFocus.zoom + frozenCompactFocus.pan.y,
};
check("impossible compact context keeps zoom and chooses the closest leaf-containing pan",
  frozenCompactFocus.zoom === 0.72
  && Math.abs(frozenCompactFocus.pan.x - 120.04) < 1e-12
  && Math.abs(frozenCompactFocus.pan.y - 223.24) < 1e-12
  && frozenRenderedLeaf.x1 >= frozenVisibleRect.x1
  && frozenRenderedLeaf.y1 >= frozenVisibleRect.y1
  && frozenRenderedLeaf.x2 <= frozenVisibleRect.x2
  && frozenRenderedLeaf.y2 <= frozenVisibleRect.y2
  && Math.abs(frozenRenderedLeaf.x2 - frozenVisibleRect.x2) < 1e-12
  && frozenCompactFocus.pan.x < 145.52,
  JSON.stringify({ frozenCompactFocus, frozenRenderedLeaf }));

const focusWithOptionalContext = (contextFields) => t.topologyFocusViewport({
  viewport: { zoom: 0.2493428464, pan: { x: 172, y: 220 } },
  subject: {
    kind: "leaf",
    modelCenter: { x: 400, y: 300 },
    modelLabelPx: 14,
    ...contextFields,
  },
  visibleRect: { x1: 18, y1: 18, x2: 344, y2: 428 },
  minZoom: 0.08,
  maxZoom: 2.6,
});
const fallbackContexts = [
  { contextBounds: fittingContextBounds },
  { context: { contextBounds: fittingContextBounds } },
  { context: { leafBounds: fittingLeafBounds } },
  { context: {
    leafBounds: { ...fittingLeafBounds, x1: Number.NaN },
    contextBounds: fittingContextBounds,
  } },
  { context: {
    leafBounds: fittingLeafBounds,
    contextBounds: { ...fittingContextBounds, y2: Number.POSITIVE_INFINITY },
  } },
  { context: {
    leafBounds: { x1: 360, y1: 270, x2: 360, y2: 320 },
    contextBounds: fittingContextBounds,
  } },
  { context: {
    leafBounds: fittingLeafBounds,
    contextBounds: { x1: 300, y1: 220, x2: 500, y2: 220 },
  } },
  { context: {
    leafBounds: fittingLeafBounds,
    contextBounds: { x1: 370, y1: 280, x2: 430, y2: 310 },
  } },
];
const fallbackViewports = fallbackContexts.map(focusWithOptionalContext);
check("malformed, non-containing, and half-configured context uses exact leaf fallback",
  fallbackViewports.every((target) => (
    target.zoom === 0.72 && target.pan.x === -107 && target.pan.y === 7
  )),
  JSON.stringify(fallbackViewports));

const infeasibleLeafFocus = focusWithOptionalContext({
  context: {
    leafBounds: { x1: 0, y1: 0, x2: 500, y2: 50 },
    contextBounds: { x1: -50, y1: -10, x2: 500, y2: 100 },
  },
});
check("fixed-zoom leaf that cannot fit uses exact leaf fallback",
  infeasibleLeafFocus.zoom === 0.72
  && infeasibleLeafFocus.pan.x === -107
  && infeasibleLeafFocus.pan.y === 7,
  JSON.stringify(infeasibleLeafFocus));

const projectBounds = { x1: -366, y1: -119.5, x2: 870, y2: 321 };
const projectVisibleRect = { x1: 18, y1: 18, x2: 338, y2: 358.5 };
const projectFocus = t.topologyFocusViewport({
  viewport: {
    zoom: 0.2510396975425331,
    pan: { x: 127.85482041587902, y: 197.5319470699433 },
  },
  subject: {
    kind: "container",
    modelBounds: projectBounds,
    modelLabelPx: 14,
  },
  visibleRect: projectVisibleRect,
  minZoom: 0.08,
  maxZoom: 2.6,
});
const renderedProjectBounds = {
  x1: projectBounds.x1 * projectFocus.zoom + projectFocus.pan.x,
  y1: projectBounds.y1 * projectFocus.zoom + projectFocus.pan.y,
  x2: projectBounds.x2 * projectFocus.zoom + projectFocus.pan.x,
  y2: projectBounds.y2 * projectFocus.zoom + projectFocus.pan.y,
};
check("focus viewport contains the selected project interaction bounds",
  Math.abs(projectFocus.zoom - 0.2588996763754045) < 1e-12
  && renderedProjectBounds.x1 >= projectVisibleRect.x1 - 1e-9
  && renderedProjectBounds.y1 >= projectVisibleRect.y1 - 1e-9
  && renderedProjectBounds.x2 <= projectVisibleRect.x2 + 1e-9
  && renderedProjectBounds.y2 <= projectVisibleRect.y2 + 1e-9,
  JSON.stringify({ projectFocus, renderedProjectBounds }));

const twelvePixelFocus = t.topologyFocusViewport({
  viewport: { zoom: 0.25, pan: { x: 0, y: 0 } },
  subject: {
    kind: "leaf",
    modelCenter: { x: 100, y: 50 },
    modelLabelPx: 12,
  },
  visibleRect: { x1: 0, y1: 0, x2: 300, y2: 200 },
  minZoom: 0.08,
  maxZoom: 2.6,
});
const alreadyReadableFocus = t.topologyFocusViewport({
  viewport: { zoom: 1.1, pan: { x: 10, y: 20 } },
  subject: {
    kind: "leaf",
    modelCenter: { x: 100, y: 50 },
    modelLabelPx: 14,
  },
  visibleRect: { x1: 0, y1: 0, x2: 300, y2: 200 },
  minZoom: 0.08,
  maxZoom: 2.6,
});
const cappedFocus = t.topologyFocusViewport({
  viewport: { zoom: 0.25, pan: { x: 0, y: 0 } },
  subject: {
    kind: "leaf",
    modelCenter: { x: 100, y: 50 },
    modelLabelPx: 12,
  },
  visibleRect: { x1: 0, y1: 0, x2: 300, y2: 200 },
  minZoom: 0.08,
  maxZoom: 0.5,
});
check("focus viewport handles font thresholds and zoom bounds",
  twelvePixelFocus.zoom === 0.75
  && alreadyReadableFocus.zoom === 1.1
  && cappedFocus.zoom === 0.5,
  JSON.stringify([twelvePixelFocus, alreadyReadableFocus, cappedFocus]));

const fittingWorktreeFocus = t.topologyFocusViewport({
  viewport: { zoom: 0.25, pan: { x: 0, y: 0 } },
  subject: {
    kind: "container",
    modelBounds: { x1: -40, y1: -20, x2: 340, y2: 180 },
    modelLabelPx: 12,
  },
  visibleRect: { x1: 0, y1: 0, x2: 300, y2: 200 },
  minZoom: 0.08,
  maxZoom: 2.6,
});
const fittingTabFocus = t.topologyFocusViewport({
  viewport: { zoom: 0.25, pan: { x: 0, y: 0 } },
  subject: {
    kind: "container",
    modelBounds: { x1: 20, y1: -30, x2: 220, y2: 220 },
    modelLabelPx: 12,
  },
  visibleRect: { x1: 0, y1: 0, x2: 300, y2: 200 },
  minZoom: 0.08,
  maxZoom: 2.6,
});
const impossibleFitFocus = t.topologyFocusViewport({
  viewport: { zoom: 0.25, pan: { x: 0, y: 0 } },
  subject: {
    kind: "container",
    modelBounds: { x1: -1000, y1: -500, x2: 1000, y2: 500 },
    modelLabelPx: 12,
  },
  visibleRect: { x1: 10, y1: 20, x2: 110, y2: 100 },
  minZoom: 0.08,
  maxZoom: 2.6,
});
check("container focus keeps readable fitting branches and centers a legal minimum zoom",
  fittingWorktreeFocus.zoom === 0.75
  && fittingWorktreeFocus.pan.x === 37.5
  && fittingWorktreeFocus.pan.y === 40
  && fittingTabFocus.zoom === 0.75
  && fittingTabFocus.pan.x === 60
  && fittingTabFocus.pan.y === 28.75
  && impossibleFitFocus.zoom === 0.08
  && impossibleFitFocus.pan.x === 60
  && impossibleFitFocus.pan.y === 60,
  JSON.stringify({ fittingWorktreeFocus, fittingTabFocus, impossibleFitFocus }));

const rebasedCapture = t.topologyRebaseViewportCapture({
  size: { width: 356, height: 440 },
  viewport: focusViewport,
}, { width: 390, height: 500 });
check("viewport capture rebases its pan with canvas size",
  rebasedCapture.size.width === 390
  && rebasedCapture.size.height === 500
  && rebasedCapture.viewport.zoom === 0.72
  && rebasedCapture.viewport.pan.x === -90
  && rebasedCapture.viewport.pan.y === 37,
  JSON.stringify(rebasedCapture));

const rebasedTwice = t.topologyRebaseViewportCapture(
  t.topologyRebaseViewportCapture({
    size: { width: 356, height: 440 },
    viewport: focusViewport,
  }, { width: 370, height: 470 }),
  { width: 390, height: 500 },
);
check("viewport capture rebasing is path independent",
  JSON.stringify(rebasedTwice) === JSON.stringify(rebasedCapture),
  `${JSON.stringify(rebasedTwice)} vs ${JSON.stringify(rebasedCapture)}`);

const zoomViewport = t.topologyZoomViewport;
check("toolbar zoom viewport helper is available",
  typeof zoomViewport === "function", String(zoomViewport));
if (typeof zoomViewport === "function") {
  const zoomNodes = [{
    id: "project",
    kind: "project",
    modelPosition: { x: 50, y: 40 },
    renderedPosition: { x: 170, y: 210 },
  }, {
    id: "pane-near",
    kind: "pane",
    modelPosition: { x: 10, y: 20 },
    renderedPosition: { x: 140, y: 205 },
  }, {
    id: "pane-far",
    kind: "pane",
    modelPosition: { x: 100, y: 20 },
    renderedPosition: { x: 300, y: 205 },
  }];
  const zoomInput = (nodes, selectedNodeId, fallbackViewport = {
    zoom: 2.6,
    pan: { x: -500, y: -10 },
  }) => ({
    nodes,
    selectedNodeId,
    viewportCenter: { x: 178, y: 220 },
    fallbackViewport,
  });
  const inputBefore = JSON.stringify(zoomNodes);
  const selectedZoom = zoomViewport(zoomInput(zoomNodes, "pane-far"));
  const nearestPaneZoom = zoomViewport(zoomInput(zoomNodes, null));
  const noPaneZoom = zoomViewport(zoomInput([zoomNodes[0]], null));
  check("toolbar zoom centers the selected node",
    selectedZoom.zoom === 2.6
    && selectedZoom.pan.x === -82
    && selectedZoom.pan.y === 168,
    JSON.stringify(selectedZoom));
  check("toolbar zoom centers the nearest pane without a selection",
    nearestPaneZoom.zoom === 2.6
    && nearestPaneZoom.pan.x === 152
    && nearestPaneZoom.pan.y === 168,
    JSON.stringify(nearestPaneZoom));
  check("toolbar zoom falls back to the nearest valid node when no pane exists",
    noPaneZoom.zoom === 2.6
    && noPaneZoom.pan.x === 48
    && noPaneZoom.pan.y === 116,
    JSON.stringify(noPaneZoom));
  const fallback = { zoom: 1.2, pan: { x: 8, y: 9 } };
  const malformedZoom = zoomViewport(zoomInput([
    { id: "broken", kind: "pane", modelPosition: { x: NaN, y: 2 }, renderedPosition: null },
  ], null, fallback));
  const invalidZoom = zoomViewport(zoomInput([], null, { zoom: NaN, pan: { x: 0, y: 0 } }));
  check("toolbar zoom rejects malformed points and invalid fallback cameras",
    JSON.stringify(malformedZoom) === JSON.stringify(fallback)
    && invalidZoom === null,
    JSON.stringify({ malformedZoom, invalidZoom }));
  check("toolbar zoom leaves normalized node input immutable",
    JSON.stringify(zoomNodes) === inputBefore,
    JSON.stringify(zoomNodes));
}

const motionDuration = t.topologyViewportMotionDuration;
check("viewport motion duration helper is available",
  typeof motionDuration === "function", String(motionDuration));
if (typeof motionDuration === "function") {
  const motionInput = (currentViewport, targetViewport, viewportSize) => ({
    currentViewport,
    targetViewport,
    viewportSize,
  });
  const smallMotion = motionDuration(motionInput(
    { zoom: 0.75, pan: { x: 134.5, y: 104.4100341796875 } },
    { zoom: 0.75, pan: { x: 84.25, y: 162.5350341796875 } },
    { width: 356, height: 440 },
  ));
  const largeMotion = motionDuration(motionInput(
    {
      zoom: 0.2509448223733938,
      pan: { x: 127.81103552532124, y: 197.54043839758126 },
    },
    { zoom: 0.72, pan: { x: 88, y: 163.70503417968752 } },
    { width: 356, height: 440 },
  ));
  check("viewport motion duration reproduces the frozen browser fixtures",
    Math.abs(smallMotion - 190.18160036503215) < 1e-12
    && largeMotion === 240,
    JSON.stringify({ smallMotion, largeMotion }));

  const stillMotion = motionDuration(motionInput(
    { zoom: 1, pan: { x: 20, y: 30 } },
    { zoom: 1, pan: { x: 20, y: 30 } },
    { width: 300, height: 400 },
  ));
  const clampedMotion = motionDuration(motionInput(
    { zoom: 1, pan: { x: 0, y: 0 } },
    { zoom: 1, pan: { x: 80, y: 0 } },
    { width: 60, height: 80 },
  ));
  check("viewport motion duration preserves the 180 to 240 millisecond bounds",
    stillMotion === 180 && clampedMotion === 240,
    JSON.stringify({ stillMotion, clampedMotion }));

  const zoomOut = motionDuration(motionInput(
    { zoom: 1, pan: { x: 0, y: 0 } },
    { zoom: 0.5, pan: { x: 0, y: 0 } },
    { width: 300, height: 400 },
  ));
  const zoomIn = motionDuration(motionInput(
    { zoom: 0.5, pan: { x: 0, y: 0 } },
    { zoom: 1, pan: { x: 0, y: 0 } },
    { width: 300, height: 400 },
  ));
  check("viewport motion duration is symmetric for equal zoom ratios",
    Math.abs(zoomOut - zoomIn) < 1e-12,
    JSON.stringify({ zoomOut, zoomIn }));

  const invalidMotions = [
    motionDuration(),
    motionDuration(motionInput(
      { zoom: 0, pan: { x: 0, y: 0 } },
      { zoom: 1, pan: { x: 0, y: 0 } },
      { width: 300, height: 400 },
    )),
    motionDuration(motionInput(
      { zoom: 1, pan: { x: 0, y: 0 } },
      { zoom: 1, pan: { x: 0, y: 0 } },
      { width: 0, height: 400 },
    )),
    motionDuration(motionInput(
      { zoom: "1", pan: { x: 0, y: 0 } },
      { zoom: 1, pan: { x: 0, y: 0 } },
      { width: 300, height: 400 },
    )),
    motionDuration(motionInput(
      { zoom: 1, pan: { x: Number.NaN, y: 0 } },
      { zoom: 1, pan: { x: 0, y: 0 } },
      { width: 300, height: 400 },
    )),
  ];
  check("viewport motion duration falls back for invalid numeric domains",
    invalidMotions.every((duration) => duration === 180),
    JSON.stringify(invalidMotions));
}

process.stdout.write(JSON.stringify(results));
"""


@pytest.fixture(scope="module")
def node_bin() -> str:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node binary required for frontend topology tests")
    return node


def _run_checks(node_bin: str) -> list[dict[str, object]]:
    proc = subprocess.run(
        [node_bin, "--input-type=commonjs", "-e", _DRIVER, str(TOPOLOGY_JS)],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert proc.returncode == 0, f"node harness failed:\n{proc.stderr}"
    return cast(list[dict[str, object]], json.loads(proc.stdout))


def test_topology_js_contracts(node_bin: str) -> None:
    results = _run_checks(node_bin)
    failures = [result for result in results if not result["ok"]]
    assert not failures, f"{len(failures)} topology.js checks failed:\n" + "\n".join(
        f"  - {result['name']}: {result['detail']}" for result in failures
    )
    assert len(results) >= 14, f"expected a meaningful check suite, got {len(results)}"


def test_compact_selection_path_label_contracts() -> None:
    topology_style = TOPOLOGY_STYLE_JS.read_text(encoding="utf-8")
    compact_styles = topology_style[
        topology_style.index("...(compact ? [{") : topology_style.index("}] : []),")
    ]
    assert "node[kind = 'worktree'].is-selection-path" in compact_styles
    assert (
        "node[kind = 'project']:selected, node[kind = 'worktree']:selected, "
        "node[kind = 'tab']:selected"
    ) in compact_styles
    assert '"text-halign": "center"' in compact_styles
    assert '"text-margin-x": 0' in compact_styles


def test_selection_path_lifecycle_contracts() -> None:
    dashboard = DASHBOARD_JS.read_text(encoding="utf-8")
    selection = dashboard[
        dashboard.index("function selectTopologyNode") : dashboard.index(
            "function revealTopologyNode"
        )
    ]
    remove_path = 'topologyCanvas.nodes(".is-selection-path").removeClass("is-selection-path");'
    add_nearest_worktree = (
        'node.parents(\'[kind = "worktree"]\').first().addClass("is-selection-path");'
    )
    focus = "focusTopologyNode(node, { selectionChanged, animate: motionAllowed() });"
    assert remove_path in selection
    assert add_nearest_worktree in selection
    assert selection.index(remove_path) < selection.index(add_nearest_worktree)
    assert selection.index(add_nearest_worktree) < selection.index(focus)

    clear = dashboard[
        dashboard.index("function clearTopologySelection") : dashboard.index(
            "function handleTopologyResize"
        )
    ]
    clear_path = (
        'if (topologyCanvas) topologyCanvas.nodes(".is-selection-path")'
        '.removeClass("is-selection-path");'
    )
    assert clear_path in clear
    assert clear.index(clear_path) < clear.index(
        'if (!hadSelection && phase.kind !== "focused" && reason === "user-clear")'
    )


def test_dashboard_static_accessibility_and_overflow_contracts() -> None:
    html = INDEX_HTML.read_text(encoding="utf-8")
    css = DASHBOARD_CSS.read_text(encoding="utf-8")
    javascript = DASHBOARD_JS.read_text(encoding="utf-8")

    attention = re.search(r'<div id="attention-list"[^>]*>', html)
    assert attention is not None
    assert 'role="region"' in attention.group(0)
    assert 'tabindex="0"' in attention.group(0)
    assert 'aria-busy="true"' in attention.group(0)
    assert 'aria-describedby="topology-a11y"' in html
    assert 'id="topology-count" class="quiet-badge" data-compact="0 nodes"' in html
    assert "Loading alerts…" in html
    assert "Loading lifecycle…" in html
    assert html.count('aria-busy="true"') >= 4

    assert re.search(r'<section class="kanban-column"\s+tabindex="0"', javascript)
    assert re.search(
        r"\.attention-item span\s*\{[^}]*overflow-wrap:\s*anywhere;",
        css,
        re.DOTALL,
    )
    assert re.search(
        r"\.job-detail dt\s*\{[^}]*color:\s*var\(--muted\);",
        css,
        re.DOTALL,
    )
    assert ".attention-list:focus-visible" in css
    assert re.search(
        r"\.panel-heading > \.quiet-badge\s*\{[^}]*min-width:\s*0;",
        css,
        re.DOTALL,
    )
    assert re.search(
        r"@media \(max-width: 760px\).*?\.panel-heading\s*\{[^}]*flex-wrap:\s*wrap;",
        css,
        re.DOTALL,
    )
    assert "function showUnavailableState" in javascript
    assert "aria-busy" in javascript
    assert "ArrowRight" in javascript
    focus_adapter = javascript[
        javascript.index("function readTopologyFocusInput") : javascript.index(
            "function readTopologyContentState"
        )
    ]
    assert "function readTopologyLeafFocusContext(node)" in focus_adapter
    assert "readTopologyLeafFocusContext(node)" in focus_adapter
    assert "parents('[kind = \"worktree\"]')" in focus_adapter
    assert "includeNodes: true" in focus_adapter
    assert "includeLabels: true" in focus_adapter
    assert "includeNodes: false" in focus_adapter
    assert "leafBounds:" in focus_adapter
    assert "contextBounds" in focus_adapter
    assert "...(context ? { context } : {})" in focus_adapter
    topology_source = TOPOLOGY_JS.read_text(encoding="utf-8")
    focus_calculator = topology_source[
        topology_source.index("function topologyFocusViewport") : topology_source.index(
            "function topologyRebaseViewportCapture"
        )
    ]
    assert 'subject.kind === "leaf" && subject.context' in focus_calculator
    assert "leafBounds" in focus_calculator
    assert "contextBounds" in focus_calculator
    assert "fitCeiling >= readableTarget" in focus_calculator
    assert "<form" not in html.lower()
    assert not re.search(r"\b(?:post|put|patch|delete)\b", javascript, re.IGNORECASE)
