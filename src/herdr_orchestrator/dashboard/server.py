from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlsplit

from herdr_orchestrator.dashboard.observer import HerdrObserver, SqliteObserver
from herdr_orchestrator.dashboard.projector import RuntimeProjector
from herdr_orchestrator.model import WorkflowConfig
from herdr_orchestrator.store import Store

ASSET_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
}


class SnapshotFeed:
    def __init__(self) -> None:
        self._condition = threading.Condition()
        self._event_id = 0
        self._snapshot: dict[str, object] | None = None

    def publish(self, snapshot: dict[str, object]) -> int:
        with self._condition:
            self._event_id += 1
            self._snapshot = snapshot
            self._condition.notify_all()
            return self._event_id

    def current(self) -> tuple[int, dict[str, object] | None]:
        with self._condition:
            return self._event_id, self._snapshot

    def wait_after(
        self,
        event_id: int,
        *,
        timeout: float,
    ) -> tuple[int, dict[str, object] | None]:
        with self._condition:
            if (
                self._snapshot is not None
                and event_id > self._event_id
            ):
                return self._event_id, self._snapshot
            self._condition.wait_for(
                lambda: self._event_id > event_id,
                timeout=timeout,
            )
            if self._event_id <= event_id:
                return event_id, None
            return self._event_id, self._snapshot


class DashboardMonitor:
    def __init__(
        self,
        snapshot: Callable[[], dict[str, object]],
        feed: SnapshotFeed,
        *,
        poll_seconds: float,
    ) -> None:
        self.snapshot = snapshot
        self.feed = feed
        self.poll_seconds = poll_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="dashboard-monitor",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self.poll_seconds + 2)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                snapshot = self.snapshot()
            except Exception as exc:
                snapshot = {
                    "schema_version": 1,
                    "generated_at": time.time(),
                    "source_health": {
                        "queue": "unavailable",
                        "herdr": "unknown",
                        "herdr_error": None,
                    },
                    "error": type(exc).__name__,
                }
            self.feed.publish(snapshot)
            self._stop.wait(self.poll_seconds)


class DashboardServer:
    def __init__(
        self,
        config: WorkflowConfig,
        *,
        host: str = "127.0.0.1",
        port: int = 8765,
        poll_seconds: float = 2.0,
        projector: RuntimeProjector | None = None,
    ) -> None:
        if host not in {"127.0.0.1", "localhost"}:
            raise ValueError("dashboard_host_must_be_loopback")
        if not 0 <= port <= 65535:
            raise ValueError("dashboard_port_invalid")
        if not 0.25 <= poll_seconds <= 60:
            raise ValueError("dashboard_poll_seconds_invalid")
        Store(config.state_db).initialize()
        self.config = config
        self.host = host
        self.port = port
        self.feed = SnapshotFeed()
        self.projector = projector or RuntimeProjector(
            config.name,
            SqliteObserver(config.state_db, config.name),
            HerdrObserver(config.workspace),
        )
        self.monitor = DashboardMonitor(
            self.projector.snapshot,
            self.feed,
            poll_seconds=poll_seconds,
        )
        handler = _handler(self.feed)
        try:
            self.httpd = ThreadingHTTPServer((host, port), handler)
        except OSError as exc:
            raise ValueError("dashboard_bind_failed") from exc
        self.httpd.daemon_threads = True

    @property
    def address(self) -> tuple[str, int]:
        host, port = self.httpd.server_address[:2]
        return str(host), int(port)

    def serve_forever(self) -> None:
        self.monitor.start()
        try:
            self.httpd.serve_forever()
        finally:
            self.monitor.stop()
            self.httpd.server_close()

    def shutdown(self) -> None:
        self.httpd.shutdown()


def _handler(feed: SnapshotFeed) -> type[BaseHTTPRequestHandler]:
    class DashboardHandler(BaseHTTPRequestHandler):
        server_version = "HerdrDashboard/1"
        sys_version = ""

        def do_GET(self) -> None:
            if not self._host_allowed():
                self.send_error(HTTPStatus.MISDIRECTED_REQUEST)
                return
            path = urlparse(self.path).path
            if path == "/api/snapshot":
                self._snapshot()
                return
            if path == "/api/events":
                self._events()
                return
            if path == "/api/health":
                event_id, snapshot = feed.current()
                self._json(
                    {
                        "ok": snapshot is not None,
                        "event_id": event_id,
                    },
                    HTTPStatus.OK if snapshot is not None else HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            asset = "index.html" if path == "/" else path.removeprefix("/assets/")
            if path == "/" or path.startswith("/assets/"):
                self._asset(asset)
                return
            self.send_error(HTTPStatus.NOT_FOUND)

        def log_message(self, format: str, *args: Any) -> None:
            return

        def _host_allowed(self) -> bool:
            raw_host = self.headers.get("Host")
            if not raw_host:
                return False
            try:
                parsed = urlsplit(f"//{raw_host}")
                request_port = parsed.port
            except ValueError:
                return False
            server_port = int(self.server.server_address[1])
            return (
                parsed.hostname in {"127.0.0.1", "localhost"}
                and (request_port is None or request_port == server_port)
            )

        def _snapshot(self) -> None:
            event_id, snapshot = feed.current()
            if snapshot is None:
                self._json(
                    {"error": "snapshot_not_ready"},
                    HTTPStatus.SERVICE_UNAVAILABLE,
                )
                return
            self._json(
                {"event_id": event_id, "snapshot": snapshot},
                HTTPStatus.OK,
            )

        def _events(self) -> None:
            requested = self.headers.get("Last-Event-ID", "0")
            try:
                event_id = max(0, int(requested))
            except ValueError:
                event_id = 0
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            try:
                while True:
                    next_id, snapshot = feed.wait_after(event_id, timeout=15)
                    if snapshot is None:
                        self.wfile.write(b": heartbeat\n\n")
                    else:
                        payload = json.dumps(
                            snapshot,
                            ensure_ascii=False,
                            separators=(",", ":"),
                        ).encode()
                        self.wfile.write(
                            f"id: {next_id}\nevent: snapshot\ndata: ".encode()
                            + payload
                            + b"\n\n"
                        )
                        event_id = next_id
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

        def _asset(self, name: str) -> None:
            if name not in {"index.html", "dashboard.css", "dashboard.js"}:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            asset = files("herdr_orchestrator.dashboard.static").joinpath(name)
            data = asset.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                ASSET_TYPES.get(Path(name).suffix, "application/octet-stream"),
            )
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; connect-src 'self'; "
                "style-src 'self'; script-src 'self'; img-src 'self' data:; "
                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(data)

        def _json(self, payload: object, status: HTTPStatus) -> None:
            data = json.dumps(payload, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)

    return DashboardHandler
