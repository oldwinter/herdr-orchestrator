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
import shutil
import subprocess
from importlib.resources import files
from typing import cast

import pytest

TOPOLOGY_JS = files("herdr_orchestrator.dashboard.static").joinpath("topology.js")

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
    + "topologyPresetPositions, topologyId };",
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
