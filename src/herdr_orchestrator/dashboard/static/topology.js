"use strict";

// Pure, DOM-free topology graph construction for the dashboard canvas.
// Loaded as a classic script BEFORE dashboard.js; the functions below become
// globals that dashboard.js calls. The same file is evaluated directly by
// tests/test_topology_js.py, which wraps it in a Node harness (appends a
// module.exports line) so the graph projection and layout can be fixture-tested
// without a browser or Cytoscape. Keep this file free of browser/DOM globals.

function stateClass(value) {
  return (topologyText(value) || "unknown")
    .toLowerCase()
    .replaceAll("_", "-")
    .replace(/[^a-z0-9-]/g, "-");
}

function normalizedProjects(topology, workflow) {
  const snapshot = topologyRecord(topology) ? topology : {};
  const projects = topologyRecords(snapshot.projects);
  if (projects.length) {
    return projects.length === snapshot.projects.length ? snapshot.projects : projects;
  }
  const workspaces = topologyRecords(snapshot.workspaces);
  if (!workspaces.length) return [];
  const workflowLabel = topologyText(workflow);
  const projectLabel = workflowLabel || "Project";
  return [{
    project_id: `workflow:${projectLabel}`,
    label: projectLabel,
    worktrees: workspaces.map((workspace) => {
      const worktree = topologyRecord(workspace.worktree) ? workspace.worktree : {};
      return {
        worktree_id: workspace.workspace_id,
        workspace_id: workspace.workspace_id,
        label: worktree.label || workspace.label || workspace.workspace_id,
        path: worktree.path || null,
        branch: worktree.branch || null,
        is_linked_worktree: worktree.is_linked_worktree === true,
        tabs: workspace.tabs || [],
      };
    }),
  }];
}

