"use strict";

let topologyTouchOwnershipState = {
  coarseOwner: "page",
};
let topologyCanvas = null;
let topologyContentSignature = "";
let topologyStructureSignature = "";
let topologyHasRendered = false;
let topologyTreeSignature = null;
let topologyCompact = null;
let topologyLayout = null;
let topologyLayoutGeneration = 0;
let topologyViewportState = {
  overviewMode: "auto",
  containerSize: null,
  focus: { kind: "idle" },
  motion: { generation: 0, active: null },
  programmaticWriteDepth: 0,
};
let topologyNavigationState = {
  orderedNodeIds: [],
  selectedNodeId: null,
  selectionOrigin: "none",
};
const topologyViewportControlMessages = Object.freeze({
  fitUnavailable: "No topology to fit.",
  zoomInBoundary: "Maximum zoom reached.",
  zoomOutBoundary: "Minimum zoom reached.",
});

function currentTopologyTouchMode() {
  const coarsePointer = primaryCoarsePointer?.matches === true;
  return {
    coarsePointer,
    owner: coarsePointer ? topologyTouchOwnershipState.coarseOwner : "graph",
  };
}

function setTopologyTouchOwner(owner) {
  topologyTouchOwnershipState = { coarseOwner: owner };
  return syncTopologyTouchOwnership();
}

function syncTopologyTouchOwnership() {
  const mode = currentTopologyTouchMode();
  const graphOwnsTouch = mode.owner === "graph";
  const topology = byId("topology");
  const toggle = byId("topology-touch-owner");

  if (topologyCanvas) {
    topologyCanvas.userPanningEnabled(graphOwnsTouch);
    topologyCanvas.userZoomingEnabled(graphOwnsTouch);
  }

  topology.dataset.touchOwner = mode.owner;
  toggle.hidden = !mode.coarsePointer;
  toggle.setAttribute("aria-pressed", String(graphOwnsTouch));
  const label = graphOwnsTouch
    ? "Disable touch navigation"
    : "Enable touch navigation";
  toggle.setAttribute("aria-label", label);
  toggle.title = label;
  return mode;
}

function renderTopology(topology, workflow) {
  const projects = normalizedProjects(topology, workflow);
  const graph = topologyGraph(projects);
  const navigationOrder = topologyNavigationOrder(graph);
  const previousSelectedId = topologyNavigationState.selectedNodeId;
  const selectionSurvives = reconcileTopologyNavigation(navigationOrder);
  if (previousSelectedId && !selectionSurvives) {
    clearTopologySelection({ reason: "structure-removed", announce: false });
  }
  const counts = graph.counts;
  const countParts = [
    `${counts.projects} project${counts.projects === 1 ? "" : "s"}`,
    `${counts.worktrees} worktree${counts.worktrees === 1 ? "" : "s"}`,
    `${counts.tabs} tab${counts.tabs === 1 ? "" : "s"}`,
    `${counts.panes} pane${counts.panes === 1 ? "" : "s"}`,
  ];
  const topologyCount = byId("topology-count");
  topologyCount.textContent = countParts.join(" · ");
  topologyCount.dataset.compact = `${graph.elements.length} nodes`;
  topologyCount.title = countParts.join(" · ");
  byId("topology").setAttribute(
    "aria-label",
    `Interactive Herdr topology (read-only): ${countParts.join(", ")}`,
  );
  byId("topology").dataset.nodeCount = String(graph.elements.length);
  renderTopologyTree(projects, graph.contentSignature, navigationOrder);

  const empty = byId("topology-empty");
  if (!graph.elements.length) {
    empty.textContent = "No matching Herdr topology";
    empty.classList.remove("is-hidden");
    byId("topology").classList.remove("is-reflowing");
    topologyLayoutGeneration += 1;
    topologyLayout?.stop();
    topologyLayout = null;
    if (topologyCanvas) topologyCanvas.elements().remove();
    syncTopologyZoomControls();
    syncTopologyFitControl();
    topologyContentSignature = "";
    topologyStructureSignature = "";
    topologyHasRendered = false;
    clearTopologySelection({ reason: "structure-removed", announce: false });
    return;
  }

  const canvas = ensureTopologyCanvas();
  if (!canvas) {
    empty.textContent = "Topology renderer unavailable";
    empty.classList.remove("is-hidden");
    syncTopologyFitControl();
    return;
  }
  empty.classList.add("is-hidden");

  if (graph.contentSignature === topologyContentSignature) return;

  const structureChanged = graph.structureSignature !== topologyStructureSignature;
  if (structureChanged) invalidateTopologyFocusForStructure();
  const selectedId = topologyNavigationState.selectedNodeId;
  const incomingIds = new Set(graph.elements.map((element) => element.data.id));

  canvas.batch(() => {
    canvas.elements().filter((element) => !incomingIds.has(element.id())).remove();
    graph.elements.forEach((element) => {
      const current = canvas.getElementById(element.data.id);
      if (current.empty()) {
        canvas.add(element);
      } else {
        current.data(element.data);
        current.classes(element.classes);
      }
    });
  });
  syncTopologyZoomControls();
  syncTopologyFitControl();

  topologyContentSignature = graph.contentSignature;
  topologyStructureSignature = graph.structureSignature;

  if (structureChanged) {
    const shouldFit = !topologyHasRendered || topologyViewportState.overviewMode === "auto";
    const layoutGeneration = ++topologyLayoutGeneration;
    topologyLayout?.stop();
    const topologyElement = byId("topology");
    topologyElement.classList.add("is-reflowing");
    const layout = canvas.layout({
      name: "preset",
      positions: (node) => graph.positions[node.id()] || node.position(),
      fit: false,
      animate: motionAllowed(),
      animationDuration: 360,
      animationEasing: "ease-out-cubic",
    });
    layout.one("layoutstop", () => {
      if (layoutGeneration !== topologyLayoutGeneration) return;
      topologyLayout = null;
      topologyElement.classList.remove("is-reflowing");
      withProgrammaticViewportWrite(() => canvas.resize());
      recordTopologyViewportSize();
      const selectedNode = topologyNavigationState.selectedNodeId
        ? canvas.getElementById(topologyNavigationState.selectedNodeId)
        : null;
      if (
        topologyViewportState.focus.kind === "focused"
        && selectedNode
        && !selectedNode.empty()
        && isTopologyCompact()
      ) {
        focusTopologyNode(selectedNode, { selectionChanged: false, animate: false });
      } else if (topologyViewportState.focus.kind === "restoring") {
        return;
      } else if (shouldFit && topologyViewportState.overviewMode === "auto") {
        fitTopology();
      }
    });
    topologyLayout = layout;
    if (motionAllowed()) {
      requestAnimationFrame(() => {
        if (layoutGeneration === topologyLayoutGeneration) layout.run();
      });
    } else {
      layout.run();
    }
    topologyHasRendered = true;
  }

  if (selectedId && !canvas.getElementById(selectedId).empty()) {
    selectTopologyNode(selectedId, {
      origin: "restore",
      viewport: "preserve",
      announce: false,
    });
  } else {
    clearTopologySelection({ reason: "user-clear", announce: false });
  }
}

