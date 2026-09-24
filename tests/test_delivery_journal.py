from __future__ import annotations

import json
import multiprocessing
import os
import re
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from herdr_orchestrator.config import load_workflow
from herdr_orchestrator.delivery import DeliveryError, StandardizedDelivery
from herdr_orchestrator.delivery_journal import (
    DeliveryEffect,
    DeliveryEffectObservation,
    DeliveryEffectState,
    DeliveryJournal,
)
from herdr_orchestrator.delivery_protocol import DeliveryTicket, TicketReceipt
from herdr_orchestrator.model import (
    AgentState,
    DispatchOutcome,
    Harness,
    TrackerBackend,
    WayfinderMode,
    WorkflowConfig,
)
from herdr_orchestrator.tracker import LocalMarkdownTracker, TrackerTicket

REPO_ROOT = Path(__file__).resolve().parents[1]

JournalPersist = Callable[
    [DeliveryJournal, str, str | None, str | None, dict[str, object], float],
    None,
]
OwnerProjectionWrite = Callable[[DeliveryJournal, str, float], None]

class TrackerPublishInterrupted(BaseException):
    pass


def _interrupt_journal(
    original: JournalPersist,
    target_event: str,
    target_key: str,
    state: list[bool],
) -> JournalPersist:
    def interrupt(
        journal: DeliveryJournal,
        event: str,
        operation_key: str | None,
        effect_kind: str | None,
        details: dict[str, object],
        observed_at: float,
    ) -> None:
        original(
            journal,
            event,
            operation_key,
            effect_kind,
            details,
            observed_at,
        )
        if not state[0] and event == target_event and operation_key == target_key:
            state[0] = True
            raise RuntimeError("journal interruption")

    return interrupt


def _interrupt_before_confirmation(
    original: JournalPersist,
    target_key: str,
    interrupted: list[bool],
) -> JournalPersist:
    def interrupt(
        journal: DeliveryJournal,
        event: str,
        operation_key: str | None,
        effect_kind: str | None,
        details: dict[str, object],
        observed_at: float,
    ) -> None:
        if not interrupted[0] and event == "effect_confirmed" and operation_key == target_key:
            interrupted[0] = True
            raise RuntimeError("applied effect before confirmation")
        original(
            journal,
            event,
            operation_key,
            effect_kind,
            details,
            observed_at,
        )

    return interrupt


def _interrupt_owner_event(
    original: JournalPersist,
    target_event: str,
    interrupted: list[bool],
) -> JournalPersist:
    def interrupt(
        journal: DeliveryJournal,
        event: str,
        operation_key: str | None,
        effect_kind: str | None,
        details: dict[str, object],
        observed_at: float,
    ) -> None:
        original(
            journal,
            event,
            operation_key,
            effect_kind,
            details,
            observed_at,
        )
        if not interrupted[0] and event == target_event:
            interrupted[0] = True
            raise RuntimeError("owner transition interruption")

    return interrupt


def _interrupt_owner_projection(
    original: OwnerProjectionWrite,
    target_status: str,
    interrupted: list[bool],
) -> OwnerProjectionWrite:
    def interrupt(
        journal: DeliveryJournal,
        status: str,
        observed_at: float,
    ) -> None:
        original(journal, status, observed_at)
        if not interrupted[0] and status == target_status:
            interrupted[0] = True
            raise RuntimeError("owner transition interruption")

    return interrupt


class BlockingDispatcher:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls = 0

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise AssertionError("test dispatcher was not released")
        raise RuntimeError("stop active delivery")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("blocked test dispatcher should not be read")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("blocked test dispatcher should not be resumed")


class ExitDispatcher:
    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        os._exit(77)

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("exiting dispatcher should not be read")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("exiting dispatcher should not be resumed")


class StoppingDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        self.calls += 1
        raise RuntimeError("stop recovered delivery")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("stopping dispatcher should not be read")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("stopping dispatcher should not be resumed")


class WorkingAgentDispatcher(StoppingDispatcher):
    def inspect_agent(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
    ) -> DispatchOutcome | None:
        return DispatchOutcome(name, AgentState.WORKING, True, "w1:p2")


class PlanningThenStoppingDispatcher:
    def __init__(self) -> None:
        self.calls = 0

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        self.calls += 1
        if "Create one accepted specification" in prompt:
            _artifact_path(prompt).write_text(
                json.dumps(_delivery_plan()),
                encoding="utf-8",
            )
            return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")
        raise RuntimeError("stop after tracker recovery")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("planning dispatcher should not be read")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("planning dispatcher should not be resumed")


class CrashAfterPlanDispatcher(PlanningThenStoppingDispatcher):
    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        if "Create one accepted specification" not in prompt:
            raise AssertionError(f"unexpected prompt before plan crash: {prompt[:120]}")
        self.calls += 1
        _artifact_path(prompt).write_text(json.dumps(_delivery_plan()), encoding="utf-8")
        raise RuntimeError("controller died after plan artifact")