function topologyGraph(projects) {
  const elements = [];
  const layoutProjects = [];
  const counts = { projects: 0, worktrees: 0, tabs: 0, panes: 0 };
  const usedIds = new Set();

  normalizedProjectRecords(projects).forEach((project, projectIndex) => {
    const projectLabel = topologyText(
      topologyIdentity(project.label, project.project_id),
      "Project",
    );
    const projectId = uniqueTopologyId(
      usedIds,
      "project",
      topologyIdentity(project.project_id, project.label),
      projectIndex,
    );
    const worktrees = project.worktrees;
    const layoutProject = { id: projectId, worktrees: [] };
    layoutProjects.push(layoutProject);
    counts.projects += 1;
    elements.push({
      group: "nodes",
      data: {
        id: projectId,
        kind: "project",
        kindLabel: "Project",
        label: projectLabel,
        displayLabel: `PROJECT  ${projectLabel}`,
        identity: topologyText(project.project_id),
        detail: `${worktrees.length} worktree${worktrees.length === 1 ? "" : "s"}`,
      },
      classes: "kind-project",
    });

    worktrees.forEach((worktree, worktreeIndex) => {
      const worktreeId = uniqueTopologyId(
        usedIds,
        `${projectId}|worktree`,
        topologyIdentity(worktree.worktree_id, worktree.workspace_id, worktree.label),
        worktreeIndex,
      );
      const tabs = worktree.tabs;
      const layoutWorktree = { id: worktreeId, tabs: [] };
      layoutProject.worktrees.push(layoutWorktree);
      const worktreeLabel = topologyText(
        topologyIdentity(worktree.label, worktree.branch, worktree.workspace_id),
        "Workspace",
      );
      const branch = topologyText(worktree.branch);
      const path = topologyText(worktree.path);
      const branchLabel = branch ? `\n${branch}` : "";
      counts.worktrees += 1;
      elements.push({
        group: "nodes",
        data: {
          id: worktreeId,
          parent: projectId,
          kind: "worktree",
          kindLabel: "Worktree",
          label: worktreeLabel,
          displayLabel: `WORKTREE  ${worktreeLabel}${branchLabel}`,
          identity: topologyText(topologyIdentity(worktree.workspace_id, worktree.worktree_id)),
          detail: [branch, path].filter(Boolean).join(" · ") || "main workspace",
          branch,
          path,
          linked: worktree.is_linked_worktree === true,
        },
        classes: `kind-worktree${worktree.is_linked_worktree === true ? " is-linked" : ""}`,
      });

      tabs.forEach((tab, tabIndex) => {
        const tabId = uniqueTopologyId(
          usedIds,
          `${worktreeId}|tab`,
          topologyIdentity(tab.tab_id, tab.label),
          tabIndex,
        );
        const panes = tab.panes;
        const layoutTab = { id: tabId, panes: [] };
        layoutWorktree.tabs.push(layoutTab);
        const tabLabel = topologyText(topologyIdentity(tab.label, tab.tab_id), "Tab");
        counts.tabs += 1;
        elements.push({
          group: "nodes",
          data: {
            id: tabId,
            parent: worktreeId,
            kind: "tab",
            kindLabel: "Tab",
            label: tabLabel,
            displayLabel: `TAB  ${tabLabel}`,
            identity: topologyText(tab.tab_id),
            detail: `${panes.length} pane${panes.length === 1 ? "" : "s"}`,
          },
          classes: `kind-tab${tab.focused === true ? " is-focused" : ""}`,
        });

        panes.forEach((pane, paneIndex) => {
          const agent = topologyRecord(pane.agent) ? pane.agent : null;
          const status = topologyText(
            topologyIdentity(agent?.agent_status, pane.agent_status),
            "unknown",
          );
          const agentLabel = agent
            ? topologyText(topologyIdentity(agent.name, agent.agent), "agent")
            : topologyText(pane.agent, "shell");
          const paneLabel = topologyText(pane.pane_id, `Pane ${paneIndex + 1}`);
          const paneId = uniqueTopologyId(
            usedIds,
            `${tabId}|pane`,
            pane.pane_id,
            paneIndex,
          );
          layoutTab.panes.push(paneId);
          counts.panes += 1;
          elements.push({
            group: "nodes",
            data: {
              id: paneId,
              parent: tabId,
              kind: "pane",
              kindLabel: "Pane",
              label: paneLabel,
              displayLabel: `${paneLabel}\n${agentLabel} · ${status}`,
              identity: topologyText(pane.pane_id),
              detail: `${agentLabel} · ${status}`,
              agent: agentLabel,
              status,
              cwd: topologyText(pane.cwd),
            },
            classes: [
              "kind-pane",
              `status-${stateClass(status)}`,
              pane.focused === true ? "is-focused" : "",
            ].filter(Boolean).join(" "),
          });
        });
      });
    });
  });

  const structure = elements.map((element) => [element.data.id, element.data.parent || null]);
  const content = elements.map((element) => [element.data, element.classes]);
  return {
    counts,
    elements,
    positions: topologyPresetPositions(layoutProjects),
    structureSignature: JSON.stringify(structure),
    contentSignature: JSON.stringify(content),
  };
}

function topologyNavigationOrder(graph) {
  if (!graph || !Array.isArray(graph.elements)) return [];
  return graph.elements
    .map((element) => element?.data?.id)
    .filter((id) => typeof id === "string" && id.length > 0);
}

function topologySelectionDirection(event) {
  if (!event?.ctrlKey || event.altKey || event.metaKey) return null;
  if (event.key === "ArrowRight" || event.key === "ArrowDown") return 1;
  if (event.key === "ArrowLeft" || event.key === "ArrowUp") return -1;
  return null;
}

