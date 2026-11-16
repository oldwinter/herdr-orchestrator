"use strict";

const byId = (id) => document.getElementById(id);
const connection = document.querySelector(".connection");
const compactViewport = window.matchMedia?.("(max-width: 760px)");
const primaryCoarsePointer = window.matchMedia?.("(pointer: coarse)") || null;
let mainGridOrderKey = "";
let currentSnapshot = null;
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
let recoveryState = { browserTransport: { kind: "connecting" }, awaitingFreshSnapshot: false };
const dataRegionIds = ["kanban", "attention-list", "topology", "timeline"];
const KANBAN_COLUMN_CARD_LIMIT = 200;
const columns = [
  { key: "queued", label: "Queued", states: ["pending"] },
  { key: "working", label: "In motion", states: ["running"] },
  { key: "attention", label: "Attention", states: ["blocked", "failed"] },
  { key: "finished", label: "Finished", states: ["succeeded"] },
];

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
      ? selected.slice(0, KANBAN_COLUMN_CARD_LIMIT).map((job) => (
        jobCard(job, kanbanCardMotion(job, nextJobVisuals))
      )).join("") + overflowNote(selected.length - KANBAN_COLUMN_CARD_LIMIT)
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
function kanbanCardMotion(job, nextJobVisuals) {
  if (!kanbanHasRendered) return "";
  const id = textValue(job.id);
  const previous = previousJobVisuals.get(id);
  return previous === undefined
    ? " is-entering"
    : previous !== nextJobVisuals.get(id) ? " is-state-change" : "";
}
function overflowNote(hidden) {
  return hidden > 0 ? `<div class="overflow-note">+${hidden} more</div>` : "";
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

function renderTimeline(events) {
  const target = byId("timeline");
  const visible = events.slice(0, 24);
  const nextVisuals = new Map(
    visible.map((event, index) => [timelineVisualId(event, index), timelineVisualSignature(event)]),
  );
  const nextSignature = JSON.stringify([events.length, ...nextVisuals]);
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
    }).join("") + overflowNote(events.length - visible.length)
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
