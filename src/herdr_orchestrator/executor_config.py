from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ExecutorKind(StrEnum):
    RESEARCH_SYNTHESIS = "research-synthesis"
    CODE_REVIEW_GATE = "code-review-gate"
    SOFTWARE_DELIVERY = "software-delivery"
    INCIDENT_RESPONSE = "incident-response"


@dataclass(frozen=True, slots=True)
class ExecutorConfig:
    """The strict, code-selected configuration for one schema-v2 executor."""

    kind: ExecutorKind
    settings: Mapping[str, Any]


REGISTERED_EXECUTORS = frozenset(ExecutorKind)

# The table names are intentionally short and stable for TOML users.  The
# hyphenated aliases are accepted as an explicit spelling of the same
# executor, not as additional executors.
EXECUTOR_TABLES: Mapping[ExecutorKind, frozenset[str]] = {
    ExecutorKind.RESEARCH_SYNTHESIS: frozenset({"research", "research-synthesis"}),
    ExecutorKind.CODE_REVIEW_GATE: frozenset({"review", "code-review-gate"}),
    ExecutorKind.SOFTWARE_DELIVERY: frozenset({"delivery", "software-delivery"}),
    ExecutorKind.INCIDENT_RESPONSE: frozenset({"incident", "incident-response"}),
}

EXECUTOR_TABLE_TO_KIND = {
    table_name: kind
    for kind, table_names in EXECUTOR_TABLES.items()
    for table_name in table_names
}

# These are configuration data, not a graph or policy language.  Keeping the
# list explicit makes adding a new domain section an intentional API change.
EXECUTOR_FIELDS: Mapping[ExecutorKind, frozenset[str]] = {
    ExecutorKind.RESEARCH_SYNTHESIS: frozenset(
        {
            "decomposition",
            "research",
            "verification",
            "loop",
            "selection",
            "synthesis",
            "citation_gate",
            "routes",
            "roles",
            "budgets",
            "input",
            "coverage",
        }
    ),
    ExecutorKind.CODE_REVIEW_GATE: frozenset(
        {
            "snapshot",
            "axes",
            "checks",
            "gate_threshold",
            "challenger",
            "adjudicator",
            "repair",
            "roles",
            "input",
            "profile",
        }
    ),
    ExecutorKind.SOFTWARE_DELIVERY: frozenset(
        {
            "base",
            "decision",
            "plan",
            "ticket_parallelism",
            "checks",
            "tracker",
            "review",
            "repair",
            "roles",
            "input",
            "profile",
        }
    ),
    ExecutorKind.INCIDENT_RESPONSE: frozenset(
        {
            "roles",
            "evidence",
            "adversary",
            "budgets",
            "fixture",
            "repair",
            "input",
            "profile",
        }
    ),
}

PROGRAMMABLE_FIELDS = frozenset(
    {
        "graph",
        "workflow_graph",
        "node",
        "nodes",
        "stage",
        "stages",
        "edge",
        "edges",
        "condition",
        "conditions",
        "expression",
        "expressions",
        "callback",
        "callbacks",
        "shell",
        "shell_command",
        "command",
        "commands",
        "exec",
        "executable",
        "transition",
        "transitions",
        "state_transition",
        "state_transitions",
        "dynamic",
    }
)

# Static checks are the one deliberate executable-looking surface.  They are
# coordinator-owned argv data and are validated separately by config.py; a
# shell string or a command field is never accepted.
STATIC_CHECK_FIELDS = frozenset(
    {
        "id",
        "name",
        "argv",
        "cwd",
        "environment",
        "timeout_seconds",
        "max_output_bytes",
    }
)
