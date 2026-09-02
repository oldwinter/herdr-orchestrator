from __future__ import annotations

import json
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from contextlib import closing
from copy import deepcopy
from dataclasses import replace
from http.client import HTTPConnection
from pathlib import Path
from urllib.request import urlopen

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.dashboard.observer import (
    HerdrObservation,
    HerdrObserver,
    QueueObservation,
    SqliteObserver,
)
from herdr_orchestrator.dashboard.projector import RuntimeProjector
from herdr_orchestrator.dashboard.server import DashboardServer, SnapshotFeed
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    NewJob,
    PlacementTarget,
    ReceiptKind,
    TaskReceipt,
)
from herdr_orchestrator.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]


class FakeQueueObserver:
    def __init__(self, observation: QueueObservation) -> None:
        self.observation = observation

    def observe(self) -> QueueObservation:
        return self.observation


class FakeHerdrObserver:
    def __init__(self, observation: HerdrObservation) -> None:
        self.observation = observation

    def observe(self) -> HerdrObservation:
        return self.observation


class FakeProjector:
    def __init__(self, snapshot: dict[str, object]) -> None:
        self.value = snapshot

    def snapshot(self) -> dict[str, object]:
        return self.value


class DashboardTests(unittest.TestCase):
    def test_server_does_not_create_missing_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            path = Path(temporary) / "missing" / "state.db"
            config = replace(base, state_db=path)

            with self.assertRaisesRegex(ValueError, "dashboard_state_db_not_found"):
                DashboardServer(config, port=0, projector=FakeProjector({}))

            self.assertFalse(path.exists())
            self.assertFalse(path.parent.exists())

    def test_server_does_not_migrate_incompatible_state_db(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            path = Path(temporary) / "legacy.db"
            Store(path).initialize()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("UPDATE schema_meta SET version = 3")

            config = replace(base, state_db=path)
            server = None
            try:
                with self.assertRaisesRegex(ValueError, "dashboard_state_db_incompatible"):
                    server = DashboardServer(config, port=0, projector=FakeProjector({}))
            finally:
                if server is not None:
                    server.httpd.server_close()

            with closing(sqlite3.connect(path)) as connection, connection:
                version = connection.execute("SELECT version FROM schema_meta").fetchone()[0]
            self.assertEqual(version, 3)

    def test_server_opens_existing_state_db_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            path = Path(temporary) / "readonly.db"
            Store(path).initialize()
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute("PRAGMA journal_mode = DELETE")
            before = path.stat().st_mtime_ns
            path.chmod(0o444)
            Path(temporary).chmod(0o555)
            config = replace(base, state_db=path)
            server = None
            try:
                server = DashboardServer(config, port=0, projector=FakeProjector({}))
            finally:
                if server is not None:
                    server.httpd.server_close()
                Path(temporary).chmod(0o700)
                path.chmod(0o644)

            self.assertEqual(path.stat().st_mtime_ns, before)
            self.assertFalse(path.with_name(f"{path.name}-wal").exists())
            self.assertFalse(path.with_name(f"{path.name}-journal").exists())

    def test_source_warning_recovery_preserves_layout_continuity(self) -> None:
        static = REPO_ROOT / "src/herdr_orchestrator/dashboard/static"
        index = (static / "index.html").read_text()
        dashboard_script = (static / "dashboard.js").read_text()
        warning_script = (static / "source-warning.js").read_text()
        dashboard_css = (static / "dashboard.css").read_text()

        warning_start = index.index('id="source-warning-region"')
        warning_markup = index[warning_start : index.index("</section>", warning_start)]
        self.assertIn('class="source-warning-region is-hidden"', warning_markup)
        self.assertIn('aria-hidden="true"', warning_markup)
        self.assertIn('class="source-warning-clip"', warning_markup)
        self.assertIn('id="source-warning" class="source-warning"', warning_markup)

        warning_style = dashboard_css[
            dashboard_css.index(".source-warning-region {") : dashboard_css.index(".metrics {")
        ]
        self.assertIn("grid-template-rows: 1fr;", warning_style)
        self.assertIn(
            "transition-property: grid-template-rows, opacity;",
            warning_style,
        )
        self.assertIn(
            ".source-warning-region.is-hidden {\n"
            "  display: grid;\n"
            "  grid-template-rows: 0fr;",
            warning_style,
        )
        self.assertIn(
            "min-height: 0;\n" "  height: auto;\n" "  overflow: hidden;",
            warning_style,
        )
        self.assertNotIn("warning-enter", dashboard_css)

        warning_writer = warning_script
        self.assertIn("function setSourceWarning", warning_writer)
        self.assertIn('const region = byId("source-warning-region");', warning_writer)
        self.assertIn('const warning = byId("source-warning");', warning_writer)
        self.assertLess(
            warning_writer.index('region.setAttribute("aria-hidden", "true")'),
            warning_writer.index('region.classList.add("is-hidden")'),
        )
        self.assertLess(
            warning_writer.index('region.removeAttribute("aria-hidden")'),
            warning_writer.index('region.classList.remove("is-hidden")'),
        )

        render = dashboard_script[
            dashboard_script.index("function render(snapshot)") : dashboard_script.index(
                "function announceStateChange"
            )
        ]
        self.assertIn(
            "setSourceWarning(\n"
            "    sourceWarningMessage(recoveryState.browserTransport, snapshot),\n"
            "  );",
            render,
        )
        self.assertNotIn("const parts = []", render)
        self.assertNotIn("warning.classList", render)

        initial = dashboard_script[
            dashboard_script.index("async function loadInitial") : dashboard_script.index(
                "function connectEvents"
            )
        ]
        self.assertEqual(initial.count("setSourceWarning("), 0)
        self.assertIn(
            'showUnavailableState("Initial snapshot unavailable. Retrying live stream.");',
            initial,
        )

        reduced = dashboard_css[dashboard_css.index("@media (prefers-reduced-motion: reduce)") :]
        self.assertIn(
            ".source-warning-region,\n"
            "  .source-warning-region.is-hidden,\n"
            "  .source-warning {\n"
            "    transform: none;\n"
            "    transition: none;\n"
            "  }",
            reduced,
        )

    def test_reconnect_recovery_wiring_preserves_freshness_and_startup_order(self) -> None:
        dashboard_script = (
            REPO_ROOT / "src/herdr_orchestrator/dashboard/static/dashboard.js"
        ).read_text()

        unavailable = dashboard_script[
            dashboard_script.index("function showUnavailableState") : dashboard_script.index(
                "function render(snapshot)"
            )
        ]
        self.assertIn('type: "transport-error", warning: message', unavailable)
        self.assertLess(
            unavailable.index('setConnection("is-offline", "Reconnecting")'),
            unavailable.index("setSourceWarning("),
        )
        self.assertIn(
            "sourceWarningMessage(recoveryState.browserTransport, currentSnapshot)",
            unavailable,
        )
        self.assertLess(
            unavailable.index("const repeatedInitialError"),
            unavailable.index("recoveryState = reduceRecoveryState"),
        )
        self.assertLess(
            unavailable.index("if (repeatedInitialError) return;"),
            unavailable.index('byId("kanban").innerHTML'),
        )

        initial = dashboard_script[
            dashboard_script.index("async function loadInitial") : dashboard_script.index(
                "function connectEvents"
            )
        ]
        self.assertLess(
            initial.index("if (currentSnapshot !== null) return;"),
            initial.index("render(payload.snapshot);"),
        )
        self.assertLess(
            initial.index("render(payload.snapshot);"),
            initial.index('recoveryState.browserTransport.kind === "open"'),
        )
        self.assertIn('setConnection("is-live", "Live")', initial)
        self.assertIn(
            'recoveryState.browserTransport.kind === "connecting"',
            initial,
        )
        self.assertNotIn('type: "snapshot-accepted"', initial)

        open_listener = dashboard_script[
            dashboard_script.index('events.addEventListener("open"') : dashboard_script.index(
                'events.addEventListener("snapshot"'
            )
        ]
        self.assertIn('type: "transport-open"', open_listener)
        self.assertLess(
            open_listener.index("setSourceWarning("),
            open_listener.index("setConnection("),
        )
        self.assertNotIn("render(", open_listener)
        self.assertNotIn("awaitingFreshSnapshot =", open_listener)

        snapshot_listener = dashboard_script[
            dashboard_script.index('events.addEventListener("snapshot"') : dashboard_script.index(
                'events.addEventListener("error"'
            )
        ]
        self.assertLess(
            snapshot_listener.index('type: "snapshot-accepted"'),
            snapshot_listener.index("render(snapshot)"),
        )
        self.assertLess(
            snapshot_listener.index("render(snapshot)"),
            snapshot_listener.index('setConnection("is-live", "Live")'),
        )

        updater = dashboard_script[dashboard_script.index("setInterval(() =>") :]
        self.assertIn(
            "currentSnapshot && !recoveryState.awaitingFreshSnapshot",
            updater,
        )
        self.assertNotIn("transportError", updater)
        self.assertLessEqual(len(dashboard_script.splitlines()), 2000)

    def test_topology_inspector_exit_preserves_visual_continuity(self) -> None:
        static = REPO_ROOT / "src/herdr_orchestrator/dashboard/static"
        index = (static / "index.html").read_text()
        dashboard_script = (static / "dashboard.js").read_text()
        dashboard_css = (static / "dashboard.css").read_text()

        inspector_start = index.index('id="topology-inspector"')
        inspector_markup = index[inspector_start : index.index("</div>", inspector_start)]
        self.assertIn('aria-hidden="true"', inspector_markup)

        inspector_style = dashboard_css[
            dashboard_css.index(".topology-inspector {") : dashboard_css.index(
                ".topology-inspector strong"
            )
        ]
        self.assertIn(
            "transition-property: opacity, transform, clip-path, display;",
            inspector_style,
        )
        self.assertIn(
            "transition-behavior: normal, normal, normal, allow-discrete;",
            inspector_style,
        )
        self.assertIn("transition-duration: 160ms, 160ms, 160ms, 160ms;", inspector_style)
        self.assertIn(
            "transition-timing-function: cubic-bezier(0.4, 0, 1, 1);",
            inspector_style,
        )
        self.assertIn("@starting-style", dashboard_css)
        self.assertNotIn("@keyframes inspector-enter", dashboard_css)

        render_inspector = dashboard_script[
            dashboard_script.index("function renderTopologyInspector") : dashboard_script.index(
                "function clearTopologySelection"
            )
        ]
        self.assertLess(
            render_inspector.index('inspector.removeAttribute("aria-hidden")'),
            render_inspector.index('inspector.classList.remove("is-hidden")'),
        )

        clear_inspector = dashboard_script[
            dashboard_script.index("function clearTopologySelection") : dashboard_script.index(
                "function handleTopologyResize"
            )
        ]
        self.assertLess(
            clear_inspector.index('inspector.setAttribute("aria-hidden", "true")'),
            clear_inspector.index('inspector.classList.add("is-hidden")'),
        )
        self.assertNotIn('inspector.innerHTML = ""', clear_inspector)

        reduced = dashboard_css[dashboard_css.index("@media (prefers-reduced-motion: reduce)") :]
        self.assertIn(
            "transition-property: color, border-color, background-color, opacity, display;",
            reduced,
        )
        self.assertNotIn(
            "transition-property: color, border-color, background-color, opacity, "
            "transform, display;",
            reduced,
        )

    def test_source_warning_message_resize_preserves_intrinsic_continuity(self) -> None:
        static = REPO_ROOT / "src/herdr_orchestrator/dashboard/static"
        index = (static / "index.html").read_text()
        warning_script = (static / "source-warning.js").read_text()
        dashboard_css = (static / "dashboard.css").read_text()

        self.assertIn('id="source-warning-clip"', index)
        self.assertIn("source-warning.js", index)

        clip_style = dashboard_css[
            dashboard_css.index(".source-warning-clip {") : dashboard_css.index(".source-warning {")
        ]
        self.assertIn("overflow: hidden;", clip_style)
        self.assertIn("transition-property: height;", clip_style)
        self.assertIn("transition-duration: 240ms;", clip_style)
        self.assertIn(
            "transition-timing-function: cubic-bezier(0.16, 1, 0.3, 1);",
            clip_style,
        )

        warning_writer = warning_script
        self.assertIn("function setSourceWarning", warning_writer)
        self.assertIn("function sourceWarningIntrinsicHeight", warning_writer)
        self.assertIn("function cancelSourceWarningResize", warning_writer)
        self.assertIn("function animateSourceWarningResize", warning_writer)
        self.assertIn('const clip = byId("source-warning-clip");', warning_writer)
        self.assertIn("warning.getBoundingClientRect().height", warning_writer)
        self.assertIn("sourceWarningResizeState.operation === operation", warning_writer)
        self.assertIn("sourceWarningResizeState.operation !== operation", warning_writer)
        self.assertIn("clip.style.height = `${currentHeight}px`", warning_writer)
        self.assertIn("clip.style.height = `${nextHeight}px`", warning_writer)
        self.assertIn('clip.style.removeProperty("height")', warning_writer)
        self.assertIn('event.propertyName === "height"', warning_writer)
        self.assertIn("cancelSourceWarningResize();", warning_writer)
        self.assertNotIn('addEventListener("transitioncancel"', warning_writer)
        self.assertNotIn("max-height", warning_writer)

        reduced = dashboard_css[dashboard_css.index("@media (prefers-reduced-motion: reduce)") :]
        self.assertIn(
            ".source-warning-clip {\n    transition: none;\n  }",
            reduced,
        )

    def test_summary_names_git_worktrees_separately_from_topology_nodes(self) -> None:
        static = REPO_ROOT / "src/herdr_orchestrator/dashboard/static"
        index = (static / "index.html").read_text()
        dashboard_script = (static / "dashboard.js").read_text()

        metric_start = index.index('<strong id="metric-worktrees">')
        metric = index[
            index.rfind("<article", 0, metric_start) : index.index("</article>", metric_start)
        ]
        self.assertIn('<span class="metric-label">Git worktrees</span>', metric)
        self.assertIn("native isolated checkouts", metric)
        self.assertNotIn('<span class="metric-label">Worktrees</span>', metric)
        self.assertNotIn('<span class="metric-label">Linked worktrees</span>', metric)
        self.assertIn('setMetric("metric-worktrees", summary.worktrees)', dashboard_script)

    def test_empty_current_mobile_kanban_compacts_with_interruptible_motion(self) -> None:
        static = REPO_ROOT / "src/herdr_orchestrator/dashboard/static"
        dashboard_script = (static / "dashboard.js").read_text()
        dashboard_css = (static / "dashboard.css").read_text()

        render = dashboard_script[
            dashboard_script.index("function renderKanban") : dashboard_script.index(
                "function kanbanColumnId"
            )
        ]
        self.assertIn(
            'data-column-state="${selected.length ? "populated" : "empty"}"',
            render,
        )
        self.assertIn(
            "target.dataset.currentColumnState = elementsByKey.get(activeColumnKey)",
            render,
        )
        self.assertIn('target.dataset.heightMotion = "ready"', render)
        self.assertGreaterEqual(render.count("requestAnimationFrame"), 2)

        active_projection = dashboard_script[
            dashboard_script.index("function setActiveKanbanColumnKey") : dashboard_script.index(
                "function adjacentKanbanColumnKey"
            )
        ]
        self.assertIn("board.dataset.currentColumnState", active_projection)
        self.assertIn("kanbanColumnElementIndex(board).get(columnKey)", active_projection)

        mobile = dashboard_css[
            dashboard_css.index("@media (max-width: 760px)") : dashboard_css.index(
                "@media (pointer: coarse)"
            )
        ]
        empty_current = mobile[
            mobile.index('.kanban[data-current-column-state="empty"]') : mobile.index(
                ".kanban-column"
            )
        ]
        self.assertIn("height: 176px;", empty_current)
        self.assertIn("min-height: 176px;", empty_current)

        compact_motion = dashboard_css[
            dashboard_css.index(
                "@media (max-width: 760px) and (prefers-reduced-motion: no-preference)"
            ) : dashboard_css.index("@media (prefers-reduced-motion: no-preference)")
        ]
        self.assertIn('.kanban[data-height-motion="ready"]', compact_motion)
        self.assertIn("height 240ms cubic-bezier(0.16, 1, 0.3, 1)", compact_motion)
        self.assertIn("min-height 240ms cubic-bezier(0.16, 1, 0.3, 1)", compact_motion)

        reduced = dashboard_css[dashboard_css.index("@media (prefers-reduced-motion: reduce)") :]
        self.assertIn(
            ".kanban {\n    transition: none;\n    scroll-behavior: auto;\n  }",
            reduced,
        )

    def test_icon_button_state_motion_respects_reduced_motion(self) -> None:
        dashboard_css = (
            REPO_ROOT / "src/herdr_orchestrator/dashboard/static/dashboard.css"
        ).read_text()

        icon_button = dashboard_css[
            dashboard_css.index(".icon-button {") : dashboard_css.index(".icon-button svg")
        ]
        self.assertIn(
            "transition-property: border-color, background-color, color, opacity;",
            icon_button,
        )
        self.assertIn("transition-duration: 160ms;", icon_button)
        self.assertIn(
            "transition-timing-function: cubic-bezier(0.16, 1, 0.3, 1);",
            icon_button,
        )

        no_preference = dashboard_css[
            dashboard_css.index(
                "@media (prefers-reduced-motion: no-preference)"
            ) : dashboard_css.index("@media (prefers-reduced-motion: reduce)")
        ]
        self.assertIn(
            ".icon-button {\n"
            "    transition-property: border-color, background-color, color, "
            "opacity, transform;\n"
            "    transition-duration: 160ms, 160ms, 160ms, 160ms, 120ms;\n"
            "  }",
            no_preference,
        )
        self.assertIn(".icon-button:active {\n    transform: scale(0.94);\n  }", no_preference)

        reduced = dashboard_css[dashboard_css.index("@media (prefers-reduced-motion: reduce)") :]
        self.assertNotIn(".icon-button:active", reduced)
        self.assertIn(
            "  .job-detail dd[title] {\n"
            "    white-space: normal;\n"
            "    overflow-wrap: anywhere;\n"
            "  }",
            dashboard_css,
        )
        self.assertLess(
            dashboard_css.index("@media (max-width: 340px)"),
            dashboard_css.index(".job-detail dd[title]"),
        )

    def test_fit_control_fails_closed_without_topology(self) -> None:
        static = REPO_ROOT / "src/herdr_orchestrator/dashboard/static"
        index = (static / "index.html").read_text()
        dashboard_script = (static / "dashboard.js").read_text()

        fit_start = index.index('id="topology-fit"')
        fit_button = index[fit_start : index.index("</button>", fit_start)]
        self.assertIn("disabled", fit_button)
        self.assertIn('aria-label="Fit topology"', fit_button)
        self.assertIn('title="Fit topology"', fit_button)
        for path in (
            "M3 7V5a2 2 0 0 1 2-2h2",
            "M17 3h2a2 2 0 0 1 2 2v2",
            "M21 17v2a2 2 0 0 1-2 2h-2",
            "M7 21H5a2 2 0 0 1-2-2v-2",
        ):
            self.assertIn(f'<path d="{path}"></path>', fit_button)
        self.assertNotIn("m3 11 9-8 9 8", fit_button)
        self.assertNotIn("M9 20v-6h6v6", fit_button)

        content_reader = dashboard_script[
            dashboard_script.index("function readTopologyContentState()") : dashboard_script.index(
                "function readTopologyZoomState()"
            )
        ]
        self.assertIn("topologyCanvas.elements()", content_reader)
        self.assertIn('kind: "unavailable"', content_reader)
        self.assertIn('kind: "ready"', content_reader)
        self.assertNotIn("hasTopology", dashboard_script)

        fit_projector = dashboard_script[
            dashboard_script.index("function syncTopologyFitControl()") : dashboard_script.index(
                "function syncTopologyZoomControls()"
            )
        ]
        self.assertIn('byId("topology-fit")', fit_projector)
        self.assertIn("readTopologyContentState()", fit_projector)
        self.assertIn("fitUnavailable", fit_projector)

        fit_writer = dashboard_script[
            dashboard_script.index("function fitTopology") : dashboard_script.index(
                "function zoomTopology"
            )
        ]
        self.assertIn('origin = "programmatic"', fit_writer)
        self.assertIn('origin === "canvas-keyboard"', fit_writer)
        self.assertIn('"No topology to fit."', dashboard_script)
        for camera_operation in (
            "recordTopologyViewportSize()",
            "getFitViewport",
            "claimTopologyViewport",
            "setTopologyViewport",
        ):
            self.assertLess(
                fit_writer.index("readTopologyContentState()"),
                fit_writer.index(camera_operation),
            )

        empty_render = dashboard_script[
            dashboard_script.index("if (!graph.elements.length)") : dashboard_script.index(
                "const canvas = ensureTopologyCanvas()"
            )
        ]
        self.assertIn("syncTopologyFitControl();", empty_render)
        populated_render = dashboard_script[
            dashboard_script.index("canvas.batch(() =>") : dashboard_script.index(
                "topologyContentSignature = graph.contentSignature"
            )
        ]
        self.assertIn("syncTopologyFitControl();", populated_render)

        keyboard_handler = dashboard_script[
            dashboard_script.index(
                'byId("topology").addEventListener("keydown"'
            ) : dashboard_script.index('byId("kanban-navigation").addEventListener')
        ]
        self.assertIn('origin: "canvas-keyboard"', keyboard_handler)

    def test_zoom_boundary_controls_follow_camera_intent(self) -> None:
        static = REPO_ROOT / "src/herdr_orchestrator/dashboard/static"
        index = (static / "index.html").read_text()
        dashboard_script = (static / "dashboard.js").read_text()
        dashboard_css = (static / "dashboard.css").read_text()

        for button_id in ("topology-zoom-out", "topology-zoom-in"):
            button_start = index.index(f'id="{button_id}"')
            button = index[button_start : index.index("</button>", button_start)]
            self.assertIn("disabled", button)

        zoom_state = dashboard_script[
            dashboard_script.index("function readTopologyZoomState()") : dashboard_script.index(
                "function syncTopologyZoomControls()"
            )
        ]
        self.assertIn('active?.purpose === "zoom"', zoom_state)
        self.assertIn("active.target.zoom", zoom_state)
        self.assertIn("topologyCanvas.minZoom()", zoom_state)
        self.assertIn("topologyCanvas.maxZoom()", zoom_state)
        self.assertNotIn("atMin", zoom_state)
        self.assertNotIn("atMax", zoom_state)

        viewport_listener = dashboard_script[
            dashboard_script.index('topologyCanvas.on("viewport"') : dashboard_script.index(
                '["dragpan", "scrollzoom", "pinchzoom"]'
            )
        ]
        self.assertLess(
            viewport_listener.index("syncTopologyZoomControls();"),
            viewport_listener.index("topologyViewportState.programmaticWriteDepth"),
        )

        viewport_writer = dashboard_script[
            dashboard_script.index("function setTopologyViewport") : dashboard_script.index(
                "function claimTopologyViewport"
            )
        ]
        self.assertGreaterEqual(viewport_writer.count("syncTopologyZoomControls();"), 4)
        self.assertLess(
            viewport_writer.index(
                "topologyViewportState.motion.active = "
                "{ generation, handle, purpose, target: next };"
            ),
            viewport_writer.index("handle.play();"),
        )
        self.assertIn(
            "withProgrammaticViewportWrite(() => active.handle.stop());",
            viewport_writer,
        )

        zoom_writer = dashboard_script[
            dashboard_script.index("function zoomTopology") : dashboard_script.index(
                "function panTopology"
            )
        ]
        self.assertIn('origin = "toolbar"', zoom_writer)
        self.assertIn('origin === "canvas-keyboard"', zoom_writer)
        self.assertIn('"Maximum zoom reached."', dashboard_script)
        self.assertIn('"Minimum zoom reached."', dashboard_script)
        self.assertIn("replaceChildren(document.createTextNode(message))", dashboard_script)
        self.assertLess(
            zoom_writer.index("getZoomedViewport"),
            zoom_writer.index("claimTopologyViewport()"),
        )

        self.assertIn(
            ".icon-button:disabled,\n.icon-button:disabled:hover {",
            dashboard_css,
        )
        disabled_style = dashboard_css[
            dashboard_css.index(".icon-button:disabled,") : dashboard_css.index(
                "}", dashboard_css.index(".icon-button:disabled,")
            )
        ]
        self.assertIn("opacity:", disabled_style)
        self.assertIn("cursor: not-allowed", disabled_style)

    def test_sqlite_observer_never_reads_prompt_content(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            store = Store(path)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow="example",
                    title="Safe title",
                    harness=Harness.CODEX,
                    prompt="TOP SECRET PROMPT",
                    dedupe_key="secret-v1",
                    max_attempts=2,
                    placement=PlacementTarget.TAB,
                )
            )

            observation = SqliteObserver(path, "example").observe()
            serialized = json.dumps(observation.jobs)

        self.assertNotIn("prompt", observation.jobs[0])
        self.assertNotIn("TOP SECRET PROMPT", serialized)

    def test_sqlite_observer_handles_uri_reserved_path_characters(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state?query.db"
            store = Store(path)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow="example",
                    title="Safe title",
                    harness=Harness.CODEX,
                    prompt="Read only.",
                    dedupe_key="reserved-path-v1",
                    max_attempts=1,
                )
            )

            observation = SqliteObserver(path, "example").observe()

        self.assertEqual([row["title"] for row in observation.jobs], ["Safe title"])

    def test_sqlite_observer_excludes_receipt_values_and_transcripts(self) -> None:
        prompt = "TOP SECRET PROMPT"
        receipt_value = "TOP SECRET RECEIPT PREFIX"
        terminal_output = "TOP SECRET TERMINAL OUTPUT"
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            store = Store(path)
            store.initialize()
            store.enqueue(
                NewJob(
                    workflow="example",
                    title="Safe title",
                    harness=Harness.CODEX,
                    prompt=prompt,
                    dedupe_key="secret-v1",
                    max_attempts=2,
                    placement=PlacementTarget.TAB,
                    receipt=TaskReceipt(ReceiptKind.OUTPUT_PREFIX, receipt_value),
                )
            )
            claimed = store.claim("example", limit=1, lease_seconds=60)[0]
            store.record_outcome(
                claimed,
                DispatchOutcome(
                    "worker",
                    AgentState.DONE,
                    False,
                    "w1:p1",
                    placement=PlacementTarget.TAB,
                    execution_path="/repo",
                    herdr_workspace_id="w1",
                ),
            )
            with closing(sqlite3.connect(path)) as connection, connection:
                connection.execute(
                    "UPDATE jobs SET error_summary = NULL WHERE id = ?",
                    (claimed.job_id,),
                )
                connection.execute(
                    "UPDATE receipts SET error_summary = ? WHERE job_id = ?",
                    (terminal_output, claimed.job_id),
                )

            observation = SqliteObserver(path, "example").observe()
            snapshot = RuntimeProjector(
                "example",
                FakeQueueObserver(observation),
                FakeHerdrObserver(HerdrObservation("ok", None, (), (), (), (), ())),
                clock=lambda: 1.0,
            ).snapshot()
            serialized = json.dumps(
                {
                    "jobs": observation.jobs,
                    "receipts": observation.receipts,
                    "snapshot": snapshot,
                }
            )

        self.assertNotIn(prompt, serialized)
        self.assertNotIn(receipt_value, serialized)
        self.assertNotIn(terminal_output, serialized)
        self.assertNotIn("receipt_value", observation.jobs[0])
        self.assertNotIn("error_summary", observation.receipts[0])

    def test_projector_correlates_jobs_and_reports_runtime_drift(self) -> None:
        now = 2_000.0
        jobs = (
            _job_row(1, "Missing agent", "running", now - 400, "worker-one"),
            _job_row(2, "Still working", "succeeded", now - 10, "worker-two"),
        )
        receipts = (
            {
                "id": 1,
                "job_id": 2,
                "attempt": 1,
                "state": "succeeded",
                "agent_name": "worker-two",
                "agent_state": "done",
                "member_reused": 0,
                "pane_id": "w1:p2",
                "error_code": None,
                "placement": "tab",
                "execution_path": "/repo",
                "herdr_workspace_id": "w1",
                "observed_at": now - 10,
            },
        )
        herdr = HerdrObservation(
            "ok",
            None,
            (
                {
                    "workspace_id": "w1",
                    "label": "repo",
                    "pane_count": 1,
                    "tab_count": 1,
                },
            ),
            (
                {
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                    "label": "Still working",
                },
            ),
            (
                {
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                    "pane_id": "w1:p2",
                    "cwd": "/repo",
                },
            ),
            (
                {
                    "name": "worker-two",
                    "agent": "codex",
                    "agent_status": "working",
                    "workspace_id": "w1",
                    "tab_id": "w1:t1",
                    "pane_id": "w1:p2",
                    "cwd": "/repo",
                },
            ),
            (
                {
                    "path": "/repo/.orchestrator/worktrees/task-2",
                    "branch": "orchestrator/task-2",
                    "label": "Task 2",
                    "open_workspace_id": "w1",
                    "is_linked_worktree": True,
                },
            ),
        )
        projector = RuntimeProjector(
            "example",
            FakeQueueObserver(QueueObservation(jobs, receipts)),
            FakeHerdrObserver(herdr),
            clock=lambda: now,
        )

        snapshot = projector.snapshot()

        self.assertEqual(snapshot["summary"]["running"], 1)
        self.assertEqual(snapshot["summary"]["active_agents"], 1)
        self.assertLessEqual(
            {
                "schema_version",
                "workflow",
                "generated_at",
                "source_health",
                "summary",
                "jobs",
                "attention",
                "topology",
                "timeline",
            },
            set(snapshot),
        )
        self.assertLessEqual({"workspaces", "projects"}, set(snapshot["topology"]))
        self.assertIn("running_agent_missing", snapshot["jobs"][0]["drift"])
        self.assertIn(
            "terminal_job_agent_working",
            snapshot["jobs"][1]["drift"],
        )
        self.assertEqual(snapshot["jobs"][1]["runtime"]["pane_id"], "w1:p2")
        self.assertIs(snapshot["jobs"][1]["agent_settled"], True)
        self.assertIs(snapshot["jobs"][1]["task_verified"], True)
        self.assertEqual(
            snapshot["topology"]["workspaces"][0]["tabs"][0]["panes"][0]["agent"]["name"],
            "worker-two",
        )
        project = snapshot["topology"]["projects"][0]
        worktree = project["worktrees"][0]
        self.assertEqual(project["label"], "example")
        self.assertEqual(worktree["workspace_id"], "w1")
        self.assertEqual(worktree["branch"], "orchestrator/task-2")
        self.assertTrue(worktree["is_linked_worktree"])
        self.assertEqual(worktree["tabs"][0]["panes"][0]["pane_id"], "w1:p2")
        self.assertEqual(snapshot["timeline"][0]["type"], "receipt")
        attention_codes = {item["code"] for item in snapshot["attention"]}
        self.assertLessEqual(
            {
                "running_agent_missing",
                "terminal_job_agent_working",
                "lease_expired",
                "job_stale",
            },
            attention_codes,
        )

    def test_topology_does_not_cross_join_foreign_entities(self) -> None:
        herdr = HerdrObservation(
            "ok",
            None,
            (
                {"workspace_id": "local", "label": "local"},
                {"workspace_id": "foreign", "label": "foreign"},
            ),
            (
                {"workspace_id": "local", "tab_id": "shared:t1", "label": "local"},
                {"workspace_id": "foreign", "tab_id": "shared:t1", "label": "foreign"},
            ),
            (
                {"workspace_id": "local", "tab_id": "shared:t1", "pane_id": "shared:p1"},
                {"workspace_id": "foreign", "tab_id": "shared:t1", "pane_id": "shared:p1"},
            ),
            (
                {
                    "name": "local-agent",
                    "workspace_id": "local",
                    "tab_id": "shared:t1",
                    "pane_id": "shared:p1",
                    "agent_status": "idle",
                },
                {
                    "name": "foreign-agent",
                    "workspace_id": "foreign",
                    "tab_id": "shared:t1",
                    "pane_id": "shared:p1",
                    "agent_status": "working",
                },
            ),
            (),
        )
        original_herdr = deepcopy(herdr)

        snapshot = RuntimeProjector(
            "example",
            FakeQueueObserver(QueueObservation((), ())),
            FakeHerdrObserver(herdr),
            clock=lambda: 2_000.0,
        ).snapshot()

        workspaces = {
            workspace["workspace_id"]: workspace for workspace in snapshot["topology"]["workspaces"]
        }
        local_panes = workspaces["local"]["tabs"][0]["panes"]
        foreign_panes = workspaces["foreign"]["tabs"][0]["panes"]
        self.assertEqual(len(local_panes), 1)
        self.assertEqual(len(foreign_panes), 1)
        self.assertEqual(local_panes[0]["agent"]["name"], "local-agent")
        self.assertEqual(foreign_panes[0]["agent"]["name"], "foreign-agent")
        self.assertEqual(herdr, original_herdr)

    def test_herdr_observer_scopes_and_whitelists_runtime_fields(self) -> None:
        workspace = Path("/repo")

        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: int | None,
        ) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == ["agent", "list"]:
                result = {
                    "agents": [
                        {
                            "name": "worker",
                            "agent": "codex",
                            "agent_status": "working",
                            "workspace_id": "w1",
                            "tab_id": "w1:t1",
                            "pane_id": "w1:p1",
                            "cwd": "/repo",
                            "terminal_title": "must not leak",
                        },
                        {
                            "name": "outside",
                            "agent": "codex",
                            "agent_status": "working",
                            "workspace_id": "w1",
                            "cwd": "/outside",
                            "terminal_title": "outside output must not leak",
                        },
                        {
                            "name": "malformed-path",
                            "agent": "codex",
                            "agent_status": "working",
                            "workspace_id": "w1",
                            "cwd": "\x00",
                            "terminal_title": "malformed output must not leak",
                        },
                        {
                            "name": "other",
                            "workspace_id": "w9",
                            "cwd": "/other",
                        },
                    ]
                }
            elif argv[1:3] == ["workspace", "list"]:
                result = {
                    "workspaces": [
                        {
                            "workspace_id": "w1",
                            "label": "repo",
                            "worktree": {
                                "repo_root": "/repo",
                                "secret": "must not leak",
                            },
                        },
                        {
                            "workspace_id": "w9",
                            "label": "other",
                            "worktree": {"repo_root": "/other"},
                        },
                    ]
                }
            elif argv[1:3] == ["worktree", "list"]:
                result = {
                    "worktrees": [
                        {
                            "path": "/repo",
                            "branch": "main",
                            "open_workspace_id": "w1",
                            "secret": "must not leak",
                        },
                        {
                            "path": "/repo/.orchestrator/worktrees/empty",
                            "branch": "empty",
                            "open_workspace_id": "",
                        },
                        {
                            "path": "/other",
                            "branch": "other",
                            "open_workspace_id": "w9",
                        },
                    ]
                }
            elif argv[1:3] == ["tab", "list"]:
                result = {
                    "tabs": [
                        {
                            "workspace_id": argv[-1],
                            "tab_id": f"{argv[-1]}:t1",
                            "label": "Task",
                        },
                        {
                            "workspace_id": "w9",
                            "tab_id": "w9:t9",
                            "label": "foreign tab must not leak",
                        },
                    ]
                }
            elif argv[1:3] == ["pane", "list"]:
                result = {
                    "panes": [
                        {
                            "workspace_id": argv[-1],
                            "tab_id": f"{argv[-1]}:t1",
                            "pane_id": f"{argv[-1]}:p1",
                            "cwd": "/repo",
                            "agent": {"terminal_output": "must not leak nested"},
                            "terminal_id": "must not leak",
                        },
                        {
                            "workspace_id": "w9",
                            "tab_id": "w9:t9",
                            "pane_id": "w9:p9",
                            "cwd": "/other",
                            "terminal_id": "foreign output must not leak",
                        },
                        {
                            "workspace_id": "w1",
                            "tab_id": "w1:t1",
                            "pane_id": "w1:p-outside",
                            "cwd": "/outside",
                            "terminal_id": "outside output must not leak",
                        },
                    ]
                }
            else:
                raise AssertionError(argv)
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"id": "test", "result": result}),
                "",
            )

        observation = HerdrObserver(workspace, runner=runner).observe()
        serialized = json.dumps(
            observation.__dict__
            if hasattr(observation, "__dict__")
            else {
                "workspaces": observation.workspaces,
                "tabs": observation.tabs,
                "panes": observation.panes,
                "agents": observation.agents,
                "worktrees": observation.worktrees,
            }
        )

        self.assertEqual(observation.health, "ok")
        self.assertEqual([row["workspace_id"] for row in observation.workspaces], ["w1"])
        self.assertEqual([row["name"] for row in observation.agents], ["worker"])
        self.assertEqual([row["workspace_id"] for row in observation.tabs], ["w1"])
        self.assertEqual([row["workspace_id"] for row in observation.panes], ["w1"])
        self.assertEqual([row["pane_id"] for row in observation.panes], ["w1:p1"])
        self.assertNotIn("must not leak", serialized)
        self.assertNotIn("secret", serialized)

    def test_herdr_observer_rejects_non_object_rows(self) -> None:
        workspace = Path("/repo")

        def runner(
            argv: list[str],
            *,
            cwd: str,
            timeout: int | None,
        ) -> subprocess.CompletedProcess[str]:
            if argv[1:3] == ["agent", "list"]:
                result = {"agents": ["malformed terminal output"]}
            else:
                result = {argv[1] + "s": []}
            return subprocess.CompletedProcess(
                argv,
                0,
                json.dumps({"id": "test", "result": result}),
                "",
            )

        observation = HerdrObserver(workspace, runner=runner).observe()

        self.assertEqual(observation.health, "unavailable")
        self.assertEqual(observation.error_code, "herdr_invalid_response")

    def test_snapshot_feed_waits_for_new_event(self) -> None:
        feed = SnapshotFeed()
        self.assertEqual(feed.current(), (0, None))

        first_id = feed.publish({"value": 1})
        same_id, none = feed.wait_after(first_id, timeout=0.01)
        second_id = feed.publish({"value": 2})
        observed_id, snapshot = feed.wait_after(first_id, timeout=0.01)
        reset_id, reset_snapshot = feed.wait_after(100, timeout=0.01)

        self.assertEqual(first_id, 1)
        self.assertEqual((same_id, none), (first_id, None))
        self.assertEqual((second_id, observed_id), (2, 2))
        self.assertEqual(snapshot, {"value": 2})
        self.assertEqual((reset_id, reset_snapshot), (2, {"value": 2}))

    def test_sse_future_id_recovers_before_first_snapshot(self) -> None:
        feed = SnapshotFeed()
        result: list[tuple[int, dict[str, object] | None]] = []
        waiter = threading.Thread(
            target=lambda: result.append(feed.wait_after(99, timeout=0.5)),
        )
        waiter.start()
        time.sleep(0.02)
        feed.publish({"value": 1})
        waiter.join(timeout=1)

        self.assertFalse(waiter.is_alive())
        self.assertEqual(result, [(1, {"value": 1})])

    def test_server_shutdown_closes_open_sse_clients(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(base, state_db=Path(temporary) / "state.db")
            Store(config.state_db).initialize()
            projector = FakeProjector(
                {
                    "schema_version": 1,
                    "workflow": "example",
                    "generated_at": 1.0,
                    "source_health": {"queue": "ok", "herdr": "ok"},
                    "summary": {},
                    "jobs": [],
                    "attention": [],
                    "topology": {"workspaces": []},
                    "timeline": [],
                }
            )
            server = DashboardServer(
                config,
                port=0,
                poll_seconds=60,
                projector=projector,
            )
            serve_thread = threading.Thread(target=server.serve_forever)
            connection: HTTPConnection | None = None
            reader: threading.Thread | None = None
            result: list[tuple[str, bytes | None]] = []
            try:
                serve_thread.start()
                deadline = time.monotonic() + 2
                while server.feed.current()[1] is None:
                    if time.monotonic() >= deadline:
                        self.fail("dashboard monitor did not publish a snapshot")
                    time.sleep(0.01)

                host, port = server.address
                event_id, _ = server.feed.current()
                connection = HTTPConnection(host, port, timeout=0.5)
                connection.request(
                    "GET",
                    "/api/events",
                    headers={"Last-Event-ID": str(event_id)},
                )
                response = connection.getresponse()

                def read_response() -> None:
                    try:
                        result.append(("ok", response.read(1)))
                    except Exception as exc:
                        result.append((type(exc).__name__, None))

                reader = threading.Thread(target=read_response, daemon=True)
                reader.start()
                time.sleep(0.05)
                server.shutdown()
                serve_thread.join(timeout=2)
                reader.join(timeout=1)

                self.assertFalse(serve_thread.is_alive())
                self.assertFalse(reader.is_alive())
                self.assertEqual(result, [("ok", b"")])
            finally:
                if connection is not None:
                    connection.close()
                if serve_thread.is_alive():
                    server.shutdown()
                    serve_thread.join(timeout=2)

    def test_server_shutdown_before_serving_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(base, state_db=Path(temporary) / "state.db")
            Store(config.state_db).initialize()
            server = DashboardServer(config, port=0, projector=FakeProjector({}))

            server.shutdown()
            server.shutdown()
            serve_thread = threading.Thread(target=server.serve_forever)
            serve_thread.start()
            serve_thread.join(timeout=1)

        self.assertFalse(serve_thread.is_alive())

    def test_http_server_serves_snapshot_and_packaged_dashboard(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(base, state_db=Path(temporary) / "state.db")
            Store(config.state_db).initialize()
            projector = FakeProjector(
                {
                    "schema_version": 1,
                    "workflow": "example",
                    "generated_at": 1.0,
                    "source_health": {"queue": "ok", "herdr": "ok"},
                    "summary": {},
                    "jobs": [],
                    "attention": [],
                    "topology": {"workspaces": []},
                    "timeline": [],
                }
            )
            server = DashboardServer(
                config,
                port=0,
                poll_seconds=0.25,
                projector=projector,
            )
            thread = threading.Thread(target=server.serve_forever)
            thread.start()
            try:
                host, port = server.address
                base_url = f"http://{host}:{port}"
                health = _wait_for_json(f"{base_url}/api/health")
                with urlopen(f"{base_url}/", timeout=2) as response:
                    index = response.read().decode()
                    policy = response.headers["Content-Security-Policy"]
                snapshot = _wait_for_json(f"{base_url}/api/snapshot")
                with urlopen(f"{base_url}/assets/dashboard.css", timeout=2) as response:
                    content_type = response.headers["Content-Type"]
                    dashboard_css = response.read().decode()
                with urlopen(f"{base_url}/assets/cytoscape.min.js", timeout=2) as response:
                    cytoscape_content_type = response.headers["Content-Type"]
                    cytoscape_data = response.read()
                with urlopen(f"{base_url}/assets/dashboard.js", timeout=2) as response:
                    dashboard_script = response.read().decode()
                with urlopen(f"{base_url}/assets/topology.js", timeout=2) as response:
                    topology_script = response.read().decode()
                with urlopen(f"{base_url}/assets/topology-style.js", timeout=2) as response:
                    topology_style_content_type = response.headers["Content-Type"]
                    topology_style_script = response.read().decode()
                with urlopen(f"{base_url}/assets/source-warning.js", timeout=2) as response:
                    source_warning_content_type = response.headers["Content-Type"]
                    source_warning_script = response.read().decode()
                with urlopen(f"{base_url}/assets/timeline-continuity.js", timeout=2) as response:
                    timeline_continuity_content_type = response.headers["Content-Type"]
                    timeline_continuity_script = response.read().decode()
                with urlopen(f"{base_url}/assets/dashboard-utils.js", timeout=2) as response:
                    dashboard_utils_content_type = response.headers["Content-Type"]
                    dashboard_utils_script = response.read().decode()
                connection = HTTPConnection(host, port, timeout=2)
                connection.request(
                    "GET",
                    "/api/health",
                    headers={"Host": "attacker.example"},
                )
                rejected_host = connection.getresponse().status
                connection.close()
            finally:
                server.shutdown()
                thread.join(timeout=3)

        self.assertTrue(health["ok"])
        self.assertIn("Herdr Operations", index)
        self.assertIn("default-src 'self'", policy)
        self.assertEqual(snapshot["snapshot"]["workflow"], "example")
        self.assertEqual(content_type, "text/css; charset=utf-8")
        self.assertEqual(cytoscape_content_type, "text/javascript; charset=utf-8")
        self.assertGreater(len(cytoscape_data), 400_000)
        self.assertIn("The Cytoscape Consortium", cytoscape_data[:180].decode())
        self.assertIn("/assets/cytoscape.min.js", index)
        self.assertIn("/assets/topology.js", index)
        self.assertIn("/assets/topology-style.js", index)
        self.assertIn("/assets/source-warning.js", index)
        self.assertIn("/assets/timeline-continuity.js", index)
        self.assertIn("/assets/dashboard-utils.js", index)
        self.assertLess(
            index.index("/assets/topology.js"), index.index("/assets/topology-style.js")
        )
        self.assertLess(
            index.index("/assets/topology-style.js"), index.index("/assets/dashboard.js")
        )
        self.assertLess(
            index.index("/assets/source-warning.js"), index.index("/assets/dashboard.js")
        )
        self.assertLess(
            index.index("/assets/timeline-continuity.js"),
            index.index("/assets/dashboard.js"),
        )
        self.assertLess(
            index.index("/assets/dashboard-utils.js"), index.index("/assets/source-warning.js")
        )
        self.assertEqual(topology_style_content_type, "text/javascript; charset=utf-8")
        self.assertEqual(source_warning_content_type, "text/javascript; charset=utf-8")
        self.assertEqual(timeline_continuity_content_type, "text/javascript; charset=utf-8")
        self.assertEqual(dashboard_utils_content_type, "text/javascript; charset=utf-8")
        self.assertIn("function topologyStyles", topology_style_script)
        self.assertIn("function setSourceWarning", source_warning_script)
        self.assertIn("function captureTimelineContinuity", timeline_continuity_script)
        self.assertIn("function finiteNumber", dashboard_utils_script)
        self.assertIn('id="topology-touch-owner"', index)
        self.assertIn('class="icon-button topology-touch-toggle"', index)
        self.assertIn('aria-controls="topology"', index)
        self.assertIn('aria-pressed="false"', index)
        self.assertIn('<path d="M12 2v20"></path>', index)
        self.assertIn('id="status-announcement"', index)
        self.assertNotIn('<div class="connection" aria-live="polite">', index)
        self.assertIn('id="connection-label" aria-live="polite" aria-atomic="true"', index)
        self.assertIn('id="last-updated" aria-live="off"', index)
        self.assertIn(
            'aria-keyshortcuts="+ - 0 Home ArrowUp ArrowDown ArrowLeft ArrowRight '
            'Control+ArrowUp Control+ArrowDown Control+ArrowLeft Control+ArrowRight Escape"',
            index,
        )
        self.assertIn("topologyGraph", topology_script)
        self.assertIn("topologyFocusViewport", topology_script)
        self.assertIn("topologyRebaseViewportCapture", topology_script)
        self.assertIn("topologyViewportMotionDuration", topology_script)
        focus_adapter = dashboard_script[
            dashboard_script.index("function readTopologyFocusInput") : dashboard_script.index(
                "function setTopologyViewport"
            )
        ]
        self.assertIn("node.isParent()", focus_adapter)
        self.assertIn(
            "const selectedBounds = node.boundingBox({\n"
            "      includeNodes: true,\n"
            "      includeLabels: false,\n"
            "      includeOverlays: true,\n"
            "      includeUnderlays: true,\n"
            "    });",
            focus_adapter,
        )
        self.assertIn(
            "const descendantBounds = descendants.boundingBox({\n"
            "      includeNodes: true,\n"
            "      includeLabels: true,\n"
            "      includeOverlays: false,\n"
            "      includeUnderlays: false,\n"
            "    });",
            focus_adapter,
        )
        viewport_writer = dashboard_script[
            dashboard_script.index("function setTopologyViewport") : dashboard_script.index(
                "function stopTopologyViewportMotion"
            )
        ]
        self.assertIn("topologyViewportMotionDuration", viewport_writer)
        self.assertIn('easing: "ease-out-cubic"', viewport_writer)
        self.assertIn("queue: false", viewport_writer)
        self.assertLess(
            viewport_writer.index("stopTopologyViewportMotion()"),
            viewport_writer.index("topologyViewportMotionDuration"),
        )
        self.assertIn("target: next", viewport_writer)
        zoom_writer = dashboard_script[
            dashboard_script.index("function zoomTopology") : dashboard_script.index(
                "function panTopology"
            )
        ]
        self.assertIn("getZoomedViewport", zoom_writer)
        self.assertIn("readTopologyZoomState", zoom_writer)
        self.assertIn("state.commandZoom", zoom_writer)
        self.assertIn("setTopologyViewport", zoom_writer)
        self.assertIn('purpose: "zoom"', zoom_writer)
        self.assertIn("if (!target) return false", zoom_writer)
        self.assertNotIn("topologyCanvas.zoom(", zoom_writer)
        fit_writer = dashboard_script[
            dashboard_script.index("function fitTopology") : dashboard_script.index(
                "function zoomTopology"
            )
        ]
        self.assertIn("getFitViewport", fit_writer)
        self.assertIn("captureTopologyViewport", fit_writer)
        self.assertIn("setTopologyViewport", fit_writer)
        self.assertIn('purpose: "fit"', fit_writer)
        self.assertNotIn("topologyCanvas.fit(", fit_writer)
        self.assertNotIn("topologyCanvas.zoom(", fit_writer)
        pan_writer = dashboard_script[
            dashboard_script.index("function panTopology") : dashboard_script.index(
                "function topologyA11yId"
            )
        ]
        self.assertIn('purpose === "pan"', pan_writer)
        self.assertIn("active.target", pan_writer)
        self.assertIn("captureTopologyViewport", pan_writer)
        self.assertIn("setTopologyViewport", pan_writer)
        self.assertIn('purpose: "pan"', pan_writer)
        self.assertNotIn("topologyCanvas.pan(", pan_writer)
        self.assertNotIn("minimumReadable", dashboard_script)
        self.assertEqual(
            dashboard_script.count("fitTopology({ user: true, animate: true"),
            2,
        )
        self.assertIn("agent settled", dashboard_script)
        self.assertIn("task verified", dashboard_script)
        self.assertIn("error_summary", dashboard_script)
        self.assertIn("data-column-key", dashboard_script)
        self.assertIn("dataset.density", dashboard_script)
        self.assertIn("dataset.attention", dashboard_script)
        self.assertIn("syncMainGridOrder", dashboard_script)
        self.assertIn("insertBefore", dashboard_script)
        self.assertIn("captureMainGridContinuity", dashboard_script)
        self.assertIn("restoreMainGridContinuity", dashboard_script)
        main_grid_order = dashboard_script[
            dashboard_script.index("function syncMainGridOrder()") : dashboard_script.index(
                "function handleCompactViewportChange"
            )
        ]
        self.assertLess(
            main_grid_order.index("captureMainGridContinuity"),
            main_grid_order.index("insertBefore"),
        )
        self.assertLess(
            main_grid_order.index("insertBefore"),
            main_grid_order.index("restoreMainGridContinuity"),
        )
        self.assertIn('behavior: "instant"', dashboard_script)
        self.assertIn("preventScroll: true", dashboard_script)
        self.assertIn('matchMedia?.("(max-width: 760px)")', dashboard_script)
        self.assertIn("kanbanOrderKey", dashboard_script)
        self.assertIn("columnOrder", dashboard_script)
        self.assertIn("handleCompactViewportChange", dashboard_script)
        self.assertIn("dataset.columnOrder", dashboard_script)
        self.assertIn("scrollLeft", dashboard_script)
        navigation_marker = 'id="kanban-navigation"'
        board_marker = 'id="kanban"'
        self.assertIn(navigation_marker, index)
        self.assertLess(index.index(navigation_marker), index.index(board_marker))
        self.assertIn(
            '<nav id="kanban-navigation" class="kanban-navigation" '
            'aria-label="Kanban columns" hidden></nav>',
            index,
        )
        self.assertIn(
            'aria-label="Kanban columns" hidden></nav>\n' '            <div id="kanban"',
            index,
        )
        self.assertIn(
            "let kanbanNavigationState = {\n  activeColumnKey: null,\n};",
            dashboard_script,
        )
        self.assertEqual(dashboard_script.count("activeColumnKey: null"), 1)
        self.assertIn('document.createElement("button")', dashboard_script)
        self.assertIn('button.type = "button"', dashboard_script)
        self.assertIn("button.dataset.columnKey = column.key", dashboard_script)
        self.assertIn(
            'button.setAttribute("aria-controls", kanbanColumnId(column.key))',
            dashboard_script,
        )
        self.assertIn('button.setAttribute("aria-current", "true")', dashboard_script)
        self.assertIn('button.removeAttribute("aria-current")', dashboard_script)
        self.assertIn("navigation.hidden = !compact", dashboard_script)
        self.assertIn("reconcileKanbanNavigation", dashboard_script)
        self.assertIn("function adjacentKanbanColumnKey", dashboard_script)
        self.assertIn("function kanbanReachableColumnStops", dashboard_script)
        self.assertIn("function nearestKanbanColumnKey", dashboard_script)
        self.assertIn("Math.min(maxScrollLeft, Math.max(0, left))", dashboard_script)
        adjacency = dashboard_script[
            dashboard_script.index("function adjacentKanbanColumnKey") : dashboard_script.index(
                "function kanbanReachableColumnStops"
            )
        ]
        self.assertIn("nextIndex < 0 || nextIndex >= columnOrder.length", adjacency)
        self.assertIn("return columnOrder[nextIndex]", adjacency)
        self.assertIn("event.composedPath()", dashboard_script)
        self.assertIn("function kanbanKeyboardOwner", dashboard_script)
        self.assertIn("origin === board", dashboard_script)
        self.assertIn('byId("kanban").addEventListener("focusin"', dashboard_script)
        focus_listener = dashboard_script[
            dashboard_script.index(
                'byId("kanban").addEventListener("focusin"'
            ) : dashboard_script.index('byId("kanban").addEventListener("keydown"')
        ]
        self.assertIn("compactViewport?.matches", focus_listener)
        self.assertIn('closest?.(".kanban-column[data-column-key]")', focus_listener)
        self.assertIn("column?.parentElement !== event.currentTarget", focus_listener)
        self.assertIn("moveKanbanToColumn(column.dataset.columnKey", focus_listener)
        self.assertIn('{ kind: "column", key: column.dataset.columnKey }', focus_listener)
        self.assertIn('origin.matches(".kanban-column")', dashboard_script)
        self.assertIn("scheduleKanbanManualScrollObservation", dashboard_script)
        manual_observer = dashboard_script[
            dashboard_script.index(
                "function scheduleKanbanManualScrollObservation"
            ) : dashboard_script.index("function kanbanKeyboardOwner")
        ]
        self.assertIn("nearestKanbanColumnKey", manual_observer)
        self.assertNotIn("scrollTo", manual_observer)
        self.assertNotIn("scrollBy", manual_observer)
        self.assertIn("kanbanScrollGeneration", dashboard_script)
        self.assertIn("active.generation !== generation", dashboard_script)
        self.assertIn("cancelKanbanProgrammaticScroll", dashboard_script)
        self.assertIn('["pointerdown", "touchstart", "wheel"]', dashboard_script)
        self.assertIn('behavior: motionAllowed() ? "smooth" : "auto"', dashboard_script)
        self.assertIn('behavior: "auto"', dashboard_script)
        self.assertNotIn("clientWidth * 0.72", dashboard_script)
        semantic_assets = index + dashboard_script
        self.assertNotIn('role="tablist"', semantic_assets)
        self.assertNotIn('role="tab"', semantic_assets)
        self.assertNotIn("aria-selected", semantic_assets)
        self.assertNotIn('type="range"', semantic_assets)
        self.assertNotIn("localStorage", semantic_assets)
        self.assertNotIn("location.hash", semantic_assets)
        self.assertNotIn("createKanbanController", dashboard_script)
        self.assertIn(".kanban-navigation {\n  display: none;", dashboard_css)
        self.assertIn(".kanban-navigation[hidden]", dashboard_css)
        self.assertIn(
            ".kanban-navigation:not([hidden]) {\n"
            "    display: grid;\n"
            "    grid-template-columns: repeat(4, minmax(0, 1fr));\n"
            "  }",
            dashboard_css,
        )
        self.assertIn(
            ".kanban-navigation button {\n    min-height: 44px;\n  }",
            dashboard_css,
        )
        self.assertIn("width: 25%;\n  height: 2px;", dashboard_css)
        self.assertIn("transition: transform 220ms ease;", dashboard_css)
        self.assertIn(
            "@media (prefers-reduced-motion: reduce)",
            dashboard_css,
        )
        self.assertIn(
            ".kanban-navigation::after {\n    transition: none;\n  }",
            dashboard_css,
        )
        self.assertIn("is-order-changing", dashboard_script)
        self.assertIn("jobVisualSignature", dashboard_script)
        self.assertIn(
            'function fitTopology({ user = false, animate = false, origin = "programmatic" } = {})',
            dashboard_script,
        )
        self.assertIn("fitPadding", dashboard_script)
        self.assertIn("compact ? 12 : 11", topology_style_script)
        self.assertIn("compact ? 14 : 13", topology_style_script)
        self.assertIn('"text-background-color": "#0d1114"', topology_style_script)
        self.assertIn('"text-background-padding": 5', topology_style_script)
        self.assertIn('"text-overflow-wrap": "anywhere"', topology_style_script)
        self.assertIn('"text-max-width": compact ? 130 : 170', topology_style_script)
        self.assertIn("animationDuration: 360", dashboard_script)
        self.assertIn("topologyLayoutGeneration", dashboard_script)
        self.assertIn("is-reflowing", dashboard_script)
        self.assertIn("jobCardSignature", dashboard_script)
        self.assertIn("timelineVisualSignature", dashboard_script)
        self.assertIn("attentionVisualSignature", dashboard_script)
        self.assertIn("refreshJobAges", dashboard_script)
        self.assertIn("panTopology", dashboard_script)
        self.assertIn("topologyNavigationOrder", dashboard_script)
        self.assertIn("topologySelectionDirection", dashboard_script)
        self.assertIn("cycleTopologySelection", dashboard_script)
        self.assertIn("selectTopologyNode", dashboard_script)
        self.assertIn("revealTopologyNode", dashboard_script)
        self.assertIn("aria-activedescendant", dashboard_script)
        self.assertIn("topology-selection-status", index)
        self.assertIn("topology-selection-help", index)
        self.assertIn("topology-active-descendant", index)
        self.assertIn("aria-current", dashboard_script)
        self.assertIn("reconcileTopologyNavigation", dashboard_script)
        self.assertIn("topologyViewportState", dashboard_script)
        self.assertIn("programmaticWriteDepth", dashboard_script)
        self.assertIn("motionGeneration", dashboard_script)
        self.assertIn("handleTopologyResize", dashboard_script)
        resize_handler = dashboard_script[
            dashboard_script.index("function handleTopologyResize()") : dashboard_script.index(
                "function renderTopologyTree"
            )
        ]
        focused_resize = resize_handler[
            resize_handler.index(
                'if (topologyViewportState.focus.kind === "focused")'
            ) : resize_handler.index('if (topologyViewportState.focus.kind === "restoring")')
        ]
        restoring_start = resize_handler.index(
            'if (topologyViewportState.focus.kind === "restoring")'
        )
        restoring_resize = resize_handler[
            restoring_start : resize_handler.index(
                "\n  stopTopologyViewportMotion();",
                restoring_start,
            )
        ]
        self.assertIn(
            'if (topologyViewportState.overviewMode === "auto")',
            focused_resize,
        )
        self.assertIn("fitTopology();", focused_resize)
        self.assertIn(
            'else {\n        setTopologyViewport(baseline.viewport, { purpose: "restore" });',
            focused_resize,
        )
        self.assertIn(
            "wasCompact && !compact " '&& topologyViewportState.overviewMode === "auto"',
            restoring_resize,
        )
        self.assertIn("fitTopology();", restoring_resize)
        self.assertIn(
            'else {\n      setTopologyViewport(target.viewport, { purpose: "restore" });',
            restoring_resize,
        )
        compact_entry_start = resize_handler.index(
            "if (!wasCompact && compact && selectedId "
            '&& topologyViewportState.overviewMode === "auto")'
        )
        compact_entry_resize = resize_handler[
            compact_entry_start : resize_handler.index(
                "\n  if (topologyHasRendered",
                compact_entry_start,
            )
        ]
        compact_fit = "fitTopology();"
        compact_capture = "const baseline = captureTopologyViewport();"
        self.assertIn(compact_fit, compact_entry_resize)
        self.assertLess(
            compact_entry_resize.index(compact_fit),
            compact_entry_resize.index(compact_capture),
        )
        self.assertNotIn("topologyViewportTouched", dashboard_script)
        self.assertNotIn("topologyViewportUpdate", dashboard_script)
        self.assertIn("Control+ArrowRight", index)
        self.assertIn("Escape", dashboard_script)
        self.assertIn('topologyCanvas.on("mouseover", "node"', dashboard_script)
        self.assertIn('topologyCanvas.on("mouseout", "node"', dashboard_script)
        self.assertIn("is-hovered", dashboard_script)
        self.assertIn(
            "node[kind = 'project']:selected, node[kind = 'worktree']:selected, "
            "node[kind = 'tab']:selected",
            topology_style_script,
        )
        self.assertIn('"text-halign": "center"', topology_style_script)
        self.assertIn('"text-margin-x": 0', topology_style_script)
        self.assertIn('byId("kanban").addEventListener', dashboard_script)
        self.assertIn(
            "Initial snapshot unavailable. Retrying live stream.",
            dashboard_script,
        )
        self.assertIn(".source-warning-region.is-hidden", dashboard_css)
        self.assertIn("connection.is-offline", dashboard_css)
        self.assertIn(
            ".connection.is-live.is-transitioning .connection-dot",
            dashboard_css,
        )
        self.assertIn(
            "animation: live-pulse 640ms cubic-bezier(0.16, 1, 0.3, 1) both;",
            dashboard_css,
        )
        self.assertNotIn("animation: live-pulse 2.4s ease-out infinite;", dashboard_css)
        self.assertIn("cursor: grab", dashboard_css)
        self.assertIn('.kanban[data-density="sparse"]', dashboard_css)
        self.assertIn('.kanban[data-density="empty"]', dashboard_css)
        self.assertNotIn("order: -1", dashboard_css)
        self.assertIn("order-shift", dashboard_css)
        self.assertIn('.topology-canvas[data-touch-owner="page"]', dashboard_css)
        self.assertIn('.topology-canvas[data-touch-owner="graph"]', dashboard_css)
        self.assertIn("pointer-events: none", dashboard_css)
        self.assertIn("touch-action: pan-y", dashboard_css)
        self.assertIn("touch-action: none", dashboard_css)
        self.assertIn(".topology-touch-toggle[hidden]", dashboard_css)
        self.assertIn('.topology-touch-toggle[aria-pressed="true"]', dashboard_css)
        self.assertIn("@media (pointer: coarse)", dashboard_css)
        self.assertIn('matchMedia?.("(pointer: coarse)")', dashboard_script)
        self.assertIn("topologyTouchOwnershipState", dashboard_script)
        self.assertIn("coarseOwner", dashboard_script)
        self.assertIn("function currentTopologyTouchMode()", dashboard_script)
        self.assertIn("function setTopologyTouchOwner(owner)", dashboard_script)
        self.assertIn("function syncTopologyTouchOwnership()", dashboard_script)
        self.assertEqual(dashboard_script.count("function syncTopologyTouchOwnership()"), 1)
        self.assertIn("userPanningEnabled", dashboard_script)
        self.assertIn("userZoomingEnabled", dashboard_script)
        self.assertIn('addEventListener("change"', dashboard_script)
        self.assertIn("addListener", dashboard_script)
        self.assertEqual(rejected_host, 421)
        self.assertFalse(thread.is_alive())

    def test_server_rejects_non_loopback_bind(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            base = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
            config = replace(base, state_db=Path(temporary) / "state.db")

            with self.assertRaisesRegex(ValueError, "dashboard_host_must_be_loopback"):
                DashboardServer(config, host="0.0.0.0")

    def test_kanban_render_preserves_scroll_owned_indicator_with_stale_focus(self) -> None:
        dashboard_script = (
            REPO_ROOT / "src/herdr_orchestrator/dashboard/static/dashboard.js"
        ).read_text()
        render = dashboard_script[
            dashboard_script.index("function renderKanban") : dashboard_script.index(
                "function kanbanColumnId"
            )
        ]

        active_projection = "columnKeys.includes(kanbanNavigationState.activeColumnKey)"
        focus_projection = "(kanbanProgrammaticScroll ? null : focusCapture?.key)"
        self.assertIn(active_projection, render)
        self.assertIn(focus_projection, render)
        self.assertLess(
            render.index(active_projection),
            render.index(focus_projection),
            "a valid scroll-owned column must win over stale navigation focus",
        )


def _job_row(
    job_id: int,
    title: str,
    state: str,
    updated_at: float,
    agent_name: str,
) -> dict[str, object]:
    return {
        "id": job_id,
        "title": title,
        "harness": "codex",
        "dedupe_key": f"job-{job_id}",
        "placement": "tab",
        "state": state,
        "attempts": 1,
        "max_attempts": 2,
        "available_at": updated_at,
        "lease_until": updated_at + 60 if state == "running" else None,
        "agent_name": agent_name,
        "error_code": None,
        "agent_settled": state == "succeeded",
        "task_verified": True if state == "succeeded" else None,
        "execution_path": "/repo",
        "herdr_workspace_id": "w1",
        "created_at": updated_at - 100,
        "updated_at": updated_at,
    }


def _wait_for_json(url: str) -> dict[str, object]:
    deadline = time.monotonic() + 3
    while True:
        try:
            with urlopen(url, timeout=1) as response:
                return json.loads(response.read())
        except Exception:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.05)


if __name__ == "__main__":
    unittest.main()