function ensureTopologyCanvas() {
  if (topologyCanvas) return topologyCanvas;
  if (typeof cytoscape !== "function") return null;

  topologyCompact = isTopologyCompact();
  recordTopologyViewportSize();
  topologyCanvas = cytoscape({
    container: byId("topology"),
    elements: [],
    autoungrabify: true,
    boxSelectionEnabled: false,
    minZoom: 0.08,
    maxZoom: 2.6,
    style: topologyStyles({ compact: topologyCompact, animate: motionAllowed() }),
  });
  syncTopologyTouchOwnership();
  syncTopologyZoomControls();
  syncTopologyFitControl();

  topologyCanvas.on("viewport", () => {
    syncTopologyZoomControls();
    if (
      topologyViewportState.programmaticWriteDepth > 0
      || topologyViewportState.motion.active
    ) return;
    claimTopologyViewport();
  });
  ["dragpan", "scrollzoom", "pinchzoom"].forEach((eventName) => {
    topologyCanvas.on(eventName, () => {
      if (topologyViewportState.programmaticWriteDepth === 0) {
        claimTopologyViewport();
      }
    });
  });
  topologyCanvas.on("tap", "node", (event) => {
    selectTopologyNode(event.target.id(), {
      origin: "pointer",
      viewport: "selection",
      announce: true,
    });
  });
  topologyCanvas.on("mouseover", "node", (event) => {
    event.target.addClass("is-hovered");
  });
  topologyCanvas.on("mouseout", "node", (event) => {
    event.target.removeClass("is-hovered");
  });
  topologyCanvas.on("tap", (event) => {
    if (event.target === topologyCanvas) clearTopologySelection({ announce: false });
  });

  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(handleTopologyResize);
    observer.observe(byId("topology"));
  }
  byId("topology").dataset.renderer = "cytoscape-canvas";
  return topologyCanvas;
}

function isTopologyCompact() {
  return byId("topology").clientWidth < 600;
}

function readTopologyViewportSize() {
  const container = byId("topology");
  const rect = typeof container.getBoundingClientRect === "function"
    ? container.getBoundingClientRect()
    : { width: 0, height: 0 };
  const width = Number(container.clientWidth) || Number(rect.width);
  const height = Number(container.clientHeight) || Number(rect.height);
  if (!Number.isFinite(width) || !Number.isFinite(height)) return null;
  return { width, height };
}

