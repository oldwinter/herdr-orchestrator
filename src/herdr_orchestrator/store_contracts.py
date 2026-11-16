"""Enqueue identity and completion-policy validation for durable jobs."""

from __future__ import annotations

import sqlite3

from herdr_orchestrator.attempts import StoreError
from herdr_orchestrator.completion import CompletionPolicy
from herdr_orchestrator.model import Harness, NewJob, PlacementTarget, TaskReceipt


def _job_contract_matches(row: sqlite3.Row, job: NewJob) -> bool:
    completion_policy = _completion_policy(job.receipt, job.completion_policy)
    return all(
        (
            row["title"] == job.title,
            row["workspace"] == job.workspace,
            row["harness"] == job.harness.value,
            row["prompt"] == job.prompt,
            row["placement"] == (job.placement.value if job.placement is not None else None),
            row["receipt_kind"] == (job.receipt.kind.value if job.receipt is not None else None),
            row["receipt_value"] == (job.receipt.value if job.receipt is not None else None),
            row["completion_policy"] == completion_policy.value,
        )
    )


def _partial_job_contract_matches(
    row: sqlite3.Row,
    *,
    title: str,
    prompt: str,
    harness: Harness | None,
    placement: PlacementTarget | None,
    receipt: TaskReceipt | None,
    completion_policy: CompletionPolicy | None,
    workspace: str | None,
) -> bool:
    effective_policy = _completion_policy(receipt, completion_policy)
    return all(
        (
            row["title"] == title,
            row["workspace"] == workspace,
            row["prompt"] == prompt,
            harness is None or row["harness"] == harness.value,
            placement is None or row["placement"] == placement.value,
            row["receipt_kind"] == (receipt.kind.value if receipt is not None else None),
            row["receipt_value"] == (receipt.value if receipt is not None else None),
            row["completion_policy"] == effective_policy.value,
        )
    )


def _completion_policy(
    receipt: TaskReceipt | None,
    requested: CompletionPolicy | None,
) -> CompletionPolicy:
    if requested is None:
        return (
            CompletionPolicy.RECEIPT_V1
            if receipt is not None
            else CompletionPolicy.LEGACY_UNVERIFIED
        )
    if requested is CompletionPolicy.STRUCTURED_V2 and receipt is None:
        return requested
    if requested is CompletionPolicy.RECEIPT_V1 and receipt is not None:
        return requested
    if requested is CompletionPolicy.LEGACY_UNVERIFIED and receipt is None:
        return requested
    raise StoreError("completion_policy_invalid")
