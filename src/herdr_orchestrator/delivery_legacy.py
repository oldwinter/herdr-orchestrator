from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from herdr_orchestrator.delivery_protocol import (
    DeliveryPlan,
    TicketReceipt,
    load_ticket_receipt,
)
from herdr_orchestrator.tracker import TrackerTicket


def existing_ticket_receipts(
    run_root: Path,
    plan: DeliveryPlan,
    *,
    confirmed: Callable[[str], bool] | None = None,
) -> dict[str, TicketReceipt]:
    receipts: dict[str, TicketReceipt] = {}
    for ticket in plan.tickets:
        path = run_root / "receipts" / f"ticket-{ticket.ticket_id}.json"
        if not path.is_file():
            continue
        if confirmed is not None and not (
            confirmed(f"ticket:accept:{ticket.ticket_id}")
            and confirmed(f"git:merge:{ticket.ticket_id}")
        ):
            continue
        receipts[ticket.ticket_id] = load_ticket_receipt(path, ticket)
    return receipts


def completed_legacy_migration(payload: object) -> bool:
    return isinstance(payload, dict) and payload.get("completed") is True


def legacy_migration_payload(
    run_root: Path,
    references: dict[str, TrackerTicket],
    spec_url: str | None,
    receipts: dict[str, TicketReceipt],
    *,
    file_sha256: Callable[[Path], str],
    completed: bool = False,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "spec_url": spec_url,
        "tickets": {key: value.reference for key, value in references.items()},
        "receipts": {
            ticket_id: {
                "commit": receipt.commit,
                "sha256": file_sha256(run_root / "receipts" / f"ticket-{ticket_id}.json"),
            }
            for ticket_id, receipt in receipts.items()
        },
    }
    if completed:
        payload["completed"] = True
    return payload
