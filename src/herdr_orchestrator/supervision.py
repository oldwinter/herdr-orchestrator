"""Durable DAG constraints and local, explicitly acknowledged worker mail."""

from __future__ import annotations

import json
import re
import sqlite3
import time
import uuid
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from herdr_orchestrator.attempts import StoreError

MESSAGE_TYPES = (
    "status",
    "dispatch",
    "worker_done",
    "merge_ready",
    "escalation",
    "handoff",
    "decision_gate",
    "heartbeat",
    "question",
    "reply",
)
PRIORITIES = ("normal", "high", "urgent")
HANDLE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.:-]{0,127}\Z")


def create_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """CREATE TABLE IF NOT EXISTS job_dependencies (
            job_id INTEGER NOT NULL REFERENCES jobs(id),
            prerequisite_id INTEGER NOT NULL REFERENCES jobs(id),
            PRIMARY KEY(job_id, prerequisite_id), CHECK(job_id != prerequisite_id))""",
        """CREATE TABLE IF NOT EXISTS supervision_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT, workflow TEXT NOT NULL,
            workspace TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
            kind TEXT NOT NULL, priority TEXT NOT NULL, subject TEXT NOT NULL,
            body TEXT NOT NULL, thread_id TEXT NOT NULL, reply_to INTEGER,
            job_id INTEGER, attempt_id INTEGER, dedupe_key TEXT NOT NULL,
            created_at REAL NOT NULL, acknowledged_at REAL,
            UNIQUE(workflow, workspace, sender, recipient, dedupe_key))""",
        """CREATE INDEX IF NOT EXISTS supervision_inbox
            ON supervision_messages(workflow, workspace, recipient, acknowledged_at, id)""",
        """CREATE INDEX IF NOT EXISTS supervision_replies
            ON supervision_messages(workflow, workspace, recipient, reply_to, id)""",
        """CREATE TABLE IF NOT EXISTS supervision_gates (
            id TEXT PRIMARY KEY, job_id INTEGER NOT NULL REFERENCES jobs(id),
            question TEXT NOT NULL, options TEXT NOT NULL, status TEXT NOT NULL,
            resolution TEXT, created_at REAL NOT NULL, resolved_at REAL)""",
        """CREATE TABLE IF NOT EXISTS supervision_broadcasts (
            workflow TEXT NOT NULL, workspace TEXT NOT NULL, sender TEXT NOT NULL,
            target TEXT NOT NULL, dedupe_key TEXT NOT NULL, contract TEXT NOT NULL,
            recipients TEXT NOT NULL, thread_id TEXT NOT NULL,
            PRIMARY KEY(workflow, workspace, sender, target, dedupe_key))""",
    )
    for statement in statements:
        connection.execute(statement)


def dependencies(connection: sqlite3.Connection, job_id: int) -> tuple[int, ...]:
    return tuple(
        int(row[0])
        for row in connection.execute(
            "SELECT prerequisite_id FROM job_dependencies WHERE job_id = ? "
            "ORDER BY prerequisite_id",
            (job_id,),
        )
    )


def add_dependencies(
    connection: sqlite3.Connection,
    job_id: int,
    requested: tuple[int, ...],
    workflow: str,
    workspace: str | None,
) -> None:
    if len(requested) > 100 or any(type(item) is not int or item <= 0 for item in requested):
        raise StoreError("dependencies_invalid")
    for prerequisite in set(requested):
        row = connection.execute(
            "SELECT workflow, workspace FROM jobs WHERE id = ?",
            (prerequisite,),
        ).fetchone()
        # Dependencies refer only to earlier, same-scope jobs. No later edit can create a cycle.
        if (
            prerequisite >= job_id
            or row is None
            or row["workflow"] != workflow
            or row["workspace"] != workspace
        ):
            raise StoreError("dependency_scope_or_order_invalid")
        connection.execute(
            "INSERT INTO job_dependencies VALUES (?, ?)",
            (job_id, prerequisite),
        )


