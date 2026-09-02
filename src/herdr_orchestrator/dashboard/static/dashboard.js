"use strict";

const byId = (id) => document.getElementById(id);
const connection = document.querySelector(".connection");
const compactViewport = window.matchMedia?.("(max-width: 760px)");
const primaryCoarsePointer = window.matchMedia?.("(pointer: coarse)") || null;
let topologyTouchOwnershipState = {
  coarseOwner: "page",
};
let mainGridOrderKey = "";
let currentSnapshot = null;
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
let kanbanHasRendered = false;
let previousJobVisuals = new Map();
let kanbanSignature = "";
let kanbanOrderKey = "";
let kanbanNavigationState = {
  activeColumnKey: null,
};
let kanbanScrollGeneration = 0;
let kanbanProgrammaticScroll = null;
let kanbanManualScrollFrame = null;
let kanbanLayoutCompact = null;
let kanbanLayoutWidth = 0;
let attentionHasRendered = false;
let attentionSignature = "";
let previousAttentionVisuals = new Map();
let timelineHasRendered = false;
let timelineSignature = "";
let previousTimelineVisuals = new Map();
let recoveryState = {
  browserTransport: { kind: "connecting" },
  awaitingFreshSnapshot: false,
};
const dataRegionIds = ["kanban", "attention-list", "topology", "timeline"];
const columns = [
  { key: "queued", label: "Queued", states: ["pending"] },
  { key: "working", label: "In motion", states: ["running"] },
  { key: "attention", label: "Attention", states: ["blocked", "failed"] },
  { key: "finished", label: "Finished", states: ["succeeded"] },
];
const topologyViewportControlMessages = Object.freeze({
  fitUnavailable: "No topology to fit.",
  zoomInBoundary: "Maximum zoom reached.",
  zoomOutBoundary: "Minimum zoom reached.",
});

function setConnection(mode, label) {
  const previousMode = connection.dataset.mode || "";
  connection.classList.remove("is-live", "is-offline");
  if (mode) connection.classList.add(mode);
  connection.dataset.mode = mode || "";
  byId("connection-label").textContent = label;
  if (previousMode && previousMode !== mode) {
    restartAnimation(connection, "is-transitioning");
  }
}

function motionAllowed() {
  return !window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
}

function restartAnimation(element, className, participants = [element], isCurrent = () => true) {
  participants.forEach((target) => target.classList.remove(className));
  if (!motionAllowed()) return;
  requestAnimationFrame(() => { if (isCurrent()) element.classList.add(className); });
}

function setMetric(id, value) {
  const target = byId(id);
  const next = textValue(value, "—");
  const changed = target.textContent !== "—" && target.textContent !== next;
  target.textContent = next;
  if (changed) restartAnimation(target, "is-changing");
}

function refreshJobAges() {
  document.querySelectorAll(".job-updated[data-epoch]").forEach((target) => {
    target.textContent = formatAge(target.dataset.epoch);
  });
}

function setDataBusy(isBusy) {
  dataRegionIds.forEach((id) => {
    byId(id).setAttribute("aria-busy", String(isBusy));
  });
}

function showUnavailableState(message) {
  const repeatedInitialError = !currentSnapshot
    && recoveryState.browserTransport.kind === "error"
    && recoveryState.browserTransport.warning === message;
  recoveryState = reduceRecoveryState(
    recoveryState,
    { type: "transport-error", warning: message },
  );
  setConnection("is-offline", "Reconnecting");
  setSourceWarning(
    sourceWarningMessage(recoveryState.browserTransport, currentSnapshot),
  );
  if (currentSnapshot) {
    byId("last-updated").textContent = `Last snapshot ${formatAge(currentSnapshot.generated_at)}`;
    return;
  }
  if (repeatedInitialError) return;
  setDataBusy(false);
  byId("kanban").innerHTML =
    '<div class="empty-state compact error-state" role="status">Queue state unavailable</div>';
  byId("attention-list").innerHTML =
    '<div class="empty-state compact error-state" role="status">Alerts unavailable</div>';
  byId("topology-empty").textContent = "Topology unavailable until a snapshot arrives";
  byId("topology-empty").classList.remove("is-hidden");
  byId("timeline").innerHTML =
    '<div class="empty-state compact error-state" role="status">Lifecycle unavailable</div>';
}

