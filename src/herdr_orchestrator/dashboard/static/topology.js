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