def pending_constraints(connection: sqlite3.Connection, job_id: int) -> list[str]:
    reasons = [
        f"dependency:{row[0]}"
        for row in connection.execute(
            """SELECT d.prerequisite_id FROM job_dependencies d JOIN jobs p
           ON p.id = d.prerequisite_id WHERE d.job_id = ?
           AND (p.state != 'succeeded' OR p.task_verified IS NOT 1)
           ORDER BY d.prerequisite_id""",
            (job_id,),
        )
    ]
    reasons.extend(
        f"gate:{row[0]}"
        for row in connection.execute(
            "SELECT id FROM supervision_gates WHERE job_id = ? "
            "AND status != 'resolved' ORDER BY id",
            (job_id,),
        )
    )
    return reasons


def _text(value: str, field: str, limit: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise StoreError(f"supervision_{field}_invalid")
    return value


def _handle(value: str) -> str:
    if not isinstance(value, str) or not HANDLE.fullmatch(value):
        raise StoreError("supervision_handle_invalid")
    return value


class Supervision:
    """Same-host coordination, not an authentication or process sandbox."""

    def __init__(self, path: Path, workflow: str, workspace: str) -> None:
        self.path, self.workflow, self.workspace = path, workflow, workspace

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            yield connection

    def _job(self, connection: sqlite3.Connection, job_id: int) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM jobs WHERE id = ? AND workflow = ? AND workspace = ?",
            (job_id, self.workflow, self.workspace),
        ).fetchone()
        if row is None:
            raise StoreError("job_not_found")
        assert isinstance(row, sqlite3.Row)
        return row

    def _recipients(self, connection: sqlite3.Connection, target: str, sender: str) -> list[str]:
        if not target.startswith("@"):
            return [_handle(target)]
        rows = connection.execute(
            """SELECT DISTINCT agent_name, harness, state FROM jobs
               WHERE workflow = ? AND workspace = ? AND agent_name IS NOT NULL
               AND state IN ('running', 'blocked')""",
            (self.workflow, self.workspace),
        ).fetchall()
        if target != "@all" and target not in {
            "@droid",
            "@grok",
            "@codex",
            "@pi",
            "@claude",
            "@hermes",
        }:
            raise StoreError("supervision_group_invalid")
        recipients = sorted(
            {
                str(row["agent_name"])
                for row in rows
                if row["agent_name"] != sender
                and (target == "@all" or target == f"@{row['harness']}")
            }
        )
        if not recipients:
            raise StoreError("supervision_group_empty")
        return recipients

    def send(
        self,
        *,
        sender: str,
        recipient: str,
        body: str,
        dedupe_key: str,
        kind: str = "status",
        subject: str = "message",
        priority: str = "normal",
        thread_id: str | None = None,
        reply_to: int | None = None,
        job_id: int | None = None,
        attempt_id: int | None = None,
        fencing_token: str | None = None,
    ) -> list[int]:
        _handle(sender)
        _text(body, "body", 16000)
        _text(subject, "subject", 256)
        _text(dedupe_key, "dedupe_key", 256)
        if kind not in MESSAGE_TYPES or priority not in PRIORITIES:
            raise StoreError("supervision_message_invalid")
        if kind == "reply" and reply_to is None:
            raise StoreError("supervision_reply_invalid")
        if thread_id is not None:
            _text(thread_id, "thread", 128)
        lifecycle = kind in {"heartbeat", "worker_done", "escalation", "question", "decision_gate"}
        with self._transaction() as connection:
            if lifecycle or any(item is not None for item in (job_id, attempt_id, fencing_token)):
                self._validate_attempt(connection, sender, job_id, attempt_id, fencing_token)
            if reply_to is not None:
                parent = connection.execute(
                    """SELECT * FROM supervision_messages WHERE id = ? AND workflow = ?
                       AND workspace = ? AND recipient = ?""",
                    (reply_to, self.workflow, self.workspace, sender),
                ).fetchone()
                if parent is None or recipient != parent["sender"]:
                    raise StoreError("supervision_reply_invalid")
                thread_id = str(parent["thread_id"])
            recipients, thread = self._delivery_targets(
                connection,
                recipient,
                sender,
                dedupe_key,
                thread_id,
                (kind, priority, subject, body, reply_to, job_id, attempt_id),
            )
            ids: list[int] = []
            for target in recipients:
                contract = (kind, priority, subject, body, reply_to, job_id, attempt_id)
                existing = connection.execute(
                    """SELECT * FROM supervision_messages WHERE workflow = ? AND workspace = ?
                       AND sender = ? AND recipient = ? AND dedupe_key = ?""",
                    (self.workflow, self.workspace, sender, target, dedupe_key),
                ).fetchone()
                if existing is not None:
                    previous = tuple(
                        existing[key]
                        for key in (
                            "kind",
                            "priority",
                            "subject",
                            "body",
                            "reply_to",
                            "job_id",
                            "attempt_id",
                        )
                    )
                    expected_thread = thread if recipient.startswith("@") else thread_id
                    if previous != contract or (
                        expected_thread and existing["thread_id"] != expected_thread
                    ):
                        raise StoreError("supervision_dedupe_conflict")
                    ids.append(int(existing["id"]))
                    continue
                cursor = connection.execute(
                    """INSERT INTO supervision_messages(workflow, workspace, sender, recipient,
                       kind, priority, subject, body, thread_id, reply_to, job_id, attempt_id,
                       dedupe_key, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        self.workflow,
                        self.workspace,
                        sender,
                        target,
                        kind,
                        priority,
                        subject,
                        body,
                        thread,
                        reply_to,
                        job_id,
                        attempt_id,
                        dedupe_key,
                        time.time(),
                    ),
                )
                assert cursor.lastrowid is not None
                ids.append(cursor.lastrowid)
            return ids

    def _delivery_targets(
        self,
        connection: sqlite3.Connection,
        target: str,
        sender: str,
        key: str,
        thread: str | None,
        contract: tuple[object, ...],
    ) -> tuple[list[str], str]:
        if not target.startswith("@"):
            return self._recipients(connection, target, sender), thread or uuid.uuid4().hex
        identity = (self.workflow, self.workspace, sender, target, key)
        encoded = json.dumps(contract)
        existing = connection.execute(
            """SELECT * FROM supervision_broadcasts WHERE workflow = ? AND workspace = ?
               AND sender = ? AND target = ? AND dedupe_key = ?""",
            identity,
        ).fetchone()
        if existing is not None:
            if existing["contract"] != encoded or (thread and thread != existing["thread_id"]):
                raise StoreError("supervision_dedupe_conflict")
            return [str(value) for value in json.loads(existing["recipients"])], str(
                existing["thread_id"]
            )
        recipients = self._recipients(connection, target, sender)
        thread = thread or uuid.uuid4().hex
        connection.execute(
            "INSERT INTO supervision_broadcasts VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (*identity, encoded, json.dumps(recipients), thread),
        )
        return recipients, thread

    def _validate_attempt(
        self,
        connection: sqlite3.Connection,
        sender: str,
        job_id: int | None,
        attempt_id: int | None,
        fencing_token: str | None,
    ) -> None:
        if job_id is None or attempt_id is None or not fencing_token:
            raise StoreError("supervision_attempt_required")
        job = self._job(connection, job_id)
        row = connection.execute(
            "SELECT * FROM job_attempts WHERE id = ? AND job_id = ?",
            (attempt_id, job_id),
        ).fetchone()
        if (
            row is None
            or job["current_attempt_id"] != attempt_id
            or job["state"] != "running"
            or row["agent_name"] != sender
            or row["fencing_token"] != fencing_token
            or row["lease_until"] <= time.time()
        ):
            raise StoreError("supervision_stale_dispatch")

    def check(
        self,
        recipient: str,
        *,
        after: int = 0,
        thread_id: str | None = None,
        kinds: tuple[str, ...] = (),
        limit: int = 100,
        include_acknowledged: bool = False,
        reply_to: int | None = None,
    ) -> list[dict[str, object]]:
        _handle(recipient)
        if not 1 <= limit <= 100 or after < 0 or any(kind not in MESSAGE_TYPES for kind in kinds):
            raise StoreError("supervision_query_invalid")
        query = """SELECT * FROM supervision_messages WHERE workflow = ? AND workspace = ?
                   AND recipient = ? AND id > ?"""
        values: list[object] = [self.workflow, self.workspace, recipient, after]
        if not include_acknowledged:
            query += " AND acknowledged_at IS NULL"
        if thread_id:
            query += " AND thread_id = ?"
            values.append(thread_id)
        if reply_to is not None:
            query += " AND reply_to = ?"
            values.append(reply_to)
        if kinds:
            query += f" AND kind IN ({','.join('?' for _ in kinds)})"
            values.extend(kinds)
        query += " ORDER BY id LIMIT ?"
        values.append(limit)
        with self._transaction() as connection:
            return [dict(row) for row in connection.execute(query, values)]

    def acknowledge(self, recipient: str, ids: tuple[int, ...]) -> int:
        _handle(recipient)
        if not ids or len(ids) > 100 or any(type(item) is not int or item <= 0 for item in ids):
            raise StoreError("supervision_ack_invalid")
        with self._transaction() as connection:
            for message_id in set(ids):
                cursor = connection.execute(
                    """UPDATE supervision_messages
                       SET acknowledged_at = COALESCE(acknowledged_at, ?)
                       WHERE id = ? AND workflow = ? AND workspace = ? AND recipient = ?""",
                    (time.time(), message_id, self.workflow, self.workspace, recipient),
                )
                if cursor.rowcount != 1:
                    raise StoreError("supervision_ack_not_owned")
        return len(set(ids))

    def create_gate(
        self,
        job_id: int,
        gate_id: str,
        question: str,
        options: tuple[str, ...] = (),
    ) -> str:
        _handle(gate_id)
        _text(question, "question", 4000)
        if len(options) > 20:
            raise StoreError("supervision_options_invalid")
        for option in options:
            _text(option, "option", 500)
        encoded = json.dumps(options)
        with self._transaction() as connection:
            job = self._job(connection, job_id)
            existing = connection.execute(
                "SELECT * FROM supervision_gates WHERE id = ?",
                (gate_id,),
            ).fetchone()
            if existing is not None:
                if (existing["job_id"], existing["question"], existing["options"]) != (
                    job_id,
                    question,
                    encoded,
                ):
                    raise StoreError("supervision_gate_conflict")
                return gate_id
            if job["state"] != "pending" or job["attempts"] != 0:
                raise StoreError("supervision_gate_requires_unstarted_job")
            connection.execute(
                "INSERT INTO supervision_gates VALUES (?, ?, ?, ?, 'pending', NULL, ?, NULL)",
                (gate_id, job_id, question, encoded, time.time()),
            )
        return gate_id

    def resolve_gate(self, gate_id: str, resolution: str) -> None:
        _text(resolution, "resolution", 4000)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM supervision_gates WHERE id = ?",
                (gate_id,),
            ).fetchone()
            if row is None:
                raise StoreError("supervision_gate_missing")
            self._job(connection, int(row["job_id"]))
            if row["status"] == "resolved" and row["resolution"] != resolution:
                raise StoreError("supervision_gate_conflict")
            connection.execute(
                """UPDATE supervision_gates SET status = 'resolved', resolution = ?,
                   resolved_at = COALESCE(resolved_at, ?) WHERE id = ?""",
                (resolution, time.time(), gate_id),
            )

    def gates(self, job_id: int | None = None) -> list[dict[str, object]]:
        with self._transaction() as connection:
            rows = connection.execute(
                """SELECT g.* FROM supervision_gates g JOIN jobs j ON j.id = g.job_id
                   WHERE j.workflow = ? AND j.workspace = ? AND (? IS NULL OR j.id = ?)
                   ORDER BY g.created_at, g.id""",
                (self.workflow, self.workspace, job_id, job_id),
            )
            return [dict(row) for row in rows]

    def constraints(self) -> list[dict[str, object]]:
        with self._transaction() as connection:
            return [
                {"job_id": int(row[0]), "waiting_for": pending_constraints(connection, row[0])}
                for row in connection.execute(
                    "SELECT id FROM jobs WHERE workflow = ? AND workspace = ? "
                    "AND state = 'pending'",
                    (self.workflow, self.workspace),
                )
                if pending_constraints(connection, row[0])
            ]