class CrashOnceTracker:
    def __init__(self, external: dict[str, object]) -> None:
        self.external = external
        self.references: dict[str, TrackerTicket] = {}
        self.spec_url: str | None = None
        self.publish_calls = 0

    def publish(self, plan: object, *, markers: object | None = None) -> dict[str, TrackerTicket]:
        self.publish_calls += 1
        self.external.setdefault("markers", markers)
        self.external.setdefault("spec_url", "https://tracker.example/spec")
        self.external.setdefault(
            "references",
            {"01": TrackerTicket("01", "https://tracker.example/tickets/01")},
        )
        if not self.external.get("crashed"):
            self.external["crashed"] = True
            raise RuntimeError("tracker died after publication")
        self.spec_url = str(self.external["spec_url"])
        references = self.external["references"]
        assert isinstance(references, dict)
        self.references = dict(references)
        return dict(self.references)

    def close(self, ticket: object, receipt: object, *, marker: str | None = None) -> None:
        raise AssertionError("test stops before tracker close")

    def observe_publication(
        self,
        plan: object,
        *,
        markers: object,
        receipts: object | None = None,
    ) -> tuple[dict[str, TrackerTicket], str | None] | None:
        references = self.external.get("references")
        spec_url = self.external.get("spec_url")
        if not isinstance(references, dict) or not isinstance(spec_url, str):
            return None
        return dict(references), spec_url

    def observe_close(
        self,
        ticket: DeliveryTicket,
        receipt: TicketReceipt,
        *,
        marker: str,
    ) -> bool:
        return False


class CompleteDispatcher:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        self.prompts.append(prompt)
        if "Create one accepted specification" in prompt:
            _artifact_path(prompt).write_text(json.dumps(_delivery_plan()), encoding="utf-8")
        elif "受限 harness router" in prompt:
            _artifact_path(prompt).write_text(
                json.dumps({"harness": "droid"}),
                encoding="utf-8",
            )
        elif "Implement exactly one accepted delivery ticket" in prompt:
            changed = workspace / "slice-01.txt"
            changed.write_text("implemented once\n", encoding="utf-8")
            _git(workspace, "add", changed.name)
            _git(workspace, "commit", "-m", "feat: implement journal slice")
            commit = _git(workspace, "rev-parse", "HEAD").stdout.strip()
            receipt = _receipt_path(prompt)
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                json.dumps(
                    {
                        "ticket_id": "01",
                        "commit": commit,
                        "acceptance": [
                            {
                                "criterion": "The slice is committed once.",
                                "passed": True,
                                "evidence": "slice-01.txt exists",
                            }
                        ],
                        "checks": ["scripted validation passed"],
                        "summary": "Implemented the recoverable slice.",
                    }
                ),
                encoding="utf-8",
            )
        elif "Standards axis only" in prompt:
            _artifact_path(prompt).write_text(
                json.dumps({"standards": []}),
                encoding="utf-8",
            )
        elif "Spec axis only" in prompt:
            _artifact_path(prompt).write_text(
                json.dumps({"spec": []}),
                encoding="utf-8",
            )
        else:
            raise AssertionError(f"unexpected delivery prompt: {prompt[:120]}")
        return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        raise AssertionError("complete dispatcher should not block")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        raise AssertionError("complete dispatcher should not be resumed")


class TwoTicketDispatcher(CompleteDispatcher):
    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        if "Create one accepted specification" in prompt:
            self.prompts.append(prompt)
            _artifact_path(prompt).write_text(
                json.dumps(_two_ticket_plan()),
                encoding="utf-8",
            )
            return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")
        if "Implement exactly one accepted delivery ticket" in prompt:
            self.prompts.append(prompt)
            receipt = _receipt_path(prompt)
            match = re.search(r"ticket-(\d{2})\.json$", receipt.name)
            if match is None:
                raise AssertionError("ticket id missing")
            ticket_id = match.group(1)
            changed = workspace / f"slice-{ticket_id}.txt"
            changed.write_text(f"implemented {ticket_id}\n", encoding="utf-8")
            _git(workspace, "add", changed.name)
            _git(workspace, "commit", "-m", f"feat: implement journal slice {ticket_id}")
            commit = _git(workspace, "rev-parse", "HEAD").stdout.strip()
            receipt.parent.mkdir(parents=True, exist_ok=True)
            receipt.write_text(
                json.dumps(
                    {
                        "ticket_id": ticket_id,
                        "commit": commit,
                        "acceptance": [
                            {
                                "criterion": f"Slice {ticket_id} is committed once.",
                                "passed": True,
                                "evidence": f"slice-{ticket_id}.txt exists",
                            }
                        ],
                        "checks": ["scripted validation passed"],
                        "summary": f"Implemented slice {ticket_id}.",
                    }
                ),
                encoding="utf-8",
            )
            return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")
        return super().dispatch(
            workspace,
            harness,
            prompt,
            timeout_seconds=timeout_seconds,
            agent_name=agent_name,
        )


class CrashAfterCloseTracker:
    def __init__(self, external: dict[str, object]) -> None:
        self.external = external
        self.references: dict[str, TrackerTicket] = {}
        self.spec_url: str | None = None
        self.close_calls = 0

    def publish(self, plan: object, *, markers: object | None = None) -> dict[str, TrackerTicket]:
        self.spec_url = "https://tracker.example/spec"
        self.references = {"01": TrackerTicket("01", "https://tracker.example/tickets/01")}
        return dict(self.references)

    def close(self, ticket: object, receipt: object, *, marker: str | None = None) -> None:
        self.close_calls += 1
        self.external.setdefault("marker", marker)
        if not self.external.get("closed"):
            self.external["closed"] = True
            self.external["close_mutations"] = 1
            raise RuntimeError("tracker died after close")

    def observe_publication(
        self,
        plan: object,
        *,
        markers: object,
        receipts: object | None = None,
    ) -> tuple[dict[str, TrackerTicket], str | None] | None:
        return (
            {"01": TrackerTicket("01", "https://tracker.example/tickets/01")},
            "https://tracker.example/spec",
        )

    def observe_close(
        self,
        ticket: object,
        receipt: object,
        *,
        marker: str,
    ) -> bool:
        return bool(self.external.get("closed"))