function render(snapshot) {
  if (!record(snapshot)) {
    throw new Error("snapshot_invalid");
  }
  const previousSnapshot = currentSnapshot;
  setDataBusy(false);
  const summary = record(snapshot.summary) ? snapshot.summary : {};
  byId("workflow-name").textContent = textValue(snapshot.workflow, "Herdr Orchestrator");
  setMetric("metric-running", summary.running);
  setMetric("metric-attention", summary.needs_attention);
  setMetric("metric-pending", summary.pending);
  setMetric("metric-agents", summary.active_agents);
  setMetric("metric-worktrees", summary.worktrees);
  setMetric("metric-succeeded", summary.succeeded);
  byId("job-total").textContent = `${textValue(summary.total, "0")} jobs`;
  byId("last-updated").textContent = recoveryState.awaitingFreshSnapshot
    ? `Last snapshot ${formatAge(snapshot.generated_at)}`
    : `Updated ${formatAge(snapshot.generated_at)}`;
  setSourceWarning(
    sourceWarningMessage(recoveryState.browserTransport, snapshot),
  );

  renderAttention(records(snapshot.attention));
  renderKanban(records(snapshot.jobs));
  renderTopology(record(snapshot.topology) ? snapshot.topology : {}, snapshot.workflow);
  renderTimeline(records(snapshot.timeline));
  announceStateChange(previousSnapshot, snapshot);
  currentSnapshot = snapshot;
}

function announceStateChange(previousSnapshot, snapshot) {
  if (!previousSnapshot) return;
  const previousJobs = new Map(
    records(previousSnapshot.jobs).map((job) => [textValue(job.id), textValue(job.state)]),
  );
  const changed = records(snapshot.jobs).filter((job) => (
    previousJobs.has(textValue(job.id))
      && previousJobs.get(textValue(job.id)) !== textValue(job.state)
  ));
  if (!changed.length) return;
  const announcement = changed.length === 1
    ? `${textValue(changed[0].title, "Job")} is now ${textValue(changed[0].state, "unknown")}.`
    : `${changed.length} jobs changed state.`;
  byId("status-announcement").textContent = announcement;
}

function renderKanban(jobs) {
  const target = byId("kanban");
  const navigation = byId("kanban-navigation");
  const selectedByColumn = new Map(
    columns.map((column) => [
      column.key,
      jobs.filter((job) => column.states.includes(job.state)),
    ]),
  );
  const populatedColumns = [...selectedByColumn.values()].filter((items) => items.length).length;
  const compact = Boolean(compactViewport?.matches);
  const attentionActive = byId("main-grid").dataset.attention === "active";
  const attentionColumn = columns.find((column) => column.key === "attention");
  const promoteAttention = compact
    && attentionActive
    && selectedByColumn.get("attention")?.length > 0;
  const columnOrder = promoteAttention
    ? [attentionColumn, ...columns.filter((column) => column !== attentionColumn)]
    : columns;
  const orderKey = columnOrder.map((column) => column.key).join(",");
  const boardPresentation = {
    density: populatedColumns === 0
      ? "empty"
      : populatedColumns <= 2 ? "sparse" : "active",
    columnOrder,
    orderKey,
    selectedByColumn,
  };
  target.dataset.density = boardPresentation.density;
  target.dataset.columnOrder = boardPresentation.orderKey;
  const nextSignature = JSON.stringify({
    jobs: jobs.map(jobCardSignature),
    orderKey: boardPresentation.orderKey,
  });
  const focusCapture = captureKanbanFocus(target, navigation);
  const repairFocusCapture = kanbanProgrammaticScroll?.focusCapture || focusCapture;
  const columnKeys = boardPresentation.columnOrder.map((column) => column.key);
  const activeColumnKey = (columnKeys.includes(kanbanNavigationState.activeColumnKey)
      ? kanbanNavigationState.activeColumnKey
      : null)
    || (kanbanProgrammaticScroll ? null : focusCapture?.key)
    || columnKeys[0];
  setActiveKanbanColumnKey(activeColumnKey, columnKeys);
  reconcileKanbanNavigation(boardPresentation.columnOrder, compact);
  const width = target.clientWidth;
  const layoutChanged = kanbanHasRendered && (
    kanbanLayoutCompact !== compact
    || (compact && kanbanLayoutWidth !== width)
  );
  if (kanbanHasRendered && nextSignature === kanbanSignature) {
    if (layoutChanged) {
      if (compact) {
        alignKanbanToActiveColumn(repairFocusCapture);
      } else {
        cancelKanbanProgrammaticScroll();
        target.scrollTo({ left: 0, behavior: "auto" });
        restoreKanbanFocus(repairFocusCapture, kanbanColumnElementIndex(target));
      }
    }
    kanbanLayoutCompact = compact;
    kanbanLayoutWidth = width;
    refreshJobAges();
    return;
  }
  const previousScrollLeft = target.scrollLeft;
  const orderChanged = kanbanHasRendered && kanbanOrderKey !== boardPresentation.orderKey;
  const scrollPositions = new Map(
    [...target.querySelectorAll(".kanban-column")].map((column) => [
      column.dataset.columnKey,
      column.scrollTop,
    ]),
  );
  const nextJobVisuals = new Map(
    jobs.map((job) => [textValue(job.id), jobVisualSignature(job)]),
  );
  target.innerHTML = boardPresentation.columnOrder.map((column) => {
    const selected = boardPresentation.selectedByColumn.get(column.key) || [];
    const cards = selected.length
      ? selected.map((job) => {
        const id = textValue(job.id);
        const previous = previousJobVisuals.get(id);
        const motionClass = !kanbanHasRendered
          ? ""
          : previous === undefined
            ? " is-entering"
            : previous !== nextJobVisuals.get(id)
              ? " is-state-change"
              : "";
        return jobCard(job, motionClass);
      }).join("")
      : `<div class="empty-state compact">No ${escapeHtml(column.label.toLowerCase())} jobs</div>`;
    return `
      <section class="kanban-column" tabindex="0" id="${kanbanColumnId(column.key)}" data-column-key="${escapeHtml(column.key)}" data-column-state="${selected.length ? "populated" : "empty"}" role="region" aria-label="${escapeHtml(column.label)}" aria-keyshortcuts="ArrowLeft ArrowRight">
        <div class="column-heading">
          <h2>${escapeHtml(column.label)}</h2>
          <span class="column-count">${selected.length}</span>
        </div>
        <div class="job-stack">${cards}</div>
      </section>
    `;
  }).join("");
  target.querySelectorAll(".kanban-column").forEach((column) => {
    if (scrollPositions.has(column.dataset.columnKey)) {
      column.scrollTop = scrollPositions.get(column.dataset.columnKey);
    }
  });
  const elementsByKey = kanbanColumnElementIndex(target);
  target.dataset.currentColumnState = elementsByKey.get(activeColumnKey)?.dataset.columnState || "populated";
  if (!kanbanHasRendered || orderChanged || layoutChanged) {
    alignKanbanToActiveColumn(repairFocusCapture, elementsByKey);
  } else {
    target.scrollLeft = previousScrollLeft;
    restoreKanbanFocus(focusCapture, elementsByKey);
  }
  previousJobVisuals = nextJobVisuals;
  kanbanSignature = nextSignature;
  kanbanOrderKey = boardPresentation.orderKey;
  kanbanLayoutCompact = compact;
  kanbanLayoutWidth = target.clientWidth;
  if (!kanbanHasRendered) {
    requestAnimationFrame(() => requestAnimationFrame(() => {
      target.dataset.heightMotion = "ready";
    }));
  }
  kanbanHasRendered = true;
}
function kanbanColumnId(columnKey) {
  return `kanban-column-${columnKey}`;
}