function recordTopologyViewportSize() {
  const size = readTopologyViewportSize();
  if (!size || size.width <= 0 || size.height <= 0) return topologyViewportState.containerSize;
  topologyViewportState.containerSize = size;
  return size;
}

function withProgrammaticViewportWrite(callback) {
  topologyViewportState.programmaticWriteDepth += 1;
  try {
    return callback();
  } finally {
    topologyViewportState.programmaticWriteDepth -= 1;
  }
}

function captureTopologyViewport() {
  if (!topologyCanvas) return null;
  const size = recordTopologyViewportSize();
  const pan = topologyCanvas.pan();
  const zoom = Number(topologyCanvas.zoom());
  if (!size || !Number.isFinite(zoom) || !Number.isFinite(pan.x) || !Number.isFinite(pan.y)) {
    return null;
  }
  return {
    size: { width: size.width, height: size.height },
    viewport: { zoom, pan: { x: pan.x, y: pan.y } },
  };
}

function topologySelectionVisibleRect() {
  const container = byId("topology");
  const containerRect = container.getBoundingClientRect();
  const size = recordTopologyViewportSize() || {
    width: containerRect.width,
    height: containerRect.height,
  };
  const inspector = byId("topology-inspector");
  const inspectorVisible = inspector
    && !inspector.classList.contains("is-hidden")
    && typeof inspector.getBoundingClientRect === "function";
  const inspectorRect = inspectorVisible ? inspector.getBoundingClientRect() : null;
  const inspectorTop = inspectorRect && inspectorRect.height > 0
    ? inspectorRect.top - containerRect.top
    : size.height;
  const bottom = inspectorRect && inspectorRect.height > 0
    ? Math.min(size.height - 18, inspectorTop - 12)
    : size.height - 18;
  return {
    x1: 18,
    y1: 18,
    x2: Math.max(18, size.width - 18),
    y2: Math.max(18, bottom),
  };
}

function readTopologyFocusInput(node, capture = captureTopologyViewport()) {
  if (!topologyCanvas || !node || typeof node.isParent !== "function") return null;
  const modelLabelPx = Number.parseFloat(String(node.style("font-size")));
  const minZoom = Number(topologyCanvas.minZoom());
  const maxZoom = Number(topologyCanvas.maxZoom());
  const visibleRect = topologySelectionVisibleRect();
  if (
    !capture
    || !Number.isFinite(capture.viewport?.zoom)
    || !Number.isFinite(modelLabelPx)
    || modelLabelPx <= 0
    || !Number.isFinite(minZoom)
    || !Number.isFinite(maxZoom)
    || minZoom > maxZoom
    || ![visibleRect.x1, visibleRect.y1, visibleRect.x2, visibleRect.y2]
      .every(Number.isFinite)
    || visibleRect.x2 <= visibleRect.x1
    || visibleRect.y2 <= visibleRect.y1
  ) return null;

  let subject;
  if (node.isParent()) {
    if (typeof node.boundingBox !== "function" || typeof node.descendants !== "function") {
      return null;
    }
    const selectedBounds = node.boundingBox({
      includeNodes: true,
      includeLabels: false,
      includeOverlays: true,
      includeUnderlays: true,
    });
    const descendants = node.descendants();
    if (!descendants || typeof descendants.boundingBox !== "function") return null;
    const descendantBounds = descendants.boundingBox({
      includeNodes: true,
      includeLabels: true,
      includeOverlays: false,
      includeUnderlays: false,
    });
    if (
      ![
        selectedBounds.x1,
        selectedBounds.y1,
        selectedBounds.x2,
        selectedBounds.y2,
        descendantBounds.x1,
        descendantBounds.y1,
        descendantBounds.x2,
        descendantBounds.y2,
      ].every(Number.isFinite)
    ) return null;
    const modelBounds = {
      x1: Math.min(selectedBounds.x1, descendantBounds.x1),
      y1: Math.min(selectedBounds.y1, descendantBounds.y1),
      x2: Math.max(selectedBounds.x2, descendantBounds.x2),
      y2: Math.max(selectedBounds.y2, descendantBounds.y2),
    };
    if (modelBounds.x2 <= modelBounds.x1 || modelBounds.y2 <= modelBounds.y1) return null;
    subject = { kind: "container", modelBounds, modelLabelPx };
  } else {
    if (typeof node.position !== "function") return null;
    const position = node.position();
    if (!Number.isFinite(position.x) || !Number.isFinite(position.y)) return null;
    const context = readTopologyLeafFocusContext(node);
    subject = {
      kind: "leaf",
      modelCenter: { x: position.x, y: position.y },
      modelLabelPx,
      ...(context ? { context } : {}),
    };
  }

  return {
    viewport: capture.viewport,
    subject,
    visibleRect,
    minZoom,
    maxZoom,
  };
}

