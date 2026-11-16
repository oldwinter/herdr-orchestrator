from __future__ import annotations

import threading
from pathlib import Path
from typing import Protocol

from herdr_orchestrator.herdr import HerdrTransport
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    WorkflowConfig,
)
from herdr_orchestrator.protocol import Command, TransportError, run_json


class DeliveryDispatcher(Protocol):
    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome: ...

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str: ...

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome: ...


class HerdrDeliveryDispatcher:
    def __init__(self, workflow: WorkflowConfig) -> None:
        self.workflow = workflow
        self._transports: dict[Path, HerdrTransport] = {}
        self._lock = threading.Lock()

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        return self._transport(workspace).dispatch(
            harness,
            prompt,
            timeout_seconds=timeout_seconds,
            agent_name=agent_name,
        )

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        return self._transport(workspace).read_agent(name, lines=lines)

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        return self._transport(workspace).respond(
            name,
            harness,
            response,
            timeout_seconds=timeout_seconds,
        )

    def inspect_agent(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
    ) -> DispatchOutcome | None:
        transport = self._transport(workspace)
        try:
            result = run_json(
                transport.runner,
                Command(
                    ["herdr", "agent", "get", name],
                    workspace,
                    10,
                ),
            )
        except TransportError as exc:
            if exc.code == "agent_not_found":
                return None
            raise
        agent = result.get("agent")
        if not isinstance(agent, dict):
            raise TransportError("herdr_invalid_response")
        state_value = agent.get("agent_status")
        pane_id = agent.get("pane_id")
        workspace_id = agent.get("workspace_id")
        if (
            agent.get("name") not in {None, name}
            or agent.get("agent") != harness.value
            or not isinstance(state_value, str)
            or not isinstance(pane_id, str)
            or not pane_id
            or not isinstance(agent.get("interactive_ready"), bool)
            or not agent["interactive_ready"]
            or any(
                not isinstance(agent.get(key), str)
                or Path(agent[key]).resolve() != workspace.resolve()
                for key in ("cwd", "foreground_cwd")
            )
            or (
                workspace_id is not None and (not isinstance(workspace_id, str) or not workspace_id)
            )
        ):
            raise TransportError("agent_identity_mismatch")
        try:
            state = AgentState(state_value)
        except ValueError as exc:
            raise TransportError("herdr_invalid_response") from exc
        return DispatchOutcome(
            name,
            state,
            True,
            pane_id,
            execution_path=str(workspace.resolve()),
            herdr_workspace_id=workspace_id,
            agent_settled=state in {AgentState.IDLE, AgentState.DONE},
        )

    def _transport(self, workspace: Path) -> HerdrTransport:
        resolved = workspace.resolve()
        with self._lock:
            transport = self._transports.get(resolved)
            if transport is None:
                transport = HerdrTransport(self.workflow.name, resolved)
                self._transports[resolved] = transport
            return transport
