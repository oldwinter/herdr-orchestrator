"use strict";

const byId = (id) => document.getElementById(id);
const connection = document.querySelector(".connection");
let currentSnapshot = null;
let topologyCanvas = null;
let topologyContentSignature = "";
let topologyStructureSignature = "";
let topologyViewportTouched = false;
let topologyViewportUpdate = false;
let topologyHasRendered = false;
let topologyTreeSignature = null;

const columns = [
  { key: "queued", label: "Queued", states: ["pending"] },
  { key: "working", label: "In motion", states: ["running"] },
  { key: "attention", label: "Attention", states: ["blocked", "failed"] },
  { key: "finished", label: "Finished", states: ["succeeded"] },
];

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatTime(epoch) {
  if (!Number.isFinite(Number(epoch))) return "unknown";
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(Number(epoch) * 1000));
}

function formatDateTime(epoch) {
  if (!Number.isFinite(Number(epoch))) return "unknown";
  return new Intl.DateTimeFormat(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  }).format(new Date(Number(epoch) * 1000));
}

function formatAge(epoch, now = Date.now() / 1000) {
  if (!Number.isFinite(Number(epoch))) return "unknown";
  const seconds = Math.max(0, Math.round(now - Number(epoch)));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

function stateClass(value) {
  return String(value || "unknown")
    .toLowerCase()
    .replaceAll("_", "-")
    .replace(/[^a-z0-9-]/g, "-");
}

function isoTime(epoch) {
  if (!Number.isFinite(Number(epoch))) return "";
  return new Date(Number(epoch) * 1000).toISOString();
}

function setConnection(mode, label) {
  connection.classList.remove("is-live", "is-offline");
  if (mode) connection.classList.add(mode);
  byId("connection-label").textContent = label;
}

function render(snapshot) {
  currentSnapshot = snapshot;
  const summary = snapshot.summary || {};
  byId("workflow-name").textContent = snapshot.workflow || "Herdr Orchestrator";
  byId("metric-running").textContent = summary.running ?? "—";
  byId("metric-attention").textContent = summary.needs_attention ?? "—";
  byId("metric-pending").textContent = summary.pending ?? "—";
  byId("metric-agents").textContent = summary.active_agents ?? "—";
  byId("metric-worktrees").textContent = summary.worktrees ?? "—";
  byId("metric-succeeded").textContent = summary.succeeded ?? "—";
  byId("job-total").textContent = `${summary.total ?? 0} jobs`;
  byId("last-updated").textContent = `Updated ${formatAge(snapshot.generated_at)}`;

  const health = snapshot.source_health || {};
  const warning = byId("source-warning");
  if (health.queue !== "ok" || health.herdr !== "ok") {
    const parts = [];
    if (health.queue !== "ok") parts.push("Queue observation unavailable");
    if (health.herdr !== "ok") {
      parts.push(`Herdr observation unavailable: ${health.herdr_error || "unknown"}`);
    }
    warning.textContent = parts.join(" · ");
    warning.classList.remove("is-hidden");
  } else {
    warning.classList.add("is-hidden");
  }

  renderKanban(snapshot.jobs || []);
  renderAttention(snapshot.attention || []);
  renderTopology(snapshot.topology || {}, snapshot.workflow);
  renderTimeline(snapshot.timeline || []);
}

function renderKanban(jobs) {
  byId("kanban").innerHTML = columns.map((column) => {
    const selected = jobs.filter((job) => column.states.includes(job.state));
    const cards = selected.length
      ? selected.map(jobCard).join("")
      : `<div class="empty-state compact">No ${escapeHtml(column.label.toLowerCase())} jobs</div>`;
    return `
      <section class="kanban-column" aria-label="${escapeHtml(column.label)}">
        <div class="column-heading">
          <span>${escapeHtml(column.label)}</span>
          <span class="column-count">${selected.length}</span>
        </div>
        <div class="job-stack">${cards}</div>
      </section>
    `;
  }).join("");
}

function jobCard(job) {
  const runtime = job.runtime || {};
  const runtimeState = runtime.agent_status;
  const agentLabel = job.agent_name || "unassigned";
  const location = runtime.pane_id || job.herdr_workspace_id || "not observed";
  const drift = (job.drift || []).map((item) => escapeHtml(item.replaceAll("_", " "))).join(" · ");
  const settled = job.agent_settled === true
    ? "yes"
    : job.agent_settled === false ? "no" : "pending";
  const verified = job.task_verified === true
    ? "yes"
    : job.task_verified === false
      ? "no"
      : job.receipt_kind ? "pending" : "not declared";
  return `
    <article class="job-card state-${stateClass(job.state)}">
      <h3>${escapeHtml(job.title)}</h3>
      <div class="job-meta">
        <span class="state-badge state-${stateClass(job.state)}">${escapeHtml(job.state)}</span>
        <span class="placement-badge">${escapeHtml(job.placement || "auto")}</span>
        ${runtimeState ? `<span class="runtime-badge runtime-${stateClass(runtimeState)}">${escapeHtml(runtimeState)}</span>` : ""}
      </div>
      <dl class="job-detail">
        <dt>worker</dt><dd>${escapeHtml(job.harness)}</dd>
        <dt>attempt</dt><dd>${escapeHtml(job.attempts)} / ${escapeHtml(job.max_attempts)}</dd>
        <dt>agent</dt><dd title="${escapeHtml(agentLabel)}">${escapeHtml(agentLabel)}</dd>
        <dt>location</dt><dd title="${escapeHtml(location)}">${escapeHtml(location)}</dd>
        <dt>agent settled</dt><dd>${escapeHtml(settled)}</dd>
        <dt>task verified</dt><dd>${escapeHtml(verified)}</dd>
        <dt>changed</dt><dd>${formatAge(job.updated_at)}</dd>
      </dl>
      ${job.error_code ? `<p class="drift-line">${escapeHtml(job.error_code)}</p>` : ""}
      ${job.error_summary ? `<p class="error-summary">${escapeHtml(job.error_summary)}</p>` : ""}
      ${drift ? `<p class="drift-line">${drift}</p>` : ""}
    </article>
  `;
}

function renderAttention(items) {
  byId("attention-count").textContent = String(items.length);
  byId("attention-list").innerHTML = items.length
    ? items.map((item) => `
      <article class="attention-item severity-${stateClass(item.severity)}">
        <strong>${escapeHtml(item.title)}</strong>
        <span>${escapeHtml(item.code)} · ${escapeHtml(item.message)}</span>
      </article>
    `).join("")
    : '<div class="empty-state compact">No active alerts</div>';
}

function renderTopology(topology, workflow) {
  const projects = normalizedProjects(topology, workflow);
  const graph = topologyGraph(projects);
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
    `Interactive Herdr topology: ${countParts.join(", ")}`,
  );
  byId("topology").dataset.nodeCount = String(graph.elements.length);
  renderTopologyTree(projects, graph.contentSignature);

  const empty = byId("topology-empty");
  if (!graph.elements.length) {
    empty.textContent = "No matching Herdr topology";
    empty.classList.remove("is-hidden");
    if (topologyCanvas) topologyCanvas.elements().remove();
    topologyContentSignature = "";
    topologyStructureSignature = "";
    topologyHasRendered = false;
    clearTopologySelection();
    return;
  }

  const canvas = ensureTopologyCanvas();
  if (!canvas) {
    empty.textContent = "Topology renderer unavailable";
    empty.classList.remove("is-hidden");
    return;
  }
  empty.classList.add("is-hidden");

  if (graph.contentSignature === topologyContentSignature) return;

  const structureChanged = graph.structureSignature !== topologyStructureSignature;
  const selectedId = canvas.$("node:selected").first().id();
  const existingViewport = { zoom: canvas.zoom(), pan: canvas.pan() };
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

  topologyContentSignature = graph.contentSignature;
  topologyStructureSignature = graph.structureSignature;

  if (structureChanged) {
    const shouldFit = !topologyHasRendered || !topologyViewportTouched;
    const layout = canvas.layout({
      name: "preset",
      positions: (node) => graph.positions[node.id()] || node.position(),
      fit: false,
      animate: false,
    });
    layout.one("layoutstop", () => {
      canvas.resize();
      if (shouldFit) {
        fitTopology({ minimumReadable: true });
      } else {
        setTopologyViewport(existingViewport);
      }
    });
    layout.run();
    topologyHasRendered = true;
  }

  if (selectedId && !canvas.getElementById(selectedId).empty()) {
    const selected = canvas.getElementById(selectedId);
    selected.select();
    renderTopologyInspector(selected);
  } else {
    clearTopologySelection();
  }
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

function ensureTopologyCanvas() {
  if (topologyCanvas) return topologyCanvas;
  if (typeof cytoscape !== "function") return null;

  topologyCanvas = cytoscape({
    container: byId("topology"),
    elements: [],
    autoungrabify: true,
    boxSelectionEnabled: false,
    minZoom: 0.08,
    maxZoom: 2.6,
    style: topologyStyles(),
  });

  topologyCanvas.on("viewport", () => {
    if (!topologyViewportUpdate) topologyViewportTouched = true;
  });
  topologyCanvas.on("tap", "node", (event) => {
    topologyCanvas.nodes().unselect();
    event.target.select();
    renderTopologyInspector(event.target);
  });
  topologyCanvas.on("tap", (event) => {
    if (event.target === topologyCanvas) clearTopologySelection();
  });

  if (typeof ResizeObserver === "function") {
    const observer = new ResizeObserver(() => {
      topologyCanvas.resize();
      if (topologyHasRendered && !topologyViewportTouched) {
        fitTopology({ minimumReadable: true });
      }
    });
    observer.observe(byId("topology"));
  }
  byId("topology").dataset.renderer = "cytoscape-canvas";
  return topologyCanvas;
}

function topologyStyles() {
  const mono = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
  return [
    {
      selector: "node",
      style: {
        "font-family": mono,
        "font-size": 11,
        "font-weight": 600,
        "label": "data(displayLabel)",
        "text-wrap": "wrap",
        "text-max-width": 210,
        "color": "#bcc3c8",
        "overlay-opacity": 0,
        "transition-property": "border-color, background-color, opacity",
        "transition-duration": "160ms",
      },
    },
    {
      selector: "node[kind = 'project']",
      style: {
        "shape": "round-rectangle",
        "background-color": "#11161a",
        "background-opacity": 0.72,
        "border-color": "#525d66",
        "border-width": 1,
        "padding": 44,
        "text-valign": "top",
        "text-halign": "left",
        "text-margin-x": 12,
        "text-margin-y": 12,
        "font-size": 13,
        "color": "#f0eee8",
      },
    },
    {
      selector: "node[kind = 'worktree']",
      style: {
        "shape": "round-rectangle",
        "background-color": "#162016",
        "background-opacity": 0.72,
        "border-color": "#879d48",
        "border-style": "dashed",
        "border-width": 1,
        "padding": 34,
        "text-valign": "top",
        "text-halign": "left",
        "text-margin-x": 10,
        "text-margin-y": 10,
        "color": "#d7ff64",
      },
    },
    {
      selector: "node[kind = 'tab']",
      style: {
        "shape": "round-rectangle",
        "width": 190,
        "height": 58,
        "background-color": "#13202b",
        "background-opacity": 0.82,
        "border-color": "#4e83aa",
        "border-width": 1,
        "padding": 27,
        "text-valign": "top",
        "text-halign": "left",
        "text-margin-x": 9,
        "text-margin-y": 9,
        "color": "#91c9f7",
      },
    },
    {
      selector: "node[kind = 'pane']",
      style: {
        "shape": "round-rectangle",
        "width": 218,
        "height": 78,
        "background-color": "#1b2024",
        "border-color": "#4a535b",
        "border-width": 1,
        "text-valign": "center",
        "text-halign": "center",
        "line-height": 1.65,
        "color": "#d8dde0",
      },
    },
    {
      selector: "node[kind = 'project']:childless",
      style: { "width": 280, "height": 90 },
    },
    {
      selector: "node[kind = 'worktree']:childless",
      style: { "width": 250, "height": 82 },
    },
    {
      selector: "node.status-working",
      style: { "border-color": "#75baff", "border-width": 2 },
    },
    {
      selector: "node.status-blocked",
      style: { "border-color": "#ff756d", "border-width": 2, "background-color": "#2b1a1b" },
    },
    {
      selector: "node.status-done, node.status-idle",
      style: { "border-color": "#6ee7a8" },
    },
    {
      selector: "node.is-focused",
      style: { "border-style": "double", "border-width": 4 },
    },
    {
      selector: "node:selected",
      style: {
        "border-color": "#d7ff64",
        "border-width": 3,
        "overlay-color": "#d7ff64",
        "overlay-opacity": 0.08,
        "overlay-padding": 8,
      },
    },
  ];
}

function fitTopology({ minimumReadable = false } = {}) {
  if (!topologyCanvas || topologyCanvas.elements().empty()) return;
  topologyViewportUpdate = true;
  topologyCanvas.fit(topologyCanvas.elements(), 60);
  const container = byId("topology");
  const minimumReadableZoom = minimumReadable && container.clientWidth < 600 ? 0.68 : 0;
  if (topologyCanvas.zoom() < minimumReadableZoom) {
    topologyCanvas.zoom({
      level: minimumReadableZoom,
      renderedPosition: {
        x: container.clientWidth / 2,
        y: container.clientHeight / 2,
      },
    });
  }
  topologyViewportUpdate = false;
  topologyViewportTouched = false;
}

function setTopologyViewport(viewport) {
  topologyViewportUpdate = true;
  topologyCanvas.zoom(viewport.zoom);
  topologyCanvas.pan(viewport.pan);
  topologyViewportUpdate = false;
}

function zoomTopology(factor) {
  if (!topologyCanvas || topologyCanvas.elements().empty()) return;
  const bounds = byId("topology").getBoundingClientRect();
  const level = Math.min(
    topologyCanvas.maxZoom(),
    Math.max(topologyCanvas.minZoom(), topologyCanvas.zoom() * factor),
  );
  topologyCanvas.zoom({
    level,
    renderedPosition: { x: bounds.width / 2, y: bounds.height / 2 },
  });
  topologyViewportTouched = true;
}

function renderTopologyInspector(node) {
  const inspector = byId("topology-inspector");
  const kind = node.data("kindLabel") || "Node";
  inspector.innerHTML = `
    <span class="inspector-kind">${escapeHtml(kind)}</span>
    <strong>${escapeHtml(node.data("label") || node.id())}</strong>
    <span title="${escapeHtml(node.data("detail") || "")}">${escapeHtml(node.data("detail") || node.data("identity") || "")}</span>
  `;
  inspector.classList.remove("is-hidden");
}

function clearTopologySelection() {
  if (topologyCanvas) topologyCanvas.nodes().unselect();
  const inspector = byId("topology-inspector");
  inspector.innerHTML = "";
  inspector.classList.add("is-hidden");
}

function renderTopologyTree(projects, signature) {
  if (signature === topologyTreeSignature) return;
  topologyTreeSignature = signature;
  const target = byId("topology-a11y");
  target.innerHTML = projects.length
    ? `<h3>Herdr topology text view</h3><ul>${projects.map((project) => `
      <li>Project: ${escapeHtml(project.label || project.project_id || "Project")}
        <ul>${(project.worktrees || []).map((worktree) => `
          <li>Worktree: ${escapeHtml(worktree.label || worktree.workspace_id || "Workspace")}
            <ul>${(worktree.tabs || []).map((tab) => `
              <li>Tab: ${escapeHtml(tab.label || tab.tab_id || "Tab")}
                <ul>${(tab.panes || []).map((pane) => {
                  const agent = pane.agent;
                  const state = agent?.agent_status || pane.agent_status || "unknown";
                  const name = agent?.name || agent?.agent || pane.agent || "shell";
                  return `<li>Pane: ${escapeHtml(pane.pane_id || "Pane")}, ${escapeHtml(name)}, ${escapeHtml(state)}</li>`;
                }).join("")}</ul>
              </li>
            `).join("")}</ul>
          </li>
        `).join("")}</ul>
      </li>
    `).join("")}</ul>`
    : "No matching Herdr topology";
}

function renderTimeline(events) {
  byId("timeline").innerHTML = events.length
    ? events.slice(0, 24).map((event) => `
      <article class="timeline-event state-${stateClass(event.state)}">
        <time class="event-time" datetime="${isoTime(event.at)}" title="${escapeHtml(formatDateTime(event.at))}">
          ${escapeHtml(formatTime(event.at))}
        </time>
        <span class="event-marker" aria-hidden="true"></span>
        <div class="event-copy">
          <strong>${escapeHtml(event.title)}</strong>
          <span>${escapeHtml(event.type)} · ${escapeHtml(event.state)}${event.attempt ? ` · attempt ${escapeHtml(event.attempt)}` : ""}</span>
          <span>${escapeHtml(event.detail || "")}${event.error_code ? ` · ${escapeHtml(event.error_code)}` : ""}</span>
        </div>
      </article>
    `).join("")
    : '<div class="empty-state compact">No lifecycle events yet</div>';
}

async function loadInitial() {
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`snapshot ${response.status}`);
    const payload = await response.json();
    render(payload.snapshot);
  } catch (_error) {
    byId("last-updated").textContent = "Snapshot is not ready";
  }
}

