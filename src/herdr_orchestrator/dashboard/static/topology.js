"use strict";

// Pure, DOM-free topology graph construction for the dashboard canvas.
// Loaded as a classic script BEFORE dashboard.js; the functions below become
// globals that dashboard.js calls. The same file is evaluated directly by
// tests/test_topology_js.py, which wraps it in a Node harness (appends a
// module.exports line) so the graph projection and layout can be fixture-tested
// without a browser or Cytoscape. Keep this file free of browser/DOM globals.

function stateClass(value) {
  return String(value || "unknown")
    .toLowerCase()
    .replaceAll("_", "-")
    .replace(/[^a-z0-9-]/g, "-");
}

function normalizedProjects(topology, workflow) {
  if (Array.isArray(topology.projects) && topology.projects.length) {
    return topology.projects;
  }
  const workspaces = Array.isArray(topology.workspaces) ? topology.workspaces : [];
  if (!workspaces.length) return [];
  return [{
    project_id: `workflow:${workflow || "project"}`,
    label: workflow || "Project",
    worktrees: workspaces.map((workspace) => {
      const worktree = workspace.worktree || {};
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

  projects.forEach((project, projectIndex) => {
    const projectId = topologyId(
      "project",
      project.project_id || project.label,
      projectIndex,
    );
    const worktrees = Array.isArray(project.worktrees) ? project.worktrees : [];
    const layoutProject = { id: projectId, worktrees: [] };
    layoutProjects.push(layoutProject);
    counts.projects += 1;
    elements.push({
      group: "nodes",
      data: {
        id: projectId,
        kind: "project",
        kindLabel: "Project",
        label: project.label || project.project_id || "Project",
        displayLabel: `PROJECT  ${project.label || project.project_id || "Project"}`,
        identity: project.project_id || "",
        detail: `${worktrees.length} worktree${worktrees.length === 1 ? "" : "s"}`,
      },
      classes: "kind-project",
    });

    worktrees.forEach((worktree, worktreeIndex) => {
      const worktreeId = topologyId(
        `${projectId}|worktree`,
        worktree.worktree_id || worktree.workspace_id || worktree.label,
        worktreeIndex,
      );
      const tabs = Array.isArray(worktree.tabs) ? worktree.tabs : [];
      const layoutWorktree = { id: worktreeId, tabs: [] };
      layoutProject.worktrees.push(layoutWorktree);
      const worktreeLabel = worktree.label || worktree.branch || worktree.workspace_id || "Workspace";
      const branchLabel = worktree.branch ? `\n${worktree.branch}` : "";
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
          identity: worktree.workspace_id || worktree.worktree_id || "",
          detail: [worktree.branch, worktree.path].filter(Boolean).join(" · ") || "main workspace",
          branch: worktree.branch || "",
          path: worktree.path || "",
          linked: worktree.is_linked_worktree === true,
        },
        classes: `kind-worktree${worktree.is_linked_worktree === true ? " is-linked" : ""}`,
      });

      tabs.forEach((tab, tabIndex) => {
        const tabId = topologyId(
          `${worktreeId}|tab`,
          tab.tab_id || tab.label,
          tabIndex,
        );
        const panes = Array.isArray(tab.panes) ? tab.panes : [];
        const layoutTab = { id: tabId, panes: [] };
        layoutWorktree.tabs.push(layoutTab);
        const tabLabel = tab.label || tab.tab_id || "Tab";
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
            identity: tab.tab_id || "",
            detail: `${panes.length} pane${panes.length === 1 ? "" : "s"}`,
          },
          classes: `kind-tab${tab.focused === true ? " is-focused" : ""}`,
        });

        panes.forEach((pane, paneIndex) => {
          const agent = pane.agent || null;
          const status = agent?.agent_status || pane.agent_status || "unknown";
          const agentLabel = agent
            ? agent.name || agent.agent || "agent"
            : pane.agent || "shell";
          const paneLabel = pane.pane_id || `Pane ${paneIndex + 1}`;
          const paneId = topologyId(`${tabId}|pane`, pane.pane_id, paneIndex);
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
              identity: pane.pane_id || "",
              detail: `${agentLabel} · ${status}`,
              agent: agentLabel,
              status,
              cwd: pane.cwd || "",
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

function topologyPresetPositions(projects) {
  const positions = {};
  const paneWidth = 218;
  const paneHeight = 78;
  const paneGapX = 36;
  const paneGapY = 30;
  const tabGapY = 86;
  const worktreeGapX = 330;
  const projectGapY = 230;
  let projectTop = 0;

  projects.forEach((project) => {
    let worktreeLeft = 0;
    let projectHeight = paneHeight;

    project.worktrees.forEach((worktree) => {
      const tabSizes = worktree.tabs.map((tab) => {
        if (!tab.panes.length) return { columns: 1, rows: 1, width: 190, height: 58 };
        const columns = Math.min(2, tab.panes.length);
        const rows = Math.ceil(tab.panes.length / columns);
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

      if (!worktree.tabs.length) {
        positions[worktree.id] = {
          x: worktreeLeft + worktreeWidth / 2,
          y: tabTop + 41,
        };
        tabTop += 82;
      } else {
        worktree.tabs.forEach((tab, tabIndex) => {
          const size = tabSizes[tabIndex];
          if (!tab.panes.length) {
            positions[tab.id] = {
              x: worktreeLeft + worktreeWidth / 2,
              y: tabTop + size.height / 2,
            };
          } else {
            const contentLeft = worktreeLeft + (worktreeWidth - size.width) / 2;
            tab.panes.forEach((paneId, paneIndex) => {
              const column = paneIndex % size.columns;
              const row = Math.floor(paneIndex / size.columns);
              positions[paneId] = {
                x: contentLeft + paneWidth / 2 + column * (paneWidth + paneGapX),
                y: tabTop + paneHeight / 2 + row * (paneHeight + paneGapY),
              };
            });
          }
          tabTop += size.height + (tabIndex === worktree.tabs.length - 1 ? 0 : tabGapY);
        });
      }

      const worktreeHeight = Math.max(tabTop - projectTop, 82);
      projectHeight = Math.max(projectHeight, worktreeHeight);
      worktreeLeft += worktreeWidth + worktreeGapX;
    });

    if (!project.worktrees.length) {
      positions[project.id] = { x: 140, y: projectTop + 45 };
      projectHeight = 90;
    }
    projectTop += projectHeight + projectGapY;
  });
  return positions;
}

function topologyId(prefix, value, fallback) {
  const identity = String(value || fallback).trim() || String(fallback);
  return `${prefix}:${encodeURIComponent(identity)}`;
}