function topologyFocusViewport({
  viewport,
  subject,
  visibleRect,
  minZoom,
  maxZoom,
  minimumRenderedLabelPx = 9,
  preferredZoom = 0.72,
}) {
  const readableTarget = Math.min(
    maxZoom,
    Math.max(
      minZoom,
      viewport.zoom,
      preferredZoom,
      minimumRenderedLabelPx / subject.modelLabelPx,
    ),
  );
  const renderedCenter = {
    x: (visibleRect.x1 + visibleRect.x2) / 2,
    y: (visibleRect.y1 + visibleRect.y2) / 2,
  };
  let zoom = readableTarget;
  let modelCenter = subject.modelCenter;
  if (subject.kind === "container") {
    const modelWidth = subject.modelBounds.x2 - subject.modelBounds.x1;
    const modelHeight = subject.modelBounds.y2 - subject.modelBounds.y1;
    const fitCeiling = Math.min(
      (visibleRect.x2 - visibleRect.x1) / modelWidth,
      (visibleRect.y2 - visibleRect.y1) / modelHeight,
    );
    zoom = Math.min(
      maxZoom,
      Math.max(minZoom, Math.min(readableTarget, fitCeiling)),
    );
    modelCenter = {
      x: (subject.modelBounds.x1 + subject.modelBounds.x2) / 2,
      y: (subject.modelBounds.y1 + subject.modelBounds.y2) / 2,
    };
  } else if (subject.kind === "leaf" && subject.context) {
    const { leafBounds, contextBounds } = subject.context;
    const validBounds = (bounds) => (
      bounds !== null
      && typeof bounds === "object"
      && !Array.isArray(bounds)
      && [bounds.x1, bounds.y1, bounds.x2, bounds.y2].every(Number.isFinite)
      && bounds.x2 > bounds.x1
      && bounds.y2 > bounds.y1
    );
    if (
      validBounds(leafBounds)
      && validBounds(contextBounds)
      && contextBounds.x1 <= leafBounds.x1
      && contextBounds.y1 <= leafBounds.y1
      && contextBounds.x2 >= leafBounds.x2
      && contextBounds.y2 >= leafBounds.y2
    ) {
      const contextWidth = contextBounds.x2 - contextBounds.x1;
      const contextHeight = contextBounds.y2 - contextBounds.y1;
      const fitCeiling = Math.min(
        (visibleRect.x2 - visibleRect.x1) / contextWidth,
        (visibleRect.y2 - visibleRect.y1) / contextHeight,
      );
      const contextCenter = {
        x: (contextBounds.x1 + contextBounds.x2) / 2,
        y: (contextBounds.y1 + contextBounds.y2) / 2,
      };
      if (fitCeiling >= readableTarget) {
        modelCenter = contextCenter;
      } else {
        const contextPan = {
          x: renderedCenter.x - contextCenter.x * zoom,
          y: renderedCenter.y - contextCenter.y * zoom,
        };
        const minPanX = visibleRect.x1 - leafBounds.x1 * zoom;
        const maxPanX = visibleRect.x2 - leafBounds.x2 * zoom;
        const minPanY = visibleRect.y1 - leafBounds.y1 * zoom;
        const maxPanY = visibleRect.y2 - leafBounds.y2 * zoom;
        const panLimits = [minPanX, maxPanX, minPanY, maxPanY];
        if (
          panLimits.every(Number.isFinite)
          && minPanX <= maxPanX
          && minPanY <= maxPanY
        ) {
          return {
            zoom,
            pan: {
              x: Math.min(maxPanX, Math.max(minPanX, contextPan.x)),
              y: Math.min(maxPanY, Math.max(minPanY, contextPan.y)),
            },
          };
        }
      }
    }
  }
  return {
    zoom,
    pan: {
      x: renderedCenter.x - modelCenter.x * zoom,
      y: renderedCenter.y - modelCenter.y * zoom,
    },
  };
}