function readTopologyLeafFocusContext(node) {
  if (typeof node.boundingBox !== "function" || typeof node.parents !== "function") return null;
  const worktree = node.parents('[kind = "worktree"]').first();
  if (!worktree || worktree.empty() || typeof worktree.boundingBox !== "function") return null;
  const options = { includeOverlays: false, includeUnderlays: false };
  const leafBounds = node.boundingBox({ ...options, includeNodes: true, includeLabels: true });
  const worktreeLabelBounds = worktree.boundingBox({ ...options, includeNodes: false, includeLabels: true });
  const bounds = [leafBounds.x1, leafBounds.y1, leafBounds.x2, leafBounds.y2, worktreeLabelBounds.x1, worktreeLabelBounds.y1, worktreeLabelBounds.x2, worktreeLabelBounds.y2];
  if (!bounds.every(Number.isFinite) || leafBounds.x2 <= leafBounds.x1 || leafBounds.y2 <= leafBounds.y1 || worktreeLabelBounds.x2 <= worktreeLabelBounds.x1 || worktreeLabelBounds.y2 <= worktreeLabelBounds.y1) return null;
  return { leafBounds: { x1: leafBounds.x1, y1: leafBounds.y1, x2: leafBounds.x2, y2: leafBounds.y2 }, contextBounds: { x1: Math.min(leafBounds.x1, worktreeLabelBounds.x1), y1: Math.min(leafBounds.y1, worktreeLabelBounds.y1), x2: Math.max(leafBounds.x2, worktreeLabelBounds.x2), y2: Math.max(leafBounds.y2, worktreeLabelBounds.y2) } };
}
function readTopologyContentState() {
  if (!topologyCanvas) return { kind: "unavailable" };
  const elements = topologyCanvas.elements();
  if (elements.empty()) return { kind: "unavailable" };
  return { kind: "ready", elements };
}
function syncTopologyFitControl() {
  const state = readTopologyContentState();
  const fit = byId("topology-fit");
  const disabled = state.kind !== "ready";
  if (fit.disabled !== disabled) fit.disabled = disabled;
  if (!disabled) setTopologyFitUnavailableStatus(false);
}
function setTopologyFitUnavailableStatus(show = false) {
  const status = byId("topology-selection-status");
  if (show) {
    status.replaceChildren(
      document.createTextNode(topologyViewportControlMessages.fitUnavailable),
    );
    return;
  }
  if (status.textContent === topologyViewportControlMessages.fitUnavailable) {
    status.replaceChildren();
  }
}
function readTopologyZoomState() {
  const content = readTopologyContentState();
  if (content.kind !== "ready") {
    return { kind: "unavailable", canZoomIn: false, canZoomOut: false };
  }
  const renderedZoom = Number(topologyCanvas.zoom());
  const minZoom = Number(topologyCanvas.minZoom());
  const maxZoom = Number(topologyCanvas.maxZoom());
  const active = topologyViewportState.motion.active;
  const activeZoomTarget = Number(active?.target?.zoom);
  const commandZoom = active?.purpose === "zoom" && Number.isFinite(activeZoomTarget)
    ? active.target.zoom
    : renderedZoom;
  if (
    ![commandZoom, minZoom, maxZoom].every(Number.isFinite)
    || minZoom > maxZoom
  ) {
    return { kind: "unavailable", canZoomIn: false, canZoomOut: false };
  }
  return {
    kind: "ready",
    commandZoom,
    canZoomIn: commandZoom < maxZoom,
    canZoomOut: commandZoom > minZoom,
  };
}
function setTopologyZoomBoundaryStatus(direction = null) {
  const status = byId("topology-selection-status");
  const message = direction === "in"
    ? topologyViewportControlMessages.zoomInBoundary
    : direction === "out"
      ? topologyViewportControlMessages.zoomOutBoundary
      : "";
  if (message) {
    status.replaceChildren(document.createTextNode(message));
    return;
  }
  if (
    status.textContent === topologyViewportControlMessages.zoomInBoundary
    || status.textContent === topologyViewportControlMessages.zoomOutBoundary
  ) {
    status.replaceChildren();
  }
}
function syncTopologyZoomControls() {
  const state = readTopologyZoomState();
  const zoomOut = byId("topology-zoom-out");
  const zoomIn = byId("topology-zoom-in");
  const disableOut = !state.canZoomOut;
  const disableIn = !state.canZoomIn;
  if (zoomOut.disabled !== disableOut) zoomOut.disabled = disableOut;
  if (zoomIn.disabled !== disableIn) zoomIn.disabled = disableIn;
  if (
    state.kind !== "ready"
    || (state.canZoomIn && statusMatchesTopologyZoomBoundary("in"))
    || (state.canZoomOut && statusMatchesTopologyZoomBoundary("out"))
  ) {
    setTopologyZoomBoundaryStatus();
  }
}
function statusMatchesTopologyZoomBoundary(direction) {
  const message = direction === "in"
    ? topologyViewportControlMessages.zoomInBoundary
    : topologyViewportControlMessages.zoomOutBoundary;
  return byId("topology-selection-status").textContent === message;
}
function setTopologyViewport(
  viewport,
  { animate = false, purpose = "programmatic", onStart, onComplete } = {},
) {
  if (!topologyCanvas) return null;
  const next = {
    zoom: Number(viewport.zoom),
    pan: { x: Number(viewport.pan.x), y: Number(viewport.pan.y) },
  };
  if (
    !Number.isFinite(next.zoom)
    || !Number.isFinite(next.pan.x)
    || !Number.isFinite(next.pan.y)
  ) return null;
  stopTopologyViewportMotion();
  if (!animate || !motionAllowed() || typeof topologyCanvas.animation !== "function") {
    withProgrammaticViewportWrite(() => {
      if (typeof topologyCanvas.viewport === "function") {
        topologyCanvas.viewport(next);
      } else {
        topologyCanvas.zoom(next.zoom);
        topologyCanvas.pan(next.pan);
      }
    });
    syncTopologyZoomControls();
    if (typeof onComplete === "function") onComplete();
    return null;
  }

  const capture = captureTopologyViewport();
  const duration = topologyViewportMotionDuration({
    currentViewport: capture?.viewport, targetViewport: next, viewportSize: capture?.size,
  });
  const generation = ++topologyViewportState.motion.generation;
  const handle = topologyCanvas.animation(
    { zoom: next.zoom, pan: next.pan },
    { duration, easing: "ease-out-cubic", queue: false },
  );
  topologyViewportState.motion.active = { generation, handle, purpose, target: next };
  syncTopologyZoomControls();
  if (typeof onStart === "function") onStart(generation);

  const settle = () => {
    if (topologyViewportState.motion.active?.generation !== generation) return;
    topologyViewportState.motion.active = null;
    syncTopologyZoomControls();
    if (typeof onComplete === "function") onComplete(generation);
  };
  const completion = typeof handle.promise === "function"
    ? handle.promise("complete")
    : null;
  if (completion && typeof completion.then === "function") {
    completion.then(() => {
      if (typeof requestAnimationFrame === "function") requestAnimationFrame(settle);
      else settle();
    });
  }
  handle.play();
  return generation;
}
function stopTopologyViewportMotion() {
  const active = topologyViewportState.motion.active;
  topologyViewportState.motion.generation += 1;
  topologyViewportState.motion.active = null;
  if (active?.handle && typeof active.handle.stop === "function") {
    withProgrammaticViewportWrite(() => active.handle.stop());
  }
  syncTopologyZoomControls();
}
function claimTopologyViewport({ overviewMode = "user" } = {}) {
  stopTopologyViewportMotion();
  topologyViewportState.focus = { kind: "idle" };
  topologyViewportState.overviewMode = overviewMode;
}
function invalidateTopologyFocusForStructure() {
  stopTopologyViewportMotion();
  topologyViewportState.focus = { kind: "idle" };
}
function fitTopology({ user = false, animate = false, origin = "programmatic" } = {}) {
  const content = readTopologyContentState();
  if (content.kind !== "ready") {
    if (origin === "canvas-keyboard") setTopologyFitUnavailableStatus(true);
    return false;
  }
  setTopologyFitUnavailableStatus(false);
  const container = byId("topology");
  const fitPadding = isTopologyCompact() ? 12 : 60;
  recordTopologyViewportSize();
  const target = topologyCanvas.getFitViewport(content.elements, fitPadding);
  if (!target) return false;
  const current = captureTopologyViewport()?.viewport;
  const cameraChanges = !current
    || current.zoom !== target.zoom
    || current.pan.x !== target.pan.x
    || current.pan.y !== target.pan.y;
  if (user) claimTopologyViewport({ overviewMode: "auto" });
  container.dataset.fitPadding = String(fitPadding);
  setTopologyViewport(target, { animate: animate && cameraChanges, purpose: "fit" });
  return true;
}
function zoomTopology(direction, { origin = "toolbar" } = {}) {
  const state = readTopologyZoomState();
  if (state.kind !== "ready") return false;
  const canZoom = direction === "in" ? state.canZoomIn : state.canZoomOut;
  if (!canZoom) {
    if (origin === "canvas-keyboard") setTopologyZoomBoundaryStatus(direction);
    return false;
  }
  const factor = direction === "in" ? 1.22 : 0.82;
  const bounds = byId("topology").getBoundingClientRect();
  const target = topologyZoomViewport({
    nodes: topologyCanvas.nodes().map((node) => ({ id: node.id(), kind: node.data("kind"), modelPosition: node.position(), renderedPosition: node.renderedPosition() })),
    selectedNodeId: topologyNavigationState.selectedNodeId,
    viewportCenter: { x: bounds.width / 2, y: bounds.height / 2 },
    fallbackViewport: topologyCanvas.getZoomedViewport({ level: state.commandZoom * factor, renderedPosition: { x: bounds.width / 2, y: bounds.height / 2 } }),
  });
  if (!target) return false;
  setTopologyZoomBoundaryStatus();
  claimTopologyViewport();
  setTopologyViewport(target, { animate: true, purpose: "zoom" });
  return true;
}
function panTopology(dx, dy) {
  if (readTopologyContentState().kind !== "ready") return false;
  const active = topologyViewportState.motion.active;
  const current = active?.purpose === "pan"
    ? active.target
    : captureTopologyViewport()?.viewport;
  if (!current) return false;
  claimTopologyViewport();
  setTopologyViewport(
    {
      zoom: current.zoom,
      pan: { x: current.pan.x + dx, y: current.pan.y + dy },
    },
    { animate: true, purpose: "pan" },
  );
  return true;
}