function kanbanColumnElementIndex(board = byId("kanban")) {
  return new Map(
    [...board.querySelectorAll(".kanban-column[data-column-key]")].map((column) => [
      column.dataset.columnKey,
      column,
    ]),
  );
}
function currentKanbanColumnKeys(board = byId("kanban")) {
  return (board.dataset.columnOrder || "")
    .split(",")
    .filter(Boolean);
}
function reconcileKanbanNavigation(columnOrder, compact) {
  const navigation = byId("kanban-navigation");
  navigation.hidden = !compact;
  const existing = new Map(
    [...navigation.querySelectorAll("button[data-column-key]")].map((button) => [
      button.dataset.columnKey,
      button,
    ]),
  );
  const retained = new Set();
  columnOrder.forEach((column, index) => {
    let button = existing.get(column.key);
    if (!button) {
      button = document.createElement("button");
      button.type = "button";
      button.dataset.columnKey = column.key;
    }
    button.textContent = column.label;
    button.setAttribute("aria-controls", kanbanColumnId(column.key));
    if (navigation.children[index] !== button) {
      navigation.insertBefore(button, navigation.children[index] || null);
    }
    retained.add(button);
  });
  existing.forEach((button) => {
    if (!retained.has(button)) button.remove();
  });
  setActiveKanbanColumnKey(
    kanbanNavigationState.activeColumnKey,
    columnOrder.map((column) => column.key),
  );
}
function setActiveKanbanColumnKey(columnKey, columnOrder = currentKanbanColumnKeys()) {
  if (!columnKey || !columnOrder.includes(columnKey)) return false;
  kanbanNavigationState.activeColumnKey = columnKey;
  const board = byId("kanban");
  const navigation = byId("kanban-navigation");
  board.dataset.activeColumnKey = columnKey;
  board.dataset.currentColumnState = kanbanColumnElementIndex(board).get(columnKey)?.dataset.columnState || "populated";
  const activeIndex = columnOrder.indexOf(columnKey);
  navigation.style.setProperty("--kanban-indicator-position", `${activeIndex * 100}%`);
  navigation.querySelectorAll("button[data-column-key]").forEach((button) => {
    if (button.dataset.columnKey === columnKey) {
      button.setAttribute("aria-current", "true");
    } else {
      button.removeAttribute("aria-current");
    }
  });
  return true;
}
function adjacentKanbanColumnKey(columnOrder, columnKey, direction) {
  const index = columnOrder.indexOf(columnKey);
  const nextIndex = index + direction;
  if (index < 0 || nextIndex < 0 || nextIndex >= columnOrder.length) return null;
  return columnOrder[nextIndex];
}

