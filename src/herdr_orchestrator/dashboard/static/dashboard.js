"use strict";

const byId = (id) => document.getElementById(id);
const connection = document.querySelector(".connection");
let currentSnapshot = null;

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
  renderTopology(snapshot.topology?.workspaces || []);
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
        <dt>changed</dt><dd>${formatAge(job.updated_at)}</dd>
      </dl>
      ${job.error_code ? `<p class="drift-line">${escapeHtml(job.error_code)}</p>` : ""}
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

function renderTopology(workspaces) {
  byId("topology-count").textContent = `${workspaces.length} workspaces`;
  byId("topology").innerHTML = workspaces.length
    ? workspaces.map(workspaceNode).join("")
    : '<div class="empty-state compact">No matching Herdr workspaces</div>';
}

function workspaceNode(workspace) {
  const worktree = workspace.worktree;
  const tabs = workspace.tabs || [];
  return `
    <section class="workspace-node">
      <div class="node-heading">
        <strong>${escapeHtml(workspace.label || workspace.workspace_id)}</strong>
        <span class="node-id">${escapeHtml(workspace.workspace_id)}</span>
      </div>
      ${worktree ? `<p class="worktree-line">${escapeHtml(worktree.branch || "main")}<br>${escapeHtml(worktree.path || "")}</p>` : ""}
      ${tabs.map(tabNode).join("")}
    </section>
  `;
}

function tabNode(tab) {
  const panes = tab.panes || [];
  return `
    <div class="tab-node">
      <div class="tab-heading">
        <span>${escapeHtml(tab.label || tab.tab_id)}</span>
        <span class="node-id">${panes.length} pane${panes.length === 1 ? "" : "s"}</span>
      </div>
      ${panes.map(paneNode).join("")}
    </div>
  `;
}

function paneNode(pane) {
  const agent = pane.agent;
  const label = agent
    ? `${agent.name || agent.agent || "agent"} · ${agent.agent_status || "unknown"}`
    : "shell";
  const status = agent?.agent_status || "unknown";
  return `
    <div class="pane-row">
      <span>${escapeHtml(pane.pane_id)}</span>
      <span class="agent-chip status-${stateClass(status)}">${escapeHtml(label)}</span>
    </div>
  `;
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

loadInitial();
connectEvents();
setInterval(() => {
  if (currentSnapshot) {
    byId("last-updated").textContent = `Updated ${formatAge(currentSnapshot.generated_at)}`;
  }
}, 1000);
