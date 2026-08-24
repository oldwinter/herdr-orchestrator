---
name: standardized-delivery
description: Run the opt-in principal-proxy engineering pipeline only when the user explicitly invokes /standardized-delivery, /matt-workflow, or /wayfinder-delivery, or uses one of these exact phrases as a requested workflow: “标准化交付”, “完整工程流程”, “Matt workflow”, “Pocock workflow”, “Wayfinder 全流程”, “自主交付”. Ordinary coding requests never trigger it.
---

# Standardized Delivery

Run one bounded delivery through Wayfinder (only when fog warrants it), specification,
tracer-bullet tickets, isolated implementation, and independent review.

## Start

1. Confirm the request explicitly matches one trigger in the description. A normal request
   to implement, fix, review, plan, or orchestrate is outside this Skill.
2. Read [`references/workflow-contract.md`](references/workflow-contract.md).
3. Confirm `HERDR_ENV=1` and run the workflow's `doctor` command.
4. Capture the delivery goal in an ignored UTF-8 Markdown file under
   `.orchestrator/requests/`. Preserve requirements, targets, and explicit authority, but do
   not copy secrets into it.
5. Run:

   ```bash
   PYTHONPATH=src python3 -m herdr_orchestrator deliver \
     --workflow workflows/multi-harness.toml \
     --goal-file .orchestrator/requests/<goal>.md
   ```

   Add controller, worker, tracker, Wayfinder, concurrency, or review overrides only when
   the user supplied them. The TOML defaults are the normal path.

## Stop

- Exit `0`: report the integration branch and commit, ticket references, review rounds, and
  runtime artifact root. The command does not push, merge into the user's branch, or deploy.
- Exit `3`: read [`references/authority.md`](references/authority.md), report the escalation
  category and artifact root, then wait for the user.
- Any other non-zero exit: read [`references/recovery.md`](references/recovery.md), report
  the stable error, failed stage, and preserved worktrees. Never claim partial work succeeded.

Completion means every implementation ticket has a validated receipt and is closed, all
commits are merged into the isolated integration branch, and the final Standards and Spec
review has no accepted must-fix finding after at most two repair rounds.