function kanbanReachableColumnStops(
  board = byId("kanban"),
  elementsByKey = kanbanColumnElementIndex(board),
) {
  const boardRect = board.getBoundingClientRect();
  const maxScrollLeft = Math.max(0, board.scrollWidth - board.clientWidth);
  const scrollPadding = Number.parseFloat(
    getComputedStyle(board).scrollPaddingInlineStart,
  ) || 0;
  return currentKanbanColumnKeys(board).flatMap((key) => {
    const column = elementsByKey.get(key);
    if (!column) return [];
    const left = column.getBoundingClientRect().left
      - boardRect.left
      + board.scrollLeft
      - scrollPadding;
    return [{ key, left: Math.min(maxScrollLeft, Math.max(0, left)) }];
  });
}

function nearestKanbanColumnKey(
  scrollLeft = byId("kanban").scrollLeft,
  stops = kanbanReachableColumnStops(),
) {
  if (!stops.length) return null;
  return stops.reduce((nearest, stop) => (
    Math.abs(stop.left - scrollLeft) < Math.abs(nearest.left - scrollLeft)
      ? stop
      : nearest
  )).key;
}

function captureKanbanFocus(board = byId("kanban"), navigation = byId("kanban-navigation")) {
  const active = document.activeElement;
  if (active === board) {
    return { kind: "board", key: kanbanNavigationState.activeColumnKey, element: active };
  }
  const navigationButton = active?.closest?.("#kanban-navigation button[data-column-key]");
  if (navigationButton && navigation.contains(navigationButton)) {
    return {
      kind: "navigation",
      key: navigationButton.dataset.columnKey,
      element: active,
    };
  }
  const column = active?.closest?.(".kanban-column[data-column-key]");
  if (column && board.contains(column)) {
    return {
      kind: active === column ? "column" : "descendant",
      key: column.dataset.columnKey,
      element: active,
    };
  }
  return null;
}

function restoreKanbanFocus(
  focusCapture,
  elementsByKey = kanbanColumnElementIndex(),
) {
  if (!focusCapture) return;
  const navigation = byId("kanban-navigation");
  if (
    focusCapture.element?.isConnected
    && !(focusCapture.kind === "navigation" && navigation.hidden)
  ) {
    focusCapture.element.focus({ preventScroll: true });
    return;
  }
  if (focusCapture.kind === "navigation") {
    const button = [...navigation.querySelectorAll("button[data-column-key]")]
      .find((candidate) => candidate.dataset.columnKey === focusCapture.key);
    if (!navigation.hidden && button) {
      button.focus({ preventScroll: true });
      return;
    }
    byId("kanban").focus({ preventScroll: true });
    return;
  }
  if (focusCapture.kind === "board") {
    byId("kanban").focus({ preventScroll: true });
    return;
  }
  elementsByKey.get(focusCapture.key)?.focus({ preventScroll: true });
}

function cancelKanbanProgrammaticScroll() {
  kanbanScrollGeneration += 1;
  kanbanProgrammaticScroll = null;
}

function settleKanbanProgrammaticScroll(generation) {
  const active = kanbanProgrammaticScroll;
  if (!active || active.generation !== generation) return;
  const board = byId("kanban");
  const reachedTarget = Math.abs(board.scrollLeft - active.targetLeft) <= 1;
  const timedOut = performance.now() >= active.deadline;
  if (!reachedTarget && !timedOut) {
    requestAnimationFrame(() => settleKanbanProgrammaticScroll(generation));
    return;
  }
  if (!reachedTarget) {
    board.scrollTo({ left: active.targetLeft, behavior: "auto" });
  }
  kanbanProgrammaticScroll = null;
  restoreKanbanFocus(active.focusCapture, kanbanColumnElementIndex(board));
}