function topologyZoomViewport({
  nodes = [],
  selectedNodeId = null,
  viewportCenter,
  fallbackViewport,
} = {}) {
  const fallbackValues = [
    fallbackViewport?.zoom,
    fallbackViewport?.pan?.x,
    fallbackViewport?.pan?.y,
  ];
  if (!fallbackValues.every(Number.isFinite) || fallbackViewport.zoom <= 0) return null;
  if (![viewportCenter?.x, viewportCenter?.y].every(Number.isFinite)) {
    return fallbackViewport;
  }
  const validNodes = Array.isArray(nodes)
    ? nodes.filter((node) => (
      node !== null
      && typeof node === "object"
      && !Array.isArray(node)
      && typeof node.id === "string"
      && node.id.length > 0
      && [
        node.modelPosition?.x,
        node.modelPosition?.y,
        node.renderedPosition?.x,
        node.renderedPosition?.y,
      ].every(Number.isFinite)
    ))
    : [];
  if (!validNodes.length) return fallbackViewport;
  const selected = typeof selectedNodeId === "string"
    ? validNodes.find((node) => node.id === selectedNodeId)
    : null;
  const panes = validNodes.filter((node) => node.kind === "pane");
  const nearestCandidates = panes.length ? panes : validNodes;
  const anchor = selected || nearestCandidates.reduce((nearest, node) => {
    if (!nearest) return node;
    const distance = Math.hypot(
      node.renderedPosition.x - viewportCenter.x,
      node.renderedPosition.y - viewportCenter.y,
    );
    const nearestDistance = Math.hypot(
      nearest.renderedPosition.x - viewportCenter.x,
      nearest.renderedPosition.y - viewportCenter.y,
    );
    return distance < nearestDistance ? node : nearest;
  }, null);
  return {
    zoom: fallbackViewport.zoom,
    pan: {
      x: viewportCenter.x - anchor.modelPosition.x * fallbackViewport.zoom,
      y: viewportCenter.y - anchor.modelPosition.y * fallbackViewport.zoom,
    },
  };
}

function topologyRebaseViewportCapture(capture, nextSize) {
  return {
    size: nextSize,
    viewport: {
      zoom: capture.viewport.zoom,
      pan: {
        x: capture.viewport.pan.x + (nextSize.width - capture.size.width) / 2,
        y: capture.viewport.pan.y + (nextSize.height - capture.size.height) / 2,
      },
    },
  };
}

function topologyViewportMotionDuration({
  currentViewport,
  targetViewport,
  viewportSize,
} = {}) {
  const minimumDuration = 180;
  const maximumDuration = 240;
  const fullDistance = 0.8;
  const values = [
    currentViewport?.zoom,
    currentViewport?.pan?.x,
    currentViewport?.pan?.y,
    targetViewport?.zoom,
    targetViewport?.pan?.x,
    targetViewport?.pan?.y,
    viewportSize?.width,
    viewportSize?.height,
  ];
  if (
    !values.every(Number.isFinite)
    || currentViewport.zoom <= 0
    || targetViewport.zoom <= 0
    || viewportSize.width <= 0
    || viewportSize.height <= 0
  ) return minimumDuration;

  const diagonal = Math.hypot(viewportSize.width, viewportSize.height);
  const zoomTravel = Math.abs(Math.log(targetViewport.zoom / currentViewport.zoom));
  const panTravel = Math.hypot(
    targetViewport.pan.x - currentViewport.pan.x,
    targetViewport.pan.y - currentViewport.pan.y,
  ) / diagonal;
  const distance = Math.hypot(zoomTravel, panTravel);
  const progress = Math.min(distance / fullDistance, 1);
  return minimumDuration + (maximumDuration - minimumDuration) * progress;
}

