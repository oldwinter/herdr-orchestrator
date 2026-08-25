# Herdr Orchestrator Threat Model

Version: 1.0.0
Updated: 2026-08-25

## 1. System Overview

Herdr Orchestrator is a local-first Python 3.12+ control plane for interactive coding
agents. A deterministic coordinator loads a TOML workflow, accepts CLI or validated planner
tasks, stores durable queue state and receipts in SQLite, and controls agent terminals through
the local `herdr` CLI. An npm wrapper installs owned files into another Git checkout and forwards
runtime commands to the Python CLI. A loopback-only HTTP/SSE dashboard projects whitelisted
SQLite and Herdr state.

Major components:

- `src/herdr_orchestrator/config.py`, `catalog.py`, and `selection.py`: workflow and harness
  metadata validation.
- `runner.py` and `store.py`: queue ownership, leases, attempts, blocked recovery, routing, and
  garbage collection.
- `herdr.py`, `herdr_layout.py`, and `protocol.py`: subprocess boundary to Herdr and terminal
  lifecycle evidence.
- `delivery.py`, `tracker.py`, and `git_workspace.py`: opt-in worktree delivery and local or
  GitHub issue tracking.
- `dashboard/`: read-only loopback HTTP/SSE projection.
- `bin/herdr-orchestrator.mjs`: npm installation, ownership manifest, upgrade, uninstall, and
  runtime forwarding.

There is no application login layer. Authority derives from the operating-system user, the
current Herdr session, harness login state, local filesystem permissions, and explicit CLI
invocation.

## 2. Architecture and Trust Boundaries

### 2.1 Local caller to CLI

CLI arguments, prompt files, response files, workflow paths, and task receipt declarations are
partially trusted local input. Validation must occur before they become filesystem paths, SQLite
queries, harness prompts, pane input, or Git/tracker operations. The process inherits powerful
local credentials, so local input is not harmless.

### 2.2 Planner and agent output to deterministic coordinator

Planner, router, topology, review, and worker output is untrusted model output. The coordinator
accepts only strict bounded schemas or explicit receipts. Model prose never directly becomes a
shell command or queue state transition. Output-prefix receipts are terminal evidence rather than
cryptographic authorship and must be scoped to a fresh settled turn.

### 2.3 Coordinator to SQLite

SQLite under `.orchestrator/` is the durable state authority. Parameterized SQL, transactions,
lease checks, attempt checks, and state preconditions protect state transitions. Anyone with the
same OS-user filesystem access can still tamper with the database, which is an accepted local
trust assumption.

### 2.4 Coordinator to Herdr and harnesses

Herdr is a local terminal runtime, not the policy controller. Agent names, pane IDs, workspace
IDs, cwd, lifecycle state, and state-change sequences cross this boundary. Responses to blocked
agents and pane cleanup require exact persisted ownership evidence. Harnesses may access files,
network services, and credentials according to their own runtime authority.

### 2.5 npm wrapper to target project

The wrapper writes only managed roots, records SHA-256 ownership, rejects path escape and symlink
redirection, and preserves user-modified files. The npm package itself is a supply-chain trust
boundary. Wrapper and manifest version skew must fail health checks.

### 2.6 Dashboard to browser

The dashboard is unauthenticated but binds only to loopback. It must not expose prompts,
environment variables, terminal output, credentials, or arbitrary database columns. Host-header
and bind validation are required. Browser access by another local process is within the local-user
trust zone.

### 2.7 Optional external systems

Harness providers, GitHub CLI, npm registry, and GitHub Actions are external boundaries. Ordinary
queue execution does not authorize push, merge, publication, messaging, production changes, or
permission changes. The GitHub tracker backend is limited to explicitly selected issue
operations for standardized delivery.

### 2.8 Continuous integration

Pull-request code is untrusted and runs only on ephemeral GitHub-hosted runners without write or
OIDC permissions. Persistent self-hosted runners are reserved for trusted `main` release planning.
Publishing remains GitHub-hosted because npm Trusted Publishing does not support self-hosted
runners. Actions are pinned to immutable commit SHAs and credentials are not persisted.

## 3. Attack Surface Inventory