function moveKanbanToColumn(
  columnKey,
  { behavior = "auto", focusCapture = null } = {},
) {
  const board = byId("kanban");
  const columnOrder = currentKanbanColumnKeys(board);
  if (!setActiveKanbanColumnKey(columnKey, columnOrder)) return false;
  const stop = kanbanReachableColumnStops(board)
    .find((candidate) => candidate.key === columnKey);
  if (!stop) return false;
  cancelKanbanProgrammaticScroll();
  const smooth = behavior === "smooth" && motionAllowed();
  const generation = ++kanbanScrollGeneration;
  if (smooth) {
    kanbanProgrammaticScroll = {
      generation,
      targetLeft: stop.left,
      deadline: performance.now() + 700,
      focusCapture,
    };
  }
  board.scrollTo({ left: stop.left, behavior: smooth ? "smooth" : "auto" });
  if (smooth) {
    requestAnimationFrame(() => settleKanbanProgrammaticScroll(generation));
  } else {
    restoreKanbanFocus(focusCapture, kanbanColumnElementIndex(board));
  }
  return true;
}

function alignKanbanToActiveColumn(
  focusCapture,
  elementsByKey = kanbanColumnElementIndex(),
) {
  const columnKey = kanbanNavigationState.activeColumnKey;
  if (!columnKey) return;
  if (!moveKanbanToColumn(columnKey, { behavior: "auto", focusCapture })) {
    restoreKanbanFocus(focusCapture, elementsByKey);
  }
}

function scheduleKanbanManualScrollObservation() {
  if (kanbanProgrammaticScroll || kanbanManualScrollFrame !== null) return;
  kanbanManualScrollFrame = requestAnimationFrame(() => {
    kanbanManualScrollFrame = null;
    if (kanbanProgrammaticScroll || !compactViewport?.matches) return;
    const nearestKey = nearestKanbanColumnKey();
    if (nearestKey) setActiveKanbanColumnKey(nearestKey);
  });
}

function kanbanKeyboardOwner(event) {
  const board = byId("kanban");
  const path = typeof event.composedPath === "function"
    ? event.composedPath()
    : [event.target];
  const origin = path[0];
  if (origin === board) {
    return { kind: "board", key: kanbanNavigationState.activeColumnKey };
  }
  if (
    origin
    && typeof origin.matches === "function"
    && origin.matches(".kanban-column")
    && origin.parentElement === board
  ) {
    return {
      kind: "column",
      key: kanbanProgrammaticScroll
        ? kanbanNavigationState.activeColumnKey
        : origin.dataset.columnKey,
    };
  }
  return null;
}

function handleKanbanResize() {
  if (!kanbanHasRendered) return;
  const board = byId("kanban");
  const compact = Boolean(compactViewport?.matches);
  const width = board.clientWidth;
  const changed = kanbanLayoutCompact !== compact
    || (compact && kanbanLayoutWidth !== width);
  kanbanLayoutCompact = compact;
  kanbanLayoutWidth = width;
  if (!changed) return;
  const focusCapture = captureKanbanFocus();
  const repairFocusCapture = kanbanProgrammaticScroll?.focusCapture || focusCapture;
  const columnOrder = currentKanbanColumnKeys(board)
    .map((key) => columns.find((column) => column.key === key))
    .filter(Boolean);
  reconcileKanbanNavigation(columnOrder, compact);
  if (compact) {
    alignKanbanToActiveColumn(repairFocusCapture);
  } else {
    cancelKanbanProgrammaticScroll();
    board.scrollTo({ left: 0, behavior: "auto" });
    restoreKanbanFocus(repairFocusCapture);
  }
}

function jobCardSignature(job) {
  const runtime = record(job.runtime) ? job.runtime : {};
  return [
    textValue(job.id),
    textValue(job.title),
    textValue(job.harness),
    textValue(job.placement),
    textValue(job.state),
    textValue(job.attempts),
    textValue(job.max_attempts),
    textValue(job.agent_name),
    textValue(job.herdr_workspace_id),
    job.agent_settled,
    job.task_verified,
    textValue(job.receipt_kind),
    textValue(job.error_code),
    textValue(job.error_summary),
    values(job.drift).map((item) => textValue(item)),
    textValue(job.updated_at),
    textValue(runtime.agent_status),
    textValue(runtime.pane_id),
  ];
}

function jobVisualSignature(job) {
  const runtime = record(job.runtime) ? job.runtime : {};
  return JSON.stringify([
    textValue(job.state),
    textValue(job.attempts),
    textValue(job.error_code),
    textValue(job.error_summary),
    job.agent_settled,
    job.task_verified,
    textValue(runtime.agent_status),
    textValue(runtime.pane_id),
  ]);
}