class CrashAfterFirstOfTwoTracker:
    def __init__(self, external: dict[str, object]) -> None:
        self.external = external
        self.references: dict[str, TrackerTicket] = {}
        self.spec_url: str | None = None

    def publish(self, plan: object, *, markers: object | None = None) -> dict[str, TrackerTicket]:
        self.spec_url = "https://tracker.example/spec"
        self.references = {
            ticket_id: TrackerTicket(
                ticket_id,
                f"https://tracker.example/tickets/{ticket_id}",
            )
            for ticket_id in ("01", "02")
        }
        return dict(self.references)

    def close(
        self,
        ticket: DeliveryTicket,
        receipt: TicketReceipt,
        *,
        marker: str | None = None,
    ) -> None:
        closed = self.external.setdefault("closed", set())
        assert isinstance(closed, set)
        if ticket.ticket_id not in closed:
            closed.add(ticket.ticket_id)
            key = f"close_mutations_{ticket.ticket_id}"
            self.external[key] = int(self.external.get(key, 0)) + 1
        if ticket.ticket_id == "01" and not self.external.get("crashed"):
            self.external["crashed"] = True
            raise RuntimeError("process died after closing ticket 01")

    def observe_publication(
        self,
        plan: object,
        *,
        markers: object,
        receipts: object | None = None,
    ) -> tuple[dict[str, TrackerTicket], str | None]:
        return (
            {
                ticket_id: TrackerTicket(
                    ticket_id,
                    f"https://tracker.example/tickets/{ticket_id}",
                )
                for ticket_id in ("01", "02")
            },
            "https://tracker.example/spec",
        )

    def observe_close(
        self,
        ticket: DeliveryTicket,
        receipt: TicketReceipt,
        *,
        marker: str,
    ) -> bool:
        closed = self.external.get("closed", set())
        return isinstance(closed, set) and ticket.ticket_id in closed


class CrashLocalMarkdownTracker(LocalMarkdownTracker):
    def __init__(self, root: Path, external: dict[str, object]) -> None:
        super().__init__(root)
        self.external = external

    def close(
        self,
        ticket: object,
        receipt: object,
        *,
        marker: str | None = None,
    ) -> None:
        super().close(ticket, receipt, marker=marker)
        self.external["close_calls"] = int(self.external.get("close_calls", 0)) + 1
        if not self.external.get("close_crashed"):
            self.external["close_crashed"] = True
            raise RuntimeError("legacy process died after close")


class StableTracker:
    def __init__(self, external: dict[str, object] | None = None) -> None:
        self.external = {} if external is None else external
        self.references: dict[str, TrackerTicket] = {}
        self.spec_url: str | None = None
        self.publish_calls = 0
        self.close_calls = 0

    def publish(self, plan: object, *, markers: object | None = None) -> dict[str, TrackerTicket]:
        self.publish_calls += 1
        if not self.external.get("published"):
            self.external["published"] = True
            self.external["publish_mutations"] = int(self.external.get("publish_mutations", 0)) + 1
        self.spec_url = "https://tracker.example/spec"
        self.references = {"01": TrackerTicket("01", "https://tracker.example/tickets/01")}
        return dict(self.references)

    def close(self, ticket: object, receipt: object, *, marker: str | None = None) -> None:
        self.close_calls += 1
        if not self.external.get("closed"):
            self.external["closed"] = True
            self.external["close_mutations"] = int(self.external.get("close_mutations", 0)) + 1

    def observe_publication(
        self,
        plan: object,
        *,
        markers: object,
        receipts: object | None = None,
    ) -> tuple[dict[str, TrackerTicket], str | None] | None:
        if not self.external.get("published"):
            return None
        return (
            {"01": TrackerTicket("01", "https://tracker.example/tickets/01")},
            "https://tracker.example/spec",
        )

    def observe_close(
        self,
        ticket: object,
        receipt: object,
        *,
        marker: str,
    ) -> bool:
        return bool(self.external.get("closed"))


class AdoptingTracker(StableTracker):
    def __init__(self) -> None:
        super().__init__()
        self.adopt_calls = 0

    def adopt(
        self,
        plan: object,
        *,
        references: dict[str, TrackerTicket],
        spec_url: str | None,
        markers: object,
        receipts: dict[str, TicketReceipt] | None = None,
        require_closed: bool = False,
    ) -> dict[str, TrackerTicket]:
        self.adopt_calls += 1
        self.external["published"] = True
        self.references = dict(references)
        self.spec_url = spec_url
        return dict(references)

    def inspect_adoption(
        self,
        plan: object,
        *,
        references: dict[str, TrackerTicket],
        spec_url: str | None,
        markers: object,
        receipts: dict[str, TicketReceipt],
        require_closed: bool = False,
    ) -> None:
        return None

    def observe_publication(
        self,
        plan: object,
        *,
        markers: object,
        receipts: object | None = None,
    ) -> tuple[dict[str, TrackerTicket], str | None] | None:
        return (
            None
            if self.adopt_calls == 0
            else super().observe_publication(
                plan,
                markers=markers,
                receipts=receipts,
            )
        )