| Surface | Untrusted inputs | Sensitive sinks | Primary controls |
| --- | --- | --- | --- |
| Python CLI | paths, IDs, harness names, response text, receipt values | SQLite, Herdr, filesystem | argparse choices, path containment, bounded values |
| Workflow loader | TOML paths and policy values | runtime configuration | schema checks, relative-path resolution, size/range limits |
| Planner/router | model-authored JSON and files | queue and topology decisions | strict schema, enabled-harness allowlist, bounded artifacts |
| Herdr adapter | CLI JSON, terminal snapshots, lifecycle states | pane input/close, success state | timeouts, sequence checks, identity/pane/cwd ownership |
| Durable store | job and outcome fields | SQLite state transitions | parameterized SQL, transactions, state/attempt checks |
| File receipts | configured relative path and agent-written bytes | success decision | containment, regular-file check, pre/post hash and size |
| Output receipts | terminal transcript | success decision | current-turn delta, prompt ambiguity rejection, settled-only verification |
| Installer | project path, manifest, existing files/symlinks | project filesystem | managed-root allowlist, SHA-256 ownership, symlink fail-closed |
| Dashboard | local HTTP requests | state snapshot | loopback bind, host validation, read-only whitelist |
| Tracker/delivery | goals, specs, model decisions | Git worktrees and GitHub issues | explicit opt-in, schemas, bounded loops, no implicit push/merge |

## 4. Critical Assets

- Local source code, uncommitted work, untracked files, worktrees, and Git branches.
- Queue integrity: job prompts, state, attempts, leases, blocked responses, receipts, and
  ownership records.
- Credentials inherited by harnesses or CLI tools, including provider, GitHub, and npm sessions.
- User-generated prompt and response-file content.
- Terminal sessions and panes belonging to the user or other agents.
- Package ownership manifest and managed target-project files.
- Delivery artifacts, review evidence, tracker references, and integration commits.

Secrets must remain in environment variables, keychains, or harness-native login state. They must
not be copied into code, logs, dashboard snapshots, receipts, or security reports.

## 5. Threat Analysis

### 5.1 Spoofing

**Threats:** A foreign agent adopts a deterministic name; a pane is moved or replaced; stale
SQLite metadata points to a different session; wrapper metadata claims another package's files.

**Mitigations:** Herdr operations validate agent name, kind, pane ID, workspace/cwd, and lifecycle
state. Cleanup and blocked resume require a creation receipt and unchanged pane. Manifests validate
package identity and schema. Remaining risk is same-OS-user tampering with both runtime and
database evidence. Severity: HIGH, likelihood: LOW under the local-user assumption.

### 5.2 Tampering

**Threats:** Path traversal or symlink redirection overwrites files; planner output injects a
command; SQL injection corrupts queue state; stale receipts falsely mark work complete; concurrent
resume or lease races alter attempts.

**Mitigations:** Paths reject absolute values and `..`, are resolved under explicit roots, and
managed paths reject symlinks. SQL uses placeholders. Planner/router/topology outputs use strict
schemas with allowlists. File receipts compare pre/post SHA-256 and size. Output receipts require
fresh transcript lines and reject exact prompt-line ambiguity. Transactions and state/attempt
preconditions serialize claims and resume. Severity: HIGH, likelihood: LOW to MEDIUM.

### 5.3 Repudiation

**Threats:** A blocked response, retry, cleanup, or external tracker change cannot be attributed;
raw prompts are omitted from logs by design.

**Mitigations:** SQLite receipts retain attempt, agent, pane, state, error code, placement, and
observation time. Delivery has a bounded decision ledger that intentionally excludes sensitive
response text. CLI JSON reports stable action and error fields. Accepted gap: there is no
cryptographic audit log, and ordinary local CLI invocations are attributable only to the OS user.
Severity: MEDIUM, likelihood: MEDIUM.

### 5.4 Information Disclosure

**Threats:** Prompts, response files, credentials, terminal transcripts, local paths, or provider
errors leak through dashboard, logs, tests, or reports.

**Mitigations:** Dashboard observers whitelist fields and never read prompt or pane output.
Runtime error summaries are bounded. Security rules prohibit secrets in code and responses.
Receipt checks inspect bounded terminal snapshots but do not persist transcripts. Accepted risk:
local CLI JSON includes operational paths and agent IDs visible to the invoking user. Severity:
HIGH for credentials, likelihood: LOW with current controls.

### 5.5 Denial of Service

**Threats:** Unbounded tasks, terminal waits, output reads, planner artifacts, HTTP clients, or
concurrency exhaust local resources; blocked jobs are mistaken for idle; repeated retries spin.

**Mitigations:** All Herdr waits and subprocesses have deadlines. Worker replicas, parallelism,
planner task counts, output lines, file sizes, attempts, and response loops are bounded. Blocked
jobs return `idle=false`. SQLite leases use bounded retry backoff. Dashboard snapshots are
read-only and polling is configured. Severity: MEDIUM, likelihood: MEDIUM.

### 5.6 Elevation of Privilege

**Threats:** Model output triggers shell execution or external side effects; ordinary queue work
silently gains standardized-delivery authority; cleanup closes user panes; a response bypasses
human approval.