function jobCard(job, motionClass = "") {
  const runtime = record(job.runtime) ? job.runtime : {};
  const runtimeState = runtime.agent_status;
  const agentLabel = textValue(job.agent_name, "unassigned");
  const location = textValue(runtime.pane_id || job.herdr_workspace_id, "not observed");
  const drift = values(job.drift)
    .map((item) => escapeHtml(textValue(item).replaceAll("_", " ")))
    .join(" · ");
  const settled = job.agent_settled === true
    ? "yes"
    : job.agent_settled === false ? "no" : "pending";
  const verified = job.task_verified === true
    ? "yes"
    : job.task_verified === false
      ? "no"
      : job.receipt_kind ? "pending" : "not declared";
  return `
    <article class="job-card state-${stateClass(job.state)}${motionClass}" data-job-id="${escapeHtml(job.id)}">
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
        <dt>changed</dt><dd class="job-updated" data-epoch="${escapeHtml(job.updated_at)}">${formatAge(job.updated_at)}</dd>
      </dl>
      ${job.error_code ? `<p class="drift-line">${escapeHtml(job.error_code)}</p>` : ""}
      ${job.error_summary ? `<p class="error-summary">${escapeHtml(job.error_summary)}</p>` : ""}
      ${drift ? `<p class="drift-line">${drift}</p>` : ""}
    </article>
  `;
}

function renderAttention(items) {
  byId("main-grid").dataset.attention = items.length ? "active" : "empty";
  syncMainGridOrder();
  const count = String(items.length);
  const countTarget = byId("attention-count");
  const countChanged = countTarget.textContent !== count;
  countTarget.textContent = count;
  if (countChanged && attentionHasRendered) restartAnimation(countTarget, "is-changing");

  const nextVisuals = new Map(
    items.map((item, index) => [attentionVisualId(item, index), attentionVisualSignature(item)]),
  );
  const nextSignature = JSON.stringify([...nextVisuals]);
  if (attentionHasRendered && nextSignature === attentionSignature) return;

  byId("attention-list").innerHTML = items.length
    ? items.map((item, index) => {
      const id = attentionVisualId(item, index);
      const previous = previousAttentionVisuals.get(id);
      const motionClass = !attentionHasRendered
        ? ""
        : previous === undefined
          ? " is-new"
          : previous !== nextVisuals.get(id)
            ? " is-updated"
            : "";
      return `
      <article class="attention-item severity-${stateClass(item.severity)}${motionClass}" data-attention-id="${escapeHtml(id)}">
        <strong>${escapeHtml(item.title)}</strong>
        <span>${escapeHtml(item.code)} · ${escapeHtml(item.message)}</span>
      </article>
    `;
    }).join("")
    : '<div class="empty-state compact">No active alerts</div>';
  previousAttentionVisuals = nextVisuals;
  attentionSignature = nextSignature;
  attentionHasRendered = true;
}

function captureMainGridContinuity(elements) {
  const activeElement = document.activeElement;
  const captures = elements.map((element) => {
    const rect = element.getBoundingClientRect();
    return {
      element,
      top: rect.top,
      visibleHeight: Math.max(0, Math.min(rect.bottom, innerHeight) - Math.max(rect.top, 0)),
      containsFocus: element.contains(activeElement),
    };
  });
  const visible = captures.filter((capture) => capture.visibleHeight > 0);
  const anchor = visible.find((capture) => capture.containsFocus)
    || visible.sort((left, right) => right.visibleHeight - left.visibleHeight)[0]
    || null;
  return {
    anchor: anchor?.element || null,
    top: anchor?.top ?? null,
    focusedElement: captures.some((capture) => capture.containsFocus) ? activeElement : null,
  };
}

function restoreMainGridContinuity(capture) {
  if (capture.anchor && capture.top !== null) {
    const delta = capture.anchor.getBoundingClientRect().top - capture.top;
    if (Math.abs(delta) > 0.5) {
      window.scrollBy({ top: delta, left: 0, behavior: "instant" });
    }
  }
  if (
    capture.focusedElement?.isConnected
    && document.activeElement !== capture.focusedElement
  ) {
    capture.focusedElement.focus({ preventScroll: true });
  }
}

function syncMainGridOrder() {
  const grid = byId("main-grid");
  const board = [...(grid?.children || [])].find((child) => child.classList.contains("board-panel"));
  const rail = [...(grid?.children || [])].find((child) => child.classList.contains("right-rail"));
  if (!grid || !board || !rail) return;
  const attentionFirst = compactViewport?.matches && grid.dataset.attention === "active";
  const desiredFirst = attentionFirst ? rail : board;
  const nextOrderKey = attentionFirst ? "attention-first" : "board-first";
  const orderChanged = mainGridOrderKey && mainGridOrderKey !== nextOrderKey;
  if (grid.firstElementChild !== desiredFirst) {
    const continuity = captureMainGridContinuity([board, rail]);
    grid.insertBefore(desiredFirst, grid.firstElementChild);
    restoreMainGridContinuity(continuity);
    if (orderChanged) restartAnimation(desiredFirst, "is-order-changing", [board, rail], () => mainGridOrderKey === nextOrderKey);
  }
  mainGridOrderKey = nextOrderKey;
}