function topologyA11yId(nodeId) {
  return `topology-a11y-node-${encodeURIComponent(String(nodeId)).replaceAll("%", "_")}`;
}

function reconcileTopologyNavigation(orderedNodeIds) {
  const selectedNodeId = topologyNavigationState.selectedNodeId;
  const selectionSurvives = !selectedNodeId || orderedNodeIds.includes(selectedNodeId);
  topologyNavigationState = {
    orderedNodeIds: [...orderedNodeIds],
    selectedNodeId: selectionSurvives ? selectedNodeId : null,
    selectionOrigin: selectionSurvives ? topologyNavigationState.selectionOrigin : "none",
  };
  return selectionSurvives;
}

function updateTopologySelectionState(node, { announce = false } = {}) {
  const topology = byId("topology");
  const kind = node.data("kindLabel") || "Node";
  const label = node.data("label") || node.id();
  const detail = node.data("detail") || node.data("identity") || "";
  const activeDescendant = byId("topology-active-descendant");
  if (activeDescendant) {
    activeDescendant.textContent = `${kind}: ${label}. ${detail}`;
    topology.setAttribute("aria-activedescendant", activeDescendant.id);
  }
  document.querySelectorAll("[data-topology-node-id]").forEach((target) => {
    if (target.dataset.topologyNodeId === node.id()) {
      target.setAttribute("aria-current", "true");
    } else {
      target.removeAttribute("aria-current");
    }
  });
  const status = byId("topology-selection-status");
  if (status) {
    status.textContent = announce ? `Selected ${kind}: ${label}. ${detail}` : "";
  }
}