**Mitigations:** Planner tasks cannot carry commands. Standardized delivery is exact-phrase/CLI
opt-in. Ordinary defaults forbid push, merge, publish, messaging, permission, and production
actions. Blocked resume requires explicit response-file invocation and exact ownership checks.
GC excludes blocked, active, worktree, reused, and foreign agents. Worktrees provide checkout
isolation but are explicitly not treated as a sandbox. Severity: CRITICAL, likelihood: LOW if
these policy seams remain intact.

## 6. Vulnerability Pattern Library

### 6.1 SQL injection

Vulnerable:

```python
connection.execute(f"SELECT * FROM jobs WHERE id = {job_id}")
```

Required:

```python
connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,))
```

Dynamic placeholder counts may be generated only from trusted collection length; values remain
parameters.

### 6.2 Command injection

Vulnerable:

```python
subprocess.run(f"herdr agent get {user_value}", shell=True)
```

Required:

```python
subprocess.run(["herdr", "agent", "get", validated_name], shell=False)
```

Never pass model-authored shell text to `shell=True`, `eval`, or `exec`.

### 6.3 Path traversal and symlink attacks

Vulnerable:

```python
target = root / user_path
target.write_text(content)
```

Required: reject absolute paths and `..`, resolve the candidate, prove
`candidate.is_relative_to(root.resolve())`, and reject symlinks for installer-owned paths before
reading or writing.

### 6.4 XSS and local dashboard disclosure

Vulnerable: interpolating job titles or errors into `innerHTML`, or exposing entire SQLite rows.

Required: use text nodes/escaping, whitelist projected fields, omit prompts and transcripts, bind
only to validated loopback addresses, and validate the Host header.

### 6.5 IDOR and terminal ownership

Vulnerable: closing or responding to a pane based only on a caller-supplied pane ID.

Required: correlate workflow job, deterministic owned agent name, persisted creation receipt,
current pane ID, cwd/workspace, and allowed lifecycle state before input or cleanup.

### 6.6 Authentication and authorization bypass

The project has no network authentication. Do not broaden dashboard binding or add mutating HTTP
routes without a separate authentication and CSRF design. Do not infer authority from an
available CLI credential; external writes require explicit user or delivery-scope authorization.

### 6.7 Receipt confusion

Vulnerable: accepting any historical transcript prefix or any pre-existing non-empty file.

Required: snapshot before dispatch, verify only after settled success states, require new
transcript evidence or changed file hash/size, and fail closed when prompt and receipt are
indistinguishable.

### 6.8 Secret disclosure

Never log environment mappings or raw exceptions containing prompts. Pass structured telemetry
through centralized sanitization, keep `.env` files ignored, maintain the reviewed secret
baseline, and require HTTPS for optional exporters.

### 6.9 Untrusted CI execution

Never run `pull_request` code on a persistent self-hosted runner. Jobs with `issues: write`,
`pull-requests: write`, `contents: write`, or `id-token: write` must not execute contributor code.
Keep trusted release planning and GitHub-hosted publishing separate.

## 7. Security Testing Strategy

- Unit-test path escape, symlink, schema, SQL state, lease, attempt, and ownership failures.
- Keep red/green tests for blocked prompt echo, prior-turn output, stale file receipts, pane
  mismatch, foreign/reused cleanup, and resume concurrency.
- Run `just check` for every change and real read-only Herdr probes for lifecycle changes.
- Scan npm tarballs for unintended files and compare wrapper/manifest versions.
- Test dashboard loopback and Host rejection, field whitelists, and absence of prompts.
- Assert that untrusted pull-request jobs remain GitHub-hosted and external-write jobs do not
  execute contributor code.
- Scan uncommitted and release diffs against this model before commit or publication.
- Never include real secrets or full terminal transcripts in test fixtures or findings.

## 8. Assumptions and Accepted Risks

- The OS account and local filesystem are trusted; this is not a multi-tenant security boundary.
- Herdr and installed harness binaries are trusted local dependencies.
- Worktrees isolate checkouts, not processes, credentials, network, ports, or the rest of the
  filesystem.
- Output-prefix receipts are conservative current-turn terminal evidence, not signed authorship or
  a general quality proof. File receipts prove freshness of bytes, not which process wrote them.
- A provider or harness can behave incorrectly after valid lifecycle evidence; task-specific
  receipts and review remain necessary.
- The dashboard is safe only while loopback-only and read-only.
- External systems can be unavailable or rate-limited; such failures must not be interpreted as
  authorization to weaken controls.

## 9. Changelog

- 1.0.0 (2026-08-25): Initial STRIDE model covering durable queue, Herdr lifecycle, receipts,
  blocked recovery, topology, installer, dashboard, delivery, tracker, and release boundaries.