function topologyPresetPositions(projects) {
  const positions = Object.create(null);
  const paneWidth = 218;
  const paneHeight = 78;
  const paneGapX = 36;
  const paneGapY = 30;
  const tabGapY = 86;
  const worktreeGapX = 330;
  const projectGapY = 230;
  let projectTop = 0;

  topologyRecords(projects).forEach((project) => {
    let worktreeLeft = 0;
    let projectHeight = paneHeight;
    const worktrees = topologyRecords(project.worktrees);

    worktrees.forEach((worktree) => {
      const tabs = topologyRecords(worktree.tabs).map((tab) => ({
        tab,
        panes: Array.isArray(tab.panes)
          ? tab.panes.filter((paneId) => typeof paneId === "string" && paneId)
          : [],
      }));
      const tabSizes = tabs.map(({ panes }) => {
        if (!panes.length) return { columns: 1, rows: 1, width: 190, height: 58 };
        const columns = Math.min(2, panes.length);
        const rows = Math.ceil(panes.length / columns);
        return {
          columns,
          rows,
          width: columns * paneWidth + (columns - 1) * paneGapX,
          height: rows * paneHeight + (rows - 1) * paneGapY,
        };
      });
      const worktreeWidth = tabSizes.length
        ? Math.max(...tabSizes.map((size) => size.width), 250)
        : 250;
      let tabTop = projectTop;

      if (!tabs.length) {
        if (typeof worktree.id === "string" && worktree.id) {
          positions[worktree.id] = {
            x: worktreeLeft + worktreeWidth / 2,
            y: tabTop + 41,
          };
        }
        tabTop += 82;
      } else {
        tabs.forEach(({ tab, panes }, tabIndex) => {
          const size = tabSizes[tabIndex];
          if (!panes.length) {
            if (typeof tab.id === "string" && tab.id) {
              positions[tab.id] = {
                x: worktreeLeft + worktreeWidth / 2,
                y: tabTop + size.height / 2,
              };
            }
          } else {
            const contentLeft = worktreeLeft + (worktreeWidth - size.width) / 2;
            panes.forEach((paneId, paneIndex) => {
              const column = paneIndex % size.columns;
              const row = Math.floor(paneIndex / size.columns);
              positions[paneId] = {
                x: contentLeft + paneWidth / 2 + column * (paneWidth + paneGapX),
                y: tabTop + paneHeight / 2 + row * (paneHeight + paneGapY),
              };
            });
          }
          tabTop += size.height + (tabIndex === tabs.length - 1 ? 0 : tabGapY);
        });
      }

      const worktreeHeight = Math.max(tabTop - projectTop, 82);
      projectHeight = Math.max(projectHeight, worktreeHeight);
      worktreeLeft += worktreeWidth + worktreeGapX;
    });

    if (!worktrees.length) {
      if (typeof project.id === "string" && project.id) {
        positions[project.id] = { x: 140, y: projectTop + 45 };
      }
      projectHeight = 90;
    }
    projectTop += projectHeight + projectGapY;
  });
  return positions;
}

function topologyId(prefix, value, fallback) {
  const fallbackIdentity = topologyText(fallback, "undefined");
  const identity = topologyText(value).trim() || fallbackIdentity.trim();
  const encoded = encodeURIComponent(identity).replace(/[!'()*]/g, (character) => (
    `%${character.charCodeAt(0).toString(16).toUpperCase()}`
  ));
  return `${prefix}:${encoded}`;
}

function uniqueTopologyId(usedIds, prefix, value, fallback) {
  const baseId = topologyId(prefix, value, fallback);
  let id = baseId;
  let duplicate = 2;
  while (usedIds.has(id)) {
    id = `${baseId}|duplicate:${duplicate}`;
    duplicate += 1;
  }
  usedIds.add(id);
  return id;
}

function topologyRecord(value) {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function topologyRecords(value) {
  return Array.isArray(value) ? value.filter(topologyRecord) : [];
}

function topologyIdentity(...values) {
  return values.find((value) => topologyText(value).trim());
}

function normalizedProjectRecords(projects) {
  return topologyRecords(projects).map((project) => ({
    ...project,
    worktrees: topologyRecords(project.worktrees).map((worktree) => ({
      ...worktree,
      tabs: topologyRecords(worktree.tabs).map((tab) => ({
        ...tab,
        panes: topologyRecords(tab.panes),
      })),
    })),
  }));
}

function wellFormedText(value) {
  const input = typeof value === "string" ? value : "";
  let output = "";
  for (let index = 0; index < input.length; index += 1) {
    const codeUnit = input.charCodeAt(index);
    if (codeUnit >= 0xD800 && codeUnit <= 0xDBFF) {
      const next = input.charCodeAt(index + 1);
      if (next >= 0xDC00 && next <= 0xDFFF) {
        output += input[index] + input[index + 1];
        index += 1;
      } else {
        output += "\uFFFD";
      }
    } else if (codeUnit >= 0xDC00 && codeUnit <= 0xDFFF) {
      output += "\uFFFD";
    } else {
      output += input[index];
    }
  }
  return output;
}

function topologyText(value, fallback = "") {
  if (value === null || value === undefined) return fallback;
  if (typeof value === "object" || typeof value === "function" || typeof value === "symbol") {
    return fallback;
  }
  return wellFormedText(String(value));
}