function focusTopologyNode(node, { selectionChanged, animate = true } = {}) {
  if (topologyViewportState.focus.kind === "idle" && !selectionChanged) return;
  if (topologyViewportState.focus.kind === "restoring") {
    stopTopologyViewportMotion();
    topologyViewportState.focus = { kind: "idle" };
  }
  const capture = captureTopologyViewport();
  const input = readTopologyFocusInput(node, capture);
  if (!input) {
    revealTopologyNode(node);
    return;
  }

  if (topologyViewportState.focus.kind === "idle") {
    if (!selectionChanged) return;
    const baseline = capture;
    if (!baseline) {
      revealTopologyNode(node);
      return;
    }
    topologyViewportState.focus = { kind: "focused", baseline };
  }

  const target = topologyFocusViewport(input);
  setTopologyViewport(target, { animate, purpose: "focus" });
}
function selectTopologyNode(
  nodeId,
  { origin = "keyboard", viewport = "selection", announce = true } = {},
) {
  if (!topologyCanvas) return false;
  const node = topologyCanvas.getElementById(String(nodeId));
  if (node.empty()) return false;
  const selectionChanged = topologyNavigationState.selectedNodeId !== node.id();
  topologyCanvas.nodes(".is-selection-path").removeClass("is-selection-path");
  topologyCanvas.nodes().unselect();
  node.select();
  node.parents('[kind = "worktree"]').first().addClass("is-selection-path");
  topologyNavigationState = {
    ...topologyNavigationState,
    selectedNodeId: node.id(),
    selectionOrigin: origin,
  };
  renderTopologyInspector(node);
  updateTopologySelectionState(node, { announce });
  if (viewport === "selection") {
    if (isTopologyCompact()) {
      focusTopologyNode(node, { selectionChanged, animate: motionAllowed() });
    } else if (origin === "keyboard") {
      revealTopologyNode(node);
    }
  }
  return true;
}
function revealTopologyNode(node) {
  if (!topologyCanvas || typeof node.renderedBoundingBox !== "function") return;
  const container = byId("topology");
  const bounds = node.renderedBoundingBox();
  const padding = container.clientWidth < 600 ? 18 : 28;
  let dx = 0;
  let dy = 0;
  if (bounds.x1 < padding) dx = padding - bounds.x1;
  else if (bounds.x2 > container.clientWidth - padding) {
    dx = container.clientWidth - padding - bounds.x2;
  }
  if (bounds.y1 < padding) dy = padding - bounds.y1;
  else if (bounds.y2 > container.clientHeight - padding) {
    dy = container.clientHeight - padding - bounds.y2;
  }
  if (!dx && !dy) return;
  const current = topologyCanvas.pan();
  const next = { x: current.x + dx, y: current.y + dy };
  setTopologyViewport(
    { zoom: topologyCanvas.zoom(), pan: next },
    { animate: motionAllowed(), purpose: "reveal" },
  );
}