function connectEvents() {
  const events = new EventSource("/api/events");
  events.addEventListener("open", () => setConnection("is-live", "Live"));
  events.addEventListener("snapshot", (event) => {
    render(JSON.parse(event.data));
    setConnection("is-live", "Live");
  });
  events.addEventListener("error", () => {
    setConnection("is-offline", "Reconnecting");
    byId("last-updated").textContent = currentSnapshot
      ? `Last snapshot ${formatAge(currentSnapshot.generated_at)}`
      : "No snapshot received";
  });
}

byId("topology-zoom-out").addEventListener("click", () => zoomTopology(0.82));
byId("topology-fit").addEventListener("click", () => fitTopology());
byId("topology-zoom-in").addEventListener("click", () => zoomTopology(1.22));
byId("topology").addEventListener("keydown", (event) => {
  if (event.key === "+" || event.key === "=") {
    event.preventDefault();
    zoomTopology(1.22);
  } else if (event.key === "-") {
    event.preventDefault();
    zoomTopology(0.82);
  } else if (event.key === "0" || event.key === "Home") {
    event.preventDefault();
    fitTopology();
  }
});

loadInitial();
connectEvents();
setInterval(() => {
  if (currentSnapshot) {
    byId("last-updated").textContent = `Updated ${formatAge(currentSnapshot.generated_at)}`;
  }
}, 1000);