function handleCompactViewportChange() {
  syncMainGridOrder();
  if (currentSnapshot) renderKanban(records(currentSnapshot.jobs));
}

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

function attentionVisualId(item, index) {
  const fallback = `${textValue(item.code, "attention")}:${textValue(item.title, index)}`;
  return textValue(item.job_id, fallback);
}

function attentionVisualSignature(item) {
  return JSON.stringify([
    textValue(item.severity),
    textValue(item.title),
    textValue(item.code),
    textValue(item.message),
  ]);
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

function renderTimeline(events) {
  const target = byId("timeline");
  const visible = events.slice(0, 24);
  const nextVisuals = new Map(
    visible.map((event, index) => [timelineVisualId(event, index), timelineVisualSignature(event)]),
  );
  const nextSignature = JSON.stringify([...nextVisuals]);
  if (timelineHasRendered && nextSignature === timelineSignature) return;
  const continuity = timelineHasRendered ? captureTimelineContinuity(target) : null;
  target.innerHTML = visible.length
    ? visible.map((event, index) => {
      const id = timelineVisualId(event, index);
      const previous = previousTimelineVisuals.get(id);
      const motionClass = !timelineHasRendered
        ? ""
        : previous === undefined
          ? " is-new"
          : previous !== nextVisuals.get(id)
            ? " is-updated"
            : "";
      return `
      <article class="timeline-event state-${stateClass(event.state)}${motionClass}" data-event-id="${escapeHtml(id)}">
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
    `;
    }).join("")
    : '<div class="empty-state compact">No lifecycle events yet</div>';
  restoreTimelineContinuity(target, continuity, { animate: motionAllowed() });
  previousTimelineVisuals = nextVisuals;
  timelineSignature = nextSignature;
  timelineHasRendered = true;
}
function timelineVisualId(event, index) {
  const fallback = `${textValue(event.at, "event")}:${textValue(event.type, String(index))}`;
  return textValue(event.id, fallback);
}
function timelineVisualSignature(event) {
  return JSON.stringify([
    textValue(event.title),
    textValue(event.type),
    textValue(event.state),
    textValue(event.attempt),
    textValue(event.detail),
    textValue(event.error_code),
  ]);
}

async function loadInitial() {
  try {
    const response = await fetch("/api/snapshot", { cache: "no-store" });
    if (!response.ok) throw new Error(`snapshot ${response.status}`);
    const payload = await response.json();
    if (currentSnapshot !== null) return;
    render(payload.snapshot);
    if (recoveryState.browserTransport.kind === "open") {
      setConnection("is-live", "Live");
    }
  } catch (_error) {
    if (recoveryState.browserTransport.kind === "connecting") {
      showUnavailableState("Initial snapshot unavailable. Retrying live stream.");
    }
  }
}

function connectEvents() {
  let events;
  try {
    events = new EventSource("/api/events");
  } catch (_error) {
    showUnavailableState("Live snapshot stream unavailable; reconnecting");
    return;
  }
  events.addEventListener("open", () => {
    recoveryState = reduceRecoveryState(recoveryState, { type: "transport-open" });
    setSourceWarning(
      sourceWarningMessage(recoveryState.browserTransport, currentSnapshot),
    );
    if (currentSnapshot) {
      setConnection("is-live", "Live");
    } else {
      setConnection(null, "Connected");
      byId("last-updated").textContent = "Waiting for first snapshot";
    }
  });
  events.addEventListener("snapshot", (event) => {
    try {
      const snapshot = JSON.parse(event.data);
      recoveryState = reduceRecoveryState(recoveryState, { type: "snapshot-accepted" });
      render(snapshot);
      setConnection("is-live", "Live");
    } catch (_error) {
      showUnavailableState("Snapshot stream unavailable; reconnecting");
    }
  });
  events.addEventListener("error", () => {
    showUnavailableState("Live snapshot stream unavailable; reconnecting");
  });
}

byId("topology-zoom-out").addEventListener("click", () => zoomTopology("out"));
byId("topology-fit").addEventListener(
  "click",
  () => {
    fitTopology({ user: true, animate: true, origin: "toolbar" });
  },
);
byId("topology-zoom-in").addEventListener("click", () => zoomTopology("in"));
byId("topology-touch-owner").addEventListener("click", () => {
  setTopologyTouchOwner(
    topologyTouchOwnershipState.coarseOwner === "graph" ? "page" : "graph",
  );
});
byId("topology").addEventListener("keydown", (event) => {
  const selectionDirection = topologySelectionDirection(event);
  if (selectionDirection !== null) {
    event.preventDefault();
    cycleTopologySelection(selectionDirection);
    return;
  }
  if (event.key === "Escape" && topologyNavigationState.selectedNodeId) {
    event.preventDefault();
    clearTopologySelection({ announce: true });
    return;
  }
  if (event.key === "+" || event.key === "=") {
    event.preventDefault();
    zoomTopology("in", { origin: "canvas-keyboard" });
  } else if (event.key === "-") {
    event.preventDefault();
    zoomTopology("out", { origin: "canvas-keyboard" });
  } else if (event.key === "0" || event.key === "Home") {
    event.preventDefault();
    fitTopology({ user: true, animate: true, origin: "canvas-keyboard" });
  } else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(event.key)) {
    event.preventDefault();
    const step = event.shiftKey ? 180 : 96;
    const delta = {
      ArrowLeft: [-step, 0],
      ArrowRight: [step, 0],
      ArrowUp: [0, -step],
      ArrowDown: [0, step],
    }[event.key];
    panTopology(delta[0], delta[1]);
  }
});