function cycleTopologySelection(direction) {
  const nodeIds = topologyNavigationState.orderedNodeIds;
  if (!nodeIds.length) return false;
  const currentIndex = topologyNavigationState.selectedNodeId
    ? nodeIds.indexOf(topologyNavigationState.selectedNodeId)
    : -1;
  const nextIndex = currentIndex < 0
    ? direction > 0 ? 0 : nodeIds.length - 1
    : (currentIndex + direction + nodeIds.length) % nodeIds.length;
  return selectTopologyNode(nodeIds[nextIndex], {
    origin: "keyboard",
    viewport: "selection",
    announce: true,
  });
}

function renderTopologyInspector(node) {
  const inspector = byId("topology-inspector");
  const kind = node.data("kindLabel") || "Node";
  inspector.innerHTML = `
    <span class="inspector-kind">${escapeHtml(kind)}</span>
    <strong>${escapeHtml(node.data("label") || node.id())}</strong>
    <span title="${escapeHtml(node.data("detail") || "")}">${escapeHtml(node.data("detail") || node.data("identity") || "")}</span>
  `;
  inspector.removeAttribute("aria-hidden");
  inspector.classList.remove("is-hidden");
}
function clearTopologySelection({ reason = "user-clear", announce = false } = {}) {
  const hadSelection = topologyNavigationState.selectedNodeId !== null;
  const phase = topologyViewportState.focus;
  if (topologyCanvas) topologyCanvas.nodes(".is-selection-path").removeClass("is-selection-path");
  if (!hadSelection && phase.kind !== "focused" && reason === "user-clear") return false;

  let restoreTarget = null;
  if (reason === "user-clear" && phase.kind === "focused") {
    restoreTarget = phase.baseline;
    const size = recordTopologyViewportSize();
    if (
      size
      && (size.width !== restoreTarget.size.width || size.height !== restoreTarget.size.height)
    ) {
      restoreTarget = topologyRebaseViewportCapture(restoreTarget, size);
    }
  }

  if (topologyCanvas) topologyCanvas.nodes().unselect();
  topologyNavigationState = {
    ...topologyNavigationState,
    selectedNodeId: null,
    selectionOrigin: "none",
  };
  byId("topology").removeAttribute("aria-activedescendant");
  const activeDescendant = byId("topology-active-descendant");
  if (activeDescendant) activeDescendant.textContent = "";
  document.querySelectorAll("[data-topology-node-id]").forEach((target) => {
    target.removeAttribute("aria-current");
  });
  const status = byId("topology-selection-status");
  if (status) status.textContent = announce ? "Topology selection cleared." : "";
  const inspector = byId("topology-inspector");
  inspector.setAttribute("aria-hidden", "true");
  inspector.classList.add("is-hidden");
  if (reason === "structure-removed") inspector.replaceChildren();

  if (reason === "structure-removed") {
    invalidateTopologyFocusForStructure();
    return hadSelection;
  }
  if (!restoreTarget) {
    stopTopologyViewportMotion();
    topologyViewportState.focus = { kind: "idle" };
    return hadSelection;
  }

  stopTopologyViewportMotion();
  if (!motionAllowed() || typeof topologyCanvas.animation !== "function") {
    topologyViewportState.focus = { kind: "idle" };
    setTopologyViewport(restoreTarget.viewport, { purpose: "restore" });
    return true;
  }

  setTopologyViewport(restoreTarget.viewport, {
    animate: true,
    purpose: "restore",
    onStart: (generation) => {
      topologyViewportState.focus = {
        kind: "restoring",
        target: restoreTarget,
        motionGeneration: generation,
      };
    },
    onComplete: (generation) => {
      if (
        topologyViewportState.focus.kind === "restoring"
        && topologyViewportState.focus.motionGeneration === generation
      ) topologyViewportState.focus = { kind: "idle" };
    },
  });
  return true;
}