class RepairCrashDispatcher(CompleteDispatcher):
    def __init__(self, external: dict[str, object]) -> None:
        super().__init__()
        self.external = external

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        if "受限 harness router" in prompt:
            return super().dispatch(
                workspace,
                harness,
                prompt,
                timeout_seconds=timeout_seconds,
                agent_name=agent_name,
            )
        if "Standards axis only" in prompt:
            self.prompts.append(prompt)
            findings: list[dict[str, str]] = []
            if not (workspace / "repair.txt").is_file():
                findings.append(
                    {
                        "severity": "must-fix",
                        "summary": "The repair marker is missing.",
                        "evidence": "repair.txt:1",
                        "source": "Accepted criterion requires the repair marker.",
                    }
                )
            _artifact_path(prompt).write_text(
                json.dumps({"standards": findings}),
                encoding="utf-8",
            )
            return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")
        if "Adjudicate independent review findings" in prompt:
            self.prompts.append(prompt)
            _artifact_path(prompt).write_text(
                json.dumps(
                    {
                        "accepted": ["standards:1"],
                        "dismissed": [],
                        "rationale": "The cited marker is required.",
                    }
                ),
                encoding="utf-8",
            )
            return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")
        if "Repair the accepted findings" in prompt:
            self.prompts.append(prompt)
            marker = workspace / "repair.txt"
            marker.write_text("repaired once\n", encoding="utf-8")
            _git(workspace, "add", marker.name)
            _git(workspace, "commit", "-m", "fix: repair accepted finding")
            commit = _git(workspace, "rev-parse", "HEAD").stdout.strip()
            before = _git(workspace, "rev-parse", "HEAD^").stdout.strip()
            if not self.external.get("omit_repair_receipt"):
                receipt = _repair_receipt_path(prompt)
                receipt.parent.mkdir(parents=True, exist_ok=True)
                receipt.write_text(
                    json.dumps(
                        {
                            "round": 1,
                            "before_commit": before,
                            "commit": commit,
                        }
                    ),
                    encoding="utf-8",
                )
            self.external["repair_commits"] = int(self.external.get("repair_commits", 0)) + 1
            if not self.external.get("disable_repair_crash") and not self.external.get(
                "repair_crashed"
            ):
                self.external["repair_crashed"] = True
                raise RuntimeError("worker died after repair commit")
            return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p2")
        return super().dispatch(
            workspace,
            harness,
            prompt,
            timeout_seconds=timeout_seconds,
            agent_name=agent_name,
        )


class ProxyResponseCrashDispatcher(CompleteDispatcher):
    def __init__(self, external: dict[str, object]) -> None:
        super().__init__()
        self.external = external
        self.blocked_workspace: Path | None = None
        self.blocked_prompt = ""
        self.blocked_agent = ""

    def dispatch(
        self,
        workspace: Path,
        harness: Harness,
        prompt: str,
        *,
        timeout_seconds: int,
        agent_name: str,
    ) -> DispatchOutcome:
        if "Act as the user's principal proxy" in prompt:
            self.prompts.append(prompt)
            _artifact_path(prompt).write_text(
                json.dumps(
                    {
                        "action": "answer",
                        "category": "spec-authorized",
                        "response": "Use the accepted local default.",
                        "rationale": "The specification already fixes the choice.",
                    }
                ),
                encoding="utf-8",
            )
            return DispatchOutcome(agent_name, AgentState.DONE, False, "w1:p3")
        if "Implement exactly one accepted delivery ticket" in prompt and not self.external.get(
            "responded"
        ):
            self.prompts.append(prompt)
            self.blocked_workspace = workspace
            self.blocked_prompt = prompt
            self.blocked_agent = agent_name
            return DispatchOutcome(
                agent_name,
                AgentState.BLOCKED,
                False,
                "w1:p2",
                "agent_blocked",
            )
        return super().dispatch(
            workspace,
            harness,
            prompt,
            timeout_seconds=timeout_seconds,
            agent_name=agent_name,
        )

    def read_agent(self, workspace: Path, name: str, *, lines: int = 120) -> str:
        return "Which accepted local default should I use?"

    def inspect_agent(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
    ) -> DispatchOutcome | None:
        if name != self.blocked_agent:
            return DispatchOutcome(name, AgentState.DONE, True, "w1:p3")
        if not self.external.get("responded"):
            return DispatchOutcome(name, AgentState.BLOCKED, True, "w1:p2")
        return DispatchOutcome(name, AgentState.DONE, True, "w1:p2")

    def respond(
        self,
        workspace: Path,
        name: str,
        harness: Harness,
        response: str,
        *,
        timeout_seconds: int,
    ) -> DispatchOutcome:
        if self.blocked_workspace is None or not self.blocked_prompt:
            raise AssertionError("missing blocked implementation context")
        self.external["response_calls"] = int(self.external.get("response_calls", 0)) + 1
        changed = self.blocked_workspace / "slice-01.txt"
        changed.write_text("implemented after proxy response\n", encoding="utf-8")
        _git(self.blocked_workspace, "add", changed.name)
        _git(self.blocked_workspace, "commit", "-m", "feat: implement proxied slice")
        commit = _git(self.blocked_workspace, "rev-parse", "HEAD").stdout.strip()
        receipt = _receipt_path(self.blocked_prompt)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(
            json.dumps(
                {
                    "ticket_id": "01",
                    "commit": commit,
                    "acceptance": [
                        {
                            "criterion": "The slice is committed once.",
                            "passed": True,
                            "evidence": "slice-01.txt exists",
                        }
                    ],
                    "checks": ["scripted validation passed"],
                    "summary": "Implemented after the proxy response.",
                }
            ),
            encoding="utf-8",
        )
        self.external["responded"] = True
        if not self.external.get("response_crashed"):
            self.external["response_crashed"] = True
            raise RuntimeError("process died after proxy response")
        return DispatchOutcome(name, AgentState.DONE, True, "w1:p2")