byId("kanban-navigation").addEventListener("click", (event) => {
  const button = event.target.closest?.("button[data-column-key]");
  if (!button || !event.currentTarget.contains(button)) return;
  moveKanbanToColumn(button.dataset.columnKey, {
    behavior: motionAllowed() ? "smooth" : "auto",
  });
});
byId("kanban-navigation").addEventListener("keydown", (event) => {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  const button = event.target.closest?.("button[data-column-key]");
  if (!button || !event.currentTarget.contains(button)) return;
  const direction = event.key === "ArrowRight" ? 1 : -1;
  const nextKey = adjacentKanbanColumnKey(
    currentKanbanColumnKeys(),
    button.dataset.columnKey,
    direction,
  );
  event.preventDefault();
  if (!nextKey) return;
  const nextButton = [...event.currentTarget.querySelectorAll("button[data-column-key]")]
    .find((candidate) => candidate.dataset.columnKey === nextKey);
  nextButton?.focus({ preventScroll: true });
  moveKanbanToColumn(nextKey, {
    behavior: motionAllowed() ? "smooth" : "auto",
  });
});
byId("kanban").addEventListener("focusin", (event) => {
  const column = event.target.closest?.(".kanban-column[data-column-key]");
  if (!compactViewport?.matches || column?.parentElement !== event.currentTarget) return;
  moveKanbanToColumn(column.dataset.columnKey, { focusCapture: { kind: "column", key: column.dataset.columnKey } });
});
byId("kanban").addEventListener("keydown", (event) => {
  if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
  const owner = kanbanKeyboardOwner(event);
  if (!owner) return;
  const direction = event.key === "ArrowRight" ? 1 : -1;
  const nextKey = adjacentKanbanColumnKey(
    currentKanbanColumnKeys(),
    owner.key,
    direction,
  );
  event.preventDefault();
  if (!nextKey) return;
  moveKanbanToColumn(nextKey, {
    behavior: motionAllowed() ? "smooth" : "auto",
    focusCapture: owner.kind === "column"
      ? { kind: "column", key: nextKey }
      : { kind: "board", key: nextKey },
  });
});
byId("kanban").addEventListener(
  "scroll",
  scheduleKanbanManualScrollObservation,
  { passive: true },
);
["pointerdown", "touchstart", "wheel"].forEach((eventName) => {
  byId("kanban").addEventListener(eventName, () => {
    cancelKanbanProgrammaticScroll();
    scheduleKanbanManualScrollObservation();
  }, { passive: true });
});
if (compactViewport) {
  if (typeof compactViewport.addEventListener === "function") {
    compactViewport.addEventListener("change", handleCompactViewportChange);
  } else if (typeof compactViewport.addListener === "function") {
    compactViewport.addListener(handleCompactViewportChange);
  }
}

if (typeof ResizeObserver === "function") {
  const kanbanResizeObserver = new ResizeObserver(handleKanbanResize);
  kanbanResizeObserver.observe(byId("kanban"));
}

if (primaryCoarsePointer) {
  if (typeof primaryCoarsePointer.addEventListener === "function") {
    primaryCoarsePointer.addEventListener("change", syncTopologyTouchOwnership);
  } else if (typeof primaryCoarsePointer.addListener === "function") {
    primaryCoarsePointer.addListener(syncTopologyTouchOwnership);
  }
}

syncTopologyTouchOwnership();
loadInitial();
connectEvents();
setInterval(() => {
  if (currentSnapshot && !recoveryState.awaitingFreshSnapshot) {
    byId("last-updated").textContent = `Updated ${formatAge(currentSnapshot.generated_at)}`;
    refreshJobAges();
  }
}, 1000);
