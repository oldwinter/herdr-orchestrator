---
name: herdr-orchestrator
description: "Use when a user asks to dispatch work across Herdr-backed coding harnesses with a durable queue, retries, receipts, topology-aware panes or worktrees, or the local operations dashboard."
---

# Herdr Orchestrator

Use the packaged CLI instead of assuming this Skill's checkout contains the runtime.

## Bootstrap

From the target Git repository, check for `.herdr-orchestrator/manifest.json`. If it is
missing, bootstrap the project:

```bash
npx --yes herdr-orchestrator install --project .
```

The installer selects locally available harness CLIs. To choose explicitly, repeat
`--harness`, for example `--harness droid --harness codex`.

Always run diagnostics before real dispatch:

```bash
npx --yes herdr-orchestrator doctor --project .
```

Exit code `1` is not success. Read the JSON `installation` and `runtime` checks and report
what is missing instead of bypassing them.

## Operate

Run durable queue commands through the same project-aware wrapper:

```bash
npx --yes herdr-orchestrator catalog --project .
npx --yes herdr-orchestrator status --project .
npx --yes herdr-orchestrator run --project . --once
npx --yes herdr-orchestrator dashboard --project .
```

Before enqueueing a task, write its prompt to a UTF-8 file in the target repository. Pass
runtime arguments after `--project`; the wrapper supplies the installed workflow path.

The orchestrator must run from a Herdr pane for real dispatch. Do not treat `blocked`,
`unknown`, timeout, or a pane that merely exists as success. Never push, merge, publish,
send, delete, change permissions, or touch production unless the user separately authorized
that exact action.

Use `upgrade` for a requested runtime update and `uninstall` only when the user explicitly
asks to remove it. Both preserve user-modified managed files and report them in JSON.