class DeliveryJournalTests(unittest.TestCase):
    def test_concurrent_loser_cannot_mutate_an_owned_delivery_run(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            owner_dispatcher = BlockingDispatcher()
            loser_dispatcher = BlockingDispatcher()
            owner = StandardizedDelivery(
                config,
                dispatcher=owner_dispatcher,
                tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            loser = StandardizedDelivery(
                config,
                dispatcher=loser_dispatcher,
                tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            owner_error: list[BaseException] = []

            def run_owner() -> None:
                try:
                    owner.run(goal)
                except BaseException as exc:
                    owner_error.append(exc)

            thread = threading.Thread(target=run_owner)
            thread.start()
            self.assertTrue(owner_dispatcher.entered.wait(timeout=5))
            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            owner_payload = json.loads((run_root / "run-owner.json").read_text(encoding="utf-8"))
            journal_before = (run_root / "journal.jsonl").read_text(encoding="utf-8")

            with self.assertRaisesRegex(DeliveryError, "delivery_run_active"):
                loser.run(goal)

            self.assertEqual(loser_dispatcher.calls, 0)
            self.assertEqual(
                (run_root / "journal.jsonl").read_text(encoding="utf-8"),
                journal_before,
            )
            self.assertRegex(owner_payload["owner_token"], r"^[0-9a-f]{32}$")
            self.assertGreater(owner_payload["lease_deadline"], owner_payload["last_renewed_at"])
            events = [json.loads(line) for line in journal_before.splitlines()]
            self.assertEqual(
                [event["sequence"] for event in events], list(range(1, len(events) + 1))
            )
            self.assertEqual(events[0]["event"], "owner_acquired")

            owner_dispatcher.release.set()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertEqual(len(owner_error), 1)
            self.assertRegex(str(owner_error[0]), "stop active delivery")

    def test_expired_owner_is_recovered_after_process_death(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            process = multiprocessing.get_context("fork").Process(
                target=_crash_delivery,
                args=(config, goal),
            )
            process.start()
            process.join(timeout=5)
            self.assertEqual(process.exitcode, 77)
            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            crashed_owner = json.loads((run_root / "run-owner.json").read_text(encoding="utf-8"))
            self.assertEqual(crashed_owner["status"], "active")

            unexpired_dispatcher = StoppingDispatcher()
            unexpired = StandardizedDelivery(
                config,
                dispatcher=unexpired_dispatcher,
                tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
                lease_seconds=0.75,
                clock=lambda: crashed_owner["lease_deadline"] - 0.05,
            )
            with self.assertRaisesRegex(DeliveryError, "delivery_run_active"):
                unexpired.run(goal)
            self.assertEqual(unexpired_dispatcher.calls, 0)

            recovered_dispatcher = StoppingDispatcher()
            recovered = StandardizedDelivery(
                config,
                dispatcher=recovered_dispatcher,
                tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
                lease_seconds=0.75,
                clock=lambda: crashed_owner["lease_deadline"] + 0.05,
            )
            with self.assertRaisesRegex(RuntimeError, "stop recovered delivery"):
                recovered.run(goal)

            events = [
                json.loads(line)
                for line in (run_root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(recovered_dispatcher.calls, 1)
            self.assertEqual(
                [event["sequence"] for event in events], list(range(1, len(events) + 1))
            )
            owner_events = [
                event for event in events if event["event"] in {"owner_acquired", "owner_released"}
            ]
            self.assertEqual(
                [event["event"] for event in owner_events],
                [
                    "owner_acquired",
                    "owner_acquired",
                    "owner_released",
                ],
            )
            self.assertNotEqual(
                owner_events[0]["owner_token"],
                owner_events[1]["owner_token"],
            )
            self.assertEqual(
                owner_events[1]["details"]["previous_owner"],
                owner_events[0]["owner_token"],
            )

    def test_owner_transitions_converge_after_each_physical_write(self) -> None:
        cases = (
            ("acquire", "journal"),
            ("acquire", "projection"),
            ("release", "journal"),
            ("release", "projection"),
        )
        for transition, physical_write in cases:
            with (
                self.subTest(
                    transition=transition,
                    physical_write=physical_write,
                ),
                tempfile.TemporaryDirectory() as temporary,
            ):
                run_root = Path(temporary).resolve() / "delivery-run"
                run_root.mkdir()
                now = [100.0]
                tokens = iter(("1" * 32, "2" * 32))
                interrupted = [False]
                target_event = "owner_acquired" if transition == "acquire" else "owner_released"
                target_status = "active" if transition == "acquire" else "released"
                patcher = (
                    patch.object(
                        DeliveryJournal,
                        "_persist_event",
                        _interrupt_owner_event(
                            DeliveryJournal._persist_event,
                            target_event,
                            interrupted,
                        ),
                    )
                    if physical_write == "journal"
                    else patch.object(
                        DeliveryJournal,
                        "_write_owner",
                        _interrupt_owner_projection(
                            DeliveryJournal._write_owner,
                            target_status,
                            interrupted,
                        ),
                    )
                )
                with (
                    patcher,
                    self.assertRaisesRegex(
                        RuntimeError,
                        "owner transition interruption",
                    ),
                    DeliveryJournal.claim(
                        run_root,
                        "a" * 12,
                        5.0,
                        error_type=DeliveryError,
                        clock=lambda current_now=now: current_now[0],
                        token_factory=tokens.__next__,
                    ),
                ):
                    pass
                self.assertTrue(interrupted[0])
                now[0] = 200.0

                with DeliveryJournal.claim(
                    run_root,
                    "a" * 12,
                    5.0,
                    error_type=DeliveryError,
                    clock=lambda current_now=now: current_now[0],
                    token_factory=tokens.__next__,
                ):
                    pass

                DeliveryJournal(
                    run_root,
                    "a" * 12,
                    "f" * 32,
                    5.0,
                    error_type=DeliveryError,
                    clock=lambda current_now=now: current_now[0],
                )
                owner = json.loads((run_root / "run-owner.json").read_text(encoding="utf-8"))
                self.assertEqual(owner["status"], "released")
                self.assertEqual(owner["owner_token"], "2" * 32)

    def test_tracker_publication_replays_from_intent_without_duplication(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            first_tracker = CrashOnceTracker(external)
            first = StandardizedDelivery(
                config,
                dispatcher=PlanningThenStoppingDispatcher(),
                tracker=first_tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )

            with self.assertRaisesRegex(RuntimeError, "tracker died after publication"):
                first.run(goal)

            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            after_crash = [
                json.loads(line)
                for line in (run_root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            tracker_events = [
                event for event in after_crash if event["operation_key"] == "tracker:publish"
            ]
            self.assertEqual(
                [event["event"] for event in tracker_events],
                ["effect_intent", "effect_started"],
            )
            marker = tracker_events[0]["details"]["markers"]["spec"]
            self.assertRegex(
                marker,
                r"^<!-- herdr-delivery:run=[0-9a-f]{12}:" r"nonce=[0-9a-f]{32}:kind=spec -->$",
            )

            second_tracker = CrashOnceTracker(external)
            second = StandardizedDelivery(
                config,
                dispatcher=PlanningThenStoppingDispatcher(),
                tracker=second_tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "stop after tracker recovery"):
                second.run(goal)

            recovered = [
                json.loads(line)
                for line in (run_root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            tracker_events = [
                event for event in recovered if event["operation_key"] == "tracker:publish"
            ]
            self.assertEqual(
                [event["event"] for event in tracker_events],
                ["effect_intent", "effect_started", "effect_confirmed"],
            )
            self.assertEqual(first_tracker.publish_calls, 1)
            self.assertEqual(second_tracker.publish_calls, 0)
            self.assertTrue((run_root / "tracker-publication.json").is_file())

    def test_tracker_publish_stage_crash_before_intent_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            config = replace(
                config,
                standardized_delivery=replace(
                    config.standardized_delivery,
                    tracker_backend=TrackerBackend.GITHUB,
                    github_repository="owner/project",
                ),
            )
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            external: dict[str, object] = {}
            first_tracker = StableTracker(external)
            first = StandardizedDelivery(
                config,
                dispatcher=PlanningThenStoppingDispatcher(),
                tracker=first_tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )

            original_reconcile = DeliveryJournal.reconcile

            def crash_before_intent(
                journal: DeliveryJournal,
                effect: DeliveryEffect,
            ) -> dict[str, object]:
                if effect.key == "tracker:publish":
                    raise TrackerPublishInterrupted()
                return original_reconcile(journal, effect)

            with (
                patch.object(DeliveryJournal, "reconcile", crash_before_intent),
                self.assertRaises(TrackerPublishInterrupted),
            ):
                first.run(goal)

            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            state = json.loads((run_root / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["stage"], "tracker-publish")
            self.assertEqual(first_tracker.publish_calls, 0)

            second_tracker = StableTracker(external)
            second = StandardizedDelivery(
                config,
                dispatcher=PlanningThenStoppingDispatcher(),
                tracker=second_tracker,
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "stop after tracker recovery"):
                second.run(goal)

            self.assertEqual(second_tracker.publish_calls, 1)
            self.assertTrue((run_root / "tracker-publication.json").is_file())

    def test_cross_kind_effect_conflict_stays_loadable(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve() / "delivery-run"
            run_root.mkdir()
            intent = {"markers": {"nonce": "a" * 32}}
            confirmed = {"tickets": {"01": "https://tracker.example/tickets/01"}}

            def matched(
                expected: dict[str, object] | None,
                started: bool,
            ) -> DeliveryEffectObservation:
                return DeliveryEffectObservation(
                    DeliveryEffectState.MATCHED,
                    dict(confirmed),
                )

            with DeliveryJournal.claim(
                run_root,
                "a" * 12,
                5.0,
                error_type=DeliveryError,
            ) as journal:
                journal.reconcile(
                    DeliveryEffect(
                        key="tracker:publish",
                        kind="tracker.publish",
                        intent=intent,
                        observe=matched,
                        apply=lambda: dict(confirmed),
                    )
                )
                with self.assertRaisesRegex(
                    DeliveryError,
                    "delivery_recovery_conflict:result.publish",
                ):
                    journal.reconcile(
                        DeliveryEffect(
                            key="tracker:publish",
                            kind="result.publish",
                            intent=intent,
                            observe=matched,
                            apply=lambda: dict(confirmed),
                        )
                    )

            reloaded = DeliveryJournal(
                run_root,
                "a" * 12,
                "f" * 32,
                5.0,
                error_type=DeliveryError,
            )
            self.assertEqual(reloaded._intent_details("tracker:publish"), intent)
            self.assertEqual(
                reloaded.require_confirmed("tracker:publish"),
                confirmed,
            )
            conflict_kinds = [
                event.effect_kind
                for event in reloaded._events
                if event.event == "effect_conflict"
            ]
            self.assertEqual(conflict_kinds, ["result.publish"])

    def test_agent_artifact_recovery_does_not_repeat_the_prompt(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            crashed_dispatcher = CrashAfterPlanDispatcher()
            first = StandardizedDelivery(
                config,
                dispatcher=crashed_dispatcher,
                tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "controller died after plan artifact"):
                first.run(goal)

            resumed_dispatcher = PlanningThenStoppingDispatcher()
            resumed = StandardizedDelivery(
                config,
                dispatcher=resumed_dispatcher,
                tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                controller_harness=Harness.DROID,
                worker_harnesses=(Harness.DROID,),
            )
            with self.assertRaisesRegex(RuntimeError, "stop after tracker recovery"):
                resumed.run(goal)

            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            plan_events = [
                json.loads(line)
                for line in (run_root / "journal.jsonl").read_text(encoding="utf-8").splitlines()
                if json.loads(line)["operation_key"] == "agent:artifact:plan"
            ]
            self.assertEqual(crashed_dispatcher.calls, 1)
            self.assertEqual(resumed_dispatcher.calls, 1)
            self.assertEqual(
                [event["event"] for event in plan_events],
                ["effect_intent", "effect_started", "effect_confirmed"],
            )

    def test_pending_artifact_stops_while_named_agent_is_working(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "controller died after plan artifact"):
                StandardizedDelivery(
                    config,
                    dispatcher=CrashAfterPlanDispatcher(),
                    tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)

            dispatcher = WorkingAgentDispatcher()
            with self.assertRaisesRegex(
                DeliveryError,
                "delivery_recovery_conflict:agent.dispatch",
            ):
                StandardizedDelivery(
                    config,
                    dispatcher=dispatcher,
                    tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            self.assertEqual(dispatcher.calls, 0)

    def test_duplicate_journal_keys_fail_before_external_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "stop recovered delivery"):
                StandardizedDelivery(
                    config,
                    dispatcher=StoppingDispatcher(),
                    tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            journal_path = run_root / "journal.jsonl"
            lines = journal_path.read_text(encoding="utf-8").splitlines()
            lines[0] = lines[0].replace(
                '"sequence": 1',
                '"sequence": 1, "sequence": 1',
                1,
            )
            journal_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            dispatcher = StoppingDispatcher()

            with self.assertRaisesRegex(DeliveryError, "delivery_journal_invalid"):
                StandardizedDelivery(
                    config,
                    dispatcher=dispatcher,
                    tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            self.assertEqual(dispatcher.calls, 0)

    def test_effect_from_a_non_owner_token_invalidates_the_journal(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary).resolve() / "repository"
            _initialize_repository(repository)
            config = _workflow(repository)
            goal = repository / "goal.md"
            goal.write_text("Deliver one recoverable slice.", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "stop recovered delivery"):
                StandardizedDelivery(
                    config,
                    dispatcher=StoppingDispatcher(),
                    tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            run_root = next(config.standardized_delivery.artifact_root.iterdir())
            journal_path = run_root / "journal.jsonl"
            events = [
                json.loads(line) for line in journal_path.read_text(encoding="utf-8").splitlines()
            ]
            effect = next(event for event in events if event["event"] == "effect_intent")
            effect["owner_token"] = "f" * 32
            journal_path.write_text(
                "\n".join(json.dumps(event, sort_keys=True) for event in events) + "\n",
                encoding="utf-8",
            )
            dispatcher = StoppingDispatcher()

            with self.assertRaisesRegex(DeliveryError, "delivery_journal_invalid"):
                StandardizedDelivery(
                    config,
                    dispatcher=dispatcher,
                    tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
                    controller_harness=Harness.DROID,
                    worker_harnesses=(Harness.DROID,),
                ).run(goal)
            self.assertEqual(dispatcher.calls, 0)

    def test_claim_preserves_owner_snapshot_when_lease_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            (run_root / "journal.jsonl").write_text(
                json.dumps(
                    {
                        "sequence": 1,
                        "run_id": "a" * 12,
                        "owner_token": "1" * 32,
                        "recorded_at": 50.0,
                        "event": "owner_acquired",
                        "operation_key": None,
                        "effect_kind": None,
                        "details": {
                            "status": "active",
                            "last_renewed_at": 50.0,
                            "lease_deadline": 100.0,
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            sentinel = b'{"status": "active", "sentinel": true}\n'
            (run_root / "run-owner.json").write_bytes(sentinel)

            with self.assertRaisesRegex(DeliveryError, "delivery_run_active"):
                with DeliveryJournal.claim(
                    run_root,
                    "a" * 12,
                    60,
                    error_type=DeliveryError,
                    clock=lambda: 60.0,
                ):
                    pass

            self.assertEqual((run_root / "run-owner.json").read_bytes(), sentinel)

    def test_release_failure_does_not_mask_the_primary_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            caught: RuntimeError | None = None

            with patch.object(
                DeliveryJournal,
                "release",
                side_effect=DeliveryError("delivery_owner_lost"),
            ):
                try:
                    with DeliveryJournal.claim(
                        run_root,
                        "a" * 12,
                        60,
                        error_type=DeliveryError,
                    ):
                        raise RuntimeError("primary failure")
                except RuntimeError as exc:
                    caught = exc

            self.assertIsNotNone(caught)
            assert caught is not None
            self.assertEqual(str(caught), "primary failure")
            self.assertTrue(
                any("delivery_owner_lost" in note for note in caught.__notes__),
                caught.__notes__,
            )

    def test_oversized_journal_fails_with_stable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            with DeliveryJournal.claim(
                run_root,
                "a" * 12,
                60,
                error_type=DeliveryError,
            ):
                pass

            with (
                patch.object(DeliveryJournal, "_JOURNAL_MAX_BYTES", 16),
                self.assertRaisesRegex(DeliveryError, "delivery_journal_too_large"),
            ):
                DeliveryJournal(
                    run_root,
                    "a" * 12,
                    "f" * 32,
                    60,
                    error_type=DeliveryError,
                )

    def test_pending_effects_survive_journal_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            run_root = Path(temporary).resolve()
            with DeliveryJournal.claim(
                run_root,
                "a" * 12,
                60,
                error_type=DeliveryError,
            ) as journal:
                journal.record_intent(
                    DeliveryEffect(
                        key="agent:artifact:plan",
                        kind="agent.dispatch",
                        intent={"role": "plan"},
                        observe=lambda expected, started: DeliveryEffectObservation(
                            DeliveryEffectState.ABSENT
                        ),
                        apply=lambda: {},
                    )
                )

            with DeliveryJournal.claim(
                run_root,
                "a" * 12,
                60,
                error_type=DeliveryError,
            ) as reloaded:
                pending = reloaded.pending_effects()

            self.assertEqual([effect.key for effect in pending], ["agent:artifact:plan"])
            self.assertFalse(pending[0].started)


def _workflow(repository: Path):
    config = load_workflow(REPO_ROOT / "workflows/multi-harness.toml")
    delivery = replace(
        config.standardized_delivery,
        artifact_root=repository / ".orchestrator/deliveries",
        tracker_root=repository / ".scratch/delivery",
        wayfinder=WayfinderMode.NEVER,
    )
    return replace(
        config,
        workspace=repository,
        state_db=repository / ".orchestrator/state.db",
        standardized_delivery=delivery,
    )


def _mutate_final_prerequisite(
    run_root: Path,
    external: dict[str, object],
    mutation: str,
) -> None:
    if mutation == "receipt":
        (run_root / "receipts/ticket-01.json").unlink()
    elif mutation == "tracker-close":
        external["closed"] = False
    elif mutation == "review":
        (run_root / "reviews/round-1/standards.json").write_text(
            json.dumps({"standards": [], "concurrent": True}),
            encoding="utf-8",
        )
    else:
        raise AssertionError(f"unknown mutation: {mutation}")


def _initialize_repository(repository: Path) -> None:
    repository.mkdir()
    _git(repository, "init", "-b", "main")
    _git(repository, "config", "user.name", "Test User")
    _git(repository, "config", "user.email", "test@example.com")
    (repository / "README.md").write_text("test repo\n", encoding="utf-8")
    _git(repository, "add", "README.md")
    _git(repository, "commit", "-m", "chore: initialize")


def _crash_delivery(config: WorkflowConfig, goal: Path) -> None:
    StandardizedDelivery(
        config,
        dispatcher=ExitDispatcher(),
        tracker=LocalMarkdownTracker(config.standardized_delivery.tracker_root),
        controller_harness=Harness.DROID,
        worker_harnesses=(Harness.DROID,),
        lease_seconds=0.75,
    ).run(goal)


def _delivery_plan() -> dict[str, object]:
    return {
        "slug": "journal-delivery",
        "title": "Journal delivery",
        "problem_statement": "A recoverable slice is missing.",
        "solution": "Deliver the slice once.",
        "user_stories": ["As an operator, I can recover the run."],
        "implementation_decisions": ["Use the delivery journal."],
        "testing_decisions": ["Restart at the tracker boundary."],
        "out_of_scope": [],
        "further_notes": [],
        "seams": ["StandardizedDelivery.run"],
        "tickets": [
            {
                "id": "01",
                "title": "Deliver the slice",
                "what_to_build": "Commit one recoverable slice.",
                "blocked_by": [],
                "acceptance_criteria": ["The slice is committed once."],
            }
        ],
    }


def _two_ticket_plan() -> dict[str, object]:
    plan = _delivery_plan()
    plan["tickets"] = [
        {
            "id": ticket_id,
            "title": f"Deliver slice {ticket_id}",
            "what_to_build": f"Commit recoverable slice {ticket_id}.",
            "blocked_by": [],
            "acceptance_criteria": [f"Slice {ticket_id} is committed once."],
        }
        for ticket_id in ("01", "02")
    ]
    return plan


def _artifact_path(prompt: str) -> Path:
    matches = re.findall(
        r"(?:Write only this UTF-8 JSON file|唯一允许写入的文件)(?::|：)\n([^\n]+)",
        prompt,
    )
    if not matches:
        raise AssertionError("artifact path missing")
    path = Path(matches[-1].strip())
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _receipt_path(prompt: str) -> Path:
    match = re.search(
        r"write only this additional UTF-8 JSON artifact:\n([^\n]+)",
        prompt,
    )
    if match is None:
        raise AssertionError("receipt path missing")
    return Path(match.group(1).strip())


def _repair_receipt_path(prompt: str) -> Path:
    match = re.search(
        r"After committing, write only this additional UTF-8 JSON artifact:\n([^\n]+)",
        prompt,
    )
    if match is None:
        raise AssertionError("repair receipt path missing")
    return Path(match.group(1).strip())


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )


if __name__ == "__main__":
    unittest.main()