function handleTopologyResize() {
  if (!topologyCanvas) return;
  const nextSize = readTopologyViewportSize();
  if (!nextSize || nextSize.width <= 0 || nextSize.height <= 0) {
    stopTopologyViewportMotion();
    return;
  }

  const wasCompact = topologyCompact === true;
  const compact = isTopologyCompact();
  withProgrammaticViewportWrite(() => topologyCanvas.resize());
  if (compact !== topologyCompact) {
    topologyCompact = compact;
    topologyCanvas.style(topologyStyles({ compact, animate: motionAllowed() }));
  }
  topologyViewportState.containerSize = nextSize;

  if (topologyViewportState.focus.kind === "focused") {
    const baseline = topologyRebaseViewportCapture(
      topologyViewportState.focus.baseline,
      nextSize,
    );
    topologyViewportState.focus = { kind: "focused", baseline };
    if (wasCompact && !compact) {
      topologyViewportState.focus = { kind: "idle" };
      if (topologyViewportState.overviewMode === "auto") {
        stopTopologyViewportMotion();
        fitTopology();
      } else {
        setTopologyViewport(baseline.viewport, { purpose: "restore" });
      }
      return;
    }
    const selectedId = topologyNavigationState.selectedNodeId;
    const selectedNode = selectedId ? topologyCanvas.getElementById(selectedId) : null;
    if (selectedNode && !selectedNode.empty() && compact) {
      focusTopologyNode(selectedNode, { selectionChanged: false, animate: false });
    }
    return;
  }

  if (topologyViewportState.focus.kind === "restoring") {
    const target = topologyRebaseViewportCapture(
      topologyViewportState.focus.target,
      nextSize,
    );
    stopTopologyViewportMotion();
    topologyViewportState.focus = { kind: "idle" };
    if (wasCompact && !compact && topologyViewportState.overviewMode === "auto") {
      fitTopology();
    } else {
      setTopologyViewport(target.viewport, { purpose: "restore" });
    }
    return;
  }

  stopTopologyViewportMotion();

  const selectedId = topologyNavigationState.selectedNodeId;
  if (!wasCompact && compact && selectedId && topologyViewportState.overviewMode === "auto") {
    const selectedNode = topologyCanvas.getElementById(selectedId);
    if (!selectedNode.empty()) {
      fitTopology();
      const baseline = captureTopologyViewport();
      if (baseline) {
        topologyViewportState.focus = { kind: "focused", baseline };
        focusTopologyNode(selectedNode, { selectionChanged: false, animate: false });
        return;
      }
    }
  }

  if (topologyHasRendered && topologyViewportState.overviewMode === "auto") {
    fitTopology();
  }
}

function renderTopologyTree(projects, signature, nodeIds = []) {
  if (signature === topologyTreeSignature) return;
  topologyTreeSignature = signature;
  let nodeIndex = 0;
  const nodeAttributes = () => {
    const nodeId = nodeIds[nodeIndex++];
    return nodeId
      ? ` id="${topologyA11yId(nodeId)}" data-topology-node-id="${escapeHtml(nodeId)}"`
      : "";
  };
  const target = byId("topology-a11y");
  const projectRecords = topologyRecords(projects);
  target.innerHTML = projectRecords.length
    ? `<h3>Herdr topology text view</h3><ul>${projectRecords.map((project) => {
      const projectAttributes = nodeAttributes();
      const projectLabel = topologyText(
        topologyIdentity(project.label, project.project_id),
        "Project",
      );
      const worktrees = topologyRecords(project.worktrees);
      return `<li${projectAttributes}>Project: ${escapeHtml(projectLabel)}
        <ul>${worktrees.map((worktree) => {
          const worktreeAttributes = nodeAttributes();
          const worktreeLabel = topologyText(
            topologyIdentity(worktree.label, worktree.workspace_id),
            "Workspace",
          );
          const tabs = topologyRecords(worktree.tabs);
          return `<li${worktreeAttributes}>Worktree: ${escapeHtml(worktreeLabel)}
            <ul>${tabs.map((tab) => {
              const tabAttributes = nodeAttributes();
              const tabLabel = topologyText(topologyIdentity(tab.label, tab.tab_id), "Tab");
              const panes = topologyRecords(tab.panes);
              return `<li${tabAttributes}>Tab: ${escapeHtml(tabLabel)}
                <ul>${panes.map((pane) => {
                  const agent = topologyRecord(pane.agent) ? pane.agent : null;
                  const state = topologyText(
                    topologyIdentity(agent?.agent_status, pane.agent_status),
                    "unknown",
                  );
                  const name = agent
                    ? topologyText(topologyIdentity(agent.name, agent.agent), "agent")
                    : topologyText(pane.agent, "shell");
                  const paneId = topologyText(pane.pane_id, "Pane");
                  return `<li${nodeAttributes()}>Pane: ${escapeHtml(paneId)}, ${escapeHtml(name)}, ${escapeHtml(state)}</li>`;
                }).join("")}</ul>
              </li>`;
            }).join("")}</ul>
          </li>`;
        }).join("")}</ul>
      </li>`;
    }).join("")}</ul>`
    : "No matching Herdr topology";
}
