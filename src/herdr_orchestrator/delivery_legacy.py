from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from herdr_orchestrator.delivery_protocol import TicketReceipt
from herdr_orchestrator.tracker import TrackerTicket


def legacy_migration_payload(
    run_root: Path,
    references: dict[str, TrackerTicket],
    spec_url: str | None,
    receipts: dict[str, TicketReceipt],
    *,
    file_sha256: Callable[[Path], str],
) -> dict[str, object]:
    return {
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
