# Distribution and installation

Herdr Orchestrator uses two installation layers plus a thin `herdr-manager` command package
over one project-local runtime contract.

## Agent Skill

Install the repository's portable Skill for one project:

```bash
npx skills add oldwinter/herdr-orchestrator \
  --skill herdr-orchestrator --agent '*' -y
```

Use `-g` for a user-level Skill. This layer only installs agent instructions. It does not
copy the Python control plane or create a workflow.

The portable Skill lives in `skills/herdr-orchestrator/`. The repository also exposes
opt-in standardized-delivery skills, so the install command names `--skill
herdr-orchestrator` explicitly and only copies that directory.

## Runtime bootstrap

From the target Git repository:

```bash
npx --yes herdr-orchestrator install --project .
```

The npm package has no runtime npm dependencies. Its executable:

1. detects installed harness CLIs, or accepts repeated `--harness` flags;
2. installs a project-relative workflow and only the selected harness profiles;
3. installs a fixed manual-manager workspace shared by the selected harnesses;
4. installs the portable Skill at `.agents/skills/herdr-orchestrator/` only when the target
   has no existing Skill router, or when `--install-skill` is explicit;
5. writes an ownership manifest with a SHA-256 hash for every managed file;
6. adds installer-managed roots to this repository's Git-local `info/exclude` without editing
   a tracked `.gitignore` or hiding an unmanaged Skill;
7. records the prior and desired inventories in a transaction journal before it changes a
   managed file, the ownership manifest, or the Git exclude block;
8. carries the Python package and invokes it with its packaged `src/` on `PYTHONPATH`.

Setup commands reject options they do not define.

Python is not copied or downloaded. The target machine must provide Python 3.12+ and Herdr.
No global install or elevated permission is required.

The packaged control plane starts new harness agents with fixed maximum-automation native
arguments: Droid `--auto high`, Grok `--always-approve --permission-mode
bypassPermissions`, Codex `--dangerously-bypass-approvals-and-sandbox
--dangerously-bypass-hook-trust`, pi `--approve`, Claude
`--dangerously-skip-permissions`, and Hermes `--yolo --accept-hooks`. These bypass local
approval, trust, hook, and sandbox prompts where each CLI supports doing so. Planner output
and task prompts cannot inject or override launch arguments. This execution policy does not
authorize push, merge, publish, messaging, worktree deletion, permission changes, or
production access.

Claude has no native flag that bypasses its first workspace-trust dialog. For a newly created
Claude agent only, the adapter sends one Enter when detection output contains the expected
execution root and all three stable built-in markers: `Accessing workspace:`,
`Quick safety check:`, and `Yes, I trust this folder`. It does not answer any other startup
block, login, secret, approval, or task question.

To bypass automatic harness detection:

```bash
npx --yes herdr-orchestrator install --project . \
  --harness droid \
  --harness codex
```

Supported names are `droid`, `grok`, `codex`, `pi`, `claude`, and `hermes`.

If `.agents/skills/` already exists, project Skill injection is opt-in:

```bash
npx --yes herdr-orchestrator install --project . --install-skill
```

`upgrade --skip-skill` removes only an unchanged Skill owned by the manifest. A modified or
independently installed Skill remains untouched.

## Managed project surface

| Path | Ownership |
| --- | --- |
| `.herdr-orchestrator/manifest.json` | Installer ownership, content hashes, and file modes |
| `.herdr-orchestrator/install-journal.json` | Active install, upgrade, or uninstall transaction |
| `.herdr-orchestrator/workflows/` | Portable project-relative workflow and prompts |
| `.herdr-orchestrator/profiles/` | Profiles for selected harnesses |
| `.herdr-orchestrator/manager/` | Fixed policy workspace for a manual manager session |
| `.agents/skills/herdr-orchestrator/` | Portable agent Skill |
| `.orchestrator/.gitignore` | Keeps durable runtime state out of Git |

The installer owns a marked block in the repository's Git-local exclude file for
`.herdr-orchestrator/`, `.orchestrator/`, and a project Skill only when that Skill is recorded
in the manifest. This keeps `git status` unchanged without hiding independently managed
content or modifying shared repository policy. A symlinked exclude file or project-relative
ancestor such as `.git` is rejected before project writes. A native linked worktree remains
supported when Git resolves the exclude into its external common Git directory. Uninstall removes
the block only when none of those roots remain.

Existing unmanaged files with different content cause `install` to stop before writing.
Existing unmanaged files with identical content are reused but not added to the npm
manifest. This keeps a Skill installed by `npx skills` under that tool's ownership.
Managed files changed by the user are preserved on reinstall, upgrade, and uninstall. A
partial reconciliation returns exit code `1` and reports those paths in `preserved`.
Manifest entries are restricted to the roots above, and symlinked managed paths are rejected.

## Installer recovery

Install, upgrade, and uninstall write `.herdr-orchestrator/install-journal.json` before
their first owned mutation. The journal records the transaction ID, package version, selected
harnesses, Skill choice, prior and desired inventories, endpoint digests and modes, ordered
operations, desired replacement bytes, and durable progress.

Each regular-file replacement uses a temporary file in the target directory. The installer
writes the bytes, sets the recorded mode, flushes the file, renames it over the validated
target, and flushes the directory. It rejects symlinks and non-regular targets before planning
and again during replay. The ownership manifest is the last semantic operation, after managed
files and the Git exclude block match the desired inventory.

The next install, upgrade, or uninstall finishes an active transaction before it performs
normal ownership classification. Replay checks the complete inventory before it writes. Every
participant must match either its recorded prior digest and mode or its desired digest and
mode. If a path matches neither endpoint, the command exits with
`installer_recovery_conflict: <path>`. It leaves both the journal and the conflicting bytes
unchanged.

Recovery always moves forward to the journal's desired inventory. Desired replacement bytes
are stored in the journal, so a newer package can finish a transaction written by an older
package before it plans its own update.

Schema version 1 journals written before mode tracking remain valid. Recovery treats a missing
mode as unknown, preserves the live mode when it replaces that file, and adopts the older
transaction-specific `.tmp` claim as the current owner claim. Install and upgrade replay may
finish a content-proven replacement while carrying the live mode onto its temporary file. For
any command, a digest-matching regular project deletion whose mode is unknown is identified as
an explicit preserved participant and rechecked at each boundary; a content mismatch remains a
recovery conflict. For install and upgrade, any such project deletion is a deliberate
compatibility boundary: recovery stops with a stable `installer_recovery_conflict` before any
additional target or manifest mutation and leaves the journal, owner claim, manifest, and
current bytes in place.
It does not infer a historical mode or perform an unprotected successor handoff; it requires
explicit operator resolution or reconstruction outside automatic recovery. For uninstall, the
Git exclude block is retained, each digest-matching unknown-mode project deletion is reported as
preserved and skipped, only the installer manifest is removed, the journal is retired, and the
command returns a partial result.
If install or upgrade is the entry point that completes such a partial legacy uninstall, it
returns that result with `recovered_command: "uninstall"` and does not start a new transaction;
the caller must invoke install or upgrade again explicitly. If all initially preserved project
targets disappear before retirement while the Git exclude block is still retained, recovery
returns a bounded conflict and keeps the journal; the next retry re-evaluates the live inventory
and removes the block when no retained project path remains. That frozen-set condition is recorded
as `progress.recovery_conflict: "legacy_exclude_coupling"`, so `doctor` continues to report
`journal-recovery:legacy_exclude_coupling` until a retry clears the marker or finds a new conflict.
If the exclude endpoint is already at its desired bytes while preserved project paths remain, or
is in any third state, recovery likewise stops with a conflict rather than claiming that the
block was retained.
An unjournaled file that appears under a managed root is also a conflict participant; doctor
reports the same `git-exclude:<path>` conflict and recovery leaves the journal and exclude
untouched until that caller entry is resolved.
Old manifests that omit `file_modes` are mode-unverified: existing files are reported as
`modified` (also listed in `mode_unverified`) and are preserved without overwrite or removal.
New journals and manifests record modes explicitly.

One owner claim remains next to the journal until the transaction finishes. If the owner
process is running, concurrent install, upgrade, or uninstall commands stop with
`installer_transaction_active` before they change the journal or a target. A new process
atomically adopts a dead owner claim before recovery.

`just test-installer-crash-matrix` runs the packed interruption matrix and legacy-removal
chains. `just check` and CI run these marked tests once as a required gate. The repeated
coverage and stability suites exclude only these heavy marked tests; all other installer tests
remain in those suites.

## Diagnostics

```bash
npx --yes herdr-orchestrator doctor --project .
```

`doctor` returns one JSON document with:

- `installation`: the package inventory, ownership-manifest claims, actual file digests, the
  managed Git exclude block, and any active journal;
- `runtime`: Python, Herdr, Git, profile checks, and a bounded real readiness turn for every
  selected harness;
- top-level `ok`: true only when both layers are healthy.

Exit code `1` means the installation or runtime needs attention. In particular, real dispatch
must run inside a Herdr pane with the expected `HERDR_*` environment. A harness readiness
status is one of `ready`, `auth_required`, `model_invalid`, `timeout`, `unavailable`, or
`error`; an executable in `PATH` alone is not ready. Doctor closes only probe agents it created.
Doctor never replays an installer transaction. An active journal makes installation health
false and remains available for the next install, upgrade, or uninstall command.

## Manual manager

For interactive oversight of the current Herdr session, install the frequent-use command once
from a source checkout:

```bash
just install-manager
herdr-manager         # Grok, then Codex, then Claude
herdr-manager claude  # selects Claude
```

The Just recipe invokes npm from a non-interactive shell, bypassing interactive wrappers that
rewrite `npm install --global .` as an invalid `mise use -g npm:.` package request. Once version
`0.1.3` or newer is published, `npm install --global herdr-orchestrator` is also supported.
Bare npm installation has no Herdr configuration side effects. `just install-manager` follows it
with the explicit `herdr-orchestrator manager-light install` opt-in.

Manager light requires Herdr 0.8.2 or newer. It links and enables the packaged plugin and owns one
marked `[ui.sidebar.agents]` block. Installation refuses malformed markers or any external Agent-row
table, validates a temporary candidate with `herdr config check`, and atomically renames the valid
candidate. Bytes outside the marker block are preserved. Inspect or remove it with:

```bash
herdr-orchestrator manager-light status
herdr-orchestrator manager-light uninstall
```

`manager-light uninstall` requires a running Herdr server. Herdr 0.8.2 has no offline unlink
operation. If the server is unavailable or candidate validation fails, the command exits nonzero
and leaves the plugin registry and configuration unchanged. Do not edit the Herdr plugin registry
directly to bypass this check.

Uninstall unlinks the plugin and removes only the intact owned block after validating the candidate.
The manager's blue token is best-effort metadata around the harness process; ordinary blocked,
working, idle, and unknown colors remain projections of Herdr's current lifecycle state.
Custom rows apply only to the expanded desktop Agent sidebar. Collapsed and mobile views retain
Herdr's built-in indicators.

From a source checkout, use `just manager` or `just manager claude`. For a one-off invocation
from any directory, use:

```bash
npx --yes herdr-manager
npx --yes herdr-manager claude
```

The command fails unless `HERDR_ENV=1` and starts the selected harness with no extra arguments
in the package's fixed manager workspace. Without an explicit harness it tries Grok, Codex, then
Claude, and fails clearly when none of those CLIs are available. The backward-compatible
`herdr-orchestrator manager --project . --harness claude` form explicitly selects the manager
workspace installed in a target project and validates the harness against that installation.

The published `herdr-manager` package exact-pins `herdr-orchestrator`. Its source directory has
its own `package-lock.json` for reproducible local checks. This keeps the thin command on the
runtime version it was tested against. Audit both npm package graphs from a source checkout:

```bash
npm audit --package-lock-only
npm audit --package-lock-only --prefix packages/herdr-manager
```

The manager policy treats terminal output as untrusted observations and scopes all visibility
to the current Herdr session. It is intentionally not durable. Use the queue commands below
for retries, deduplication, leases, unattended work, and receipts.

## Runtime commands

The wrapper supplies the installed workflow path, so callers only identify the project:

```bash
npx --yes herdr-orchestrator catalog --project .
npx --yes herdr-orchestrator status --project .
npx --yes herdr-orchestrator run --project . --once
npx --yes herdr-orchestrator run --project . --until-idle
npx --yes herdr-orchestrator retry --project . --job-id 42
npx --yes herdr-orchestrator resume --project . --job-id 43 --response-file approval.txt
npx --yes herdr-orchestrator gc --project . --succeeded-agents
npx --yes herdr-orchestrator gc --project . --failed-agents
npx --yes herdr-orchestrator dashboard --project .
```

The wrapper rejects a forwarded `--workflow` option. Runtime commands always use the installed
project workflow.

Arguments not consumed by the wrapper are passed to the Python CLI. For example:

```bash
npx --yes herdr-orchestrator enqueue --project . \
  --harness codex \
  --title "Review architecture" \
  --prompt-file prompts/review.md \
  --dedupe-key review-architecture-v1 \
  --receipt-prefix "TASK-OK review-architecture"
```

`run --once` reports the claimed wave in `batch` and the ending global counts in `queue`.
`--until-idle` repeats waves until the selected worker pool has no pending/running/blocked work or
the bounded drain timeout expires. A blocked job returns `idle=false`, `reason=blocked`. Read
`worker_pool_idle` and `queue_idle` separately when the worker pool is narrowed. Retry retains job
identity and adds attempt budget only to a failed job. After human review, `resume` sends the
response file to the exact recorded blocked agent and pane without incrementing the attempt or
repeating the task prompt. GC is dry-run unless `--apply` is present. Succeeded and failed agents
use separate explicit scopes; blocked agents are never regular GC candidates. GC never removes
worktrees; cleanup requires a
persisted creation receipt and an unchanged current pane ID. It closes only that pane, including
for tab-placed jobs, and never closes the containing tab.

## Upgrade and uninstall

```bash
npx --yes herdr-orchestrator upgrade --project .
npx --yes herdr-orchestrator uninstall --project .
```

`update` is an alias for `upgrade`. Passing `--harness` during upgrade reconciles the selected
catalog. Unchanged profiles that are no longer selected are removed. Modified files are
reported and retained.

Uninstall removes the manifest after processing it. User-modified managed files remain in
place and are listed in the JSON result; they are no longer managed after that point. A content
or mode change counts as a modification. Uninstall first finishes any older active transaction,
then journals its own removals. Repeating uninstall after journal retirement is safe: the
command does not claim or remove bytes when no ownership manifest exists. It does not remove
unjournaled directories, and it reports any bytes left under managed roots as `preserved`.
Empty `.agents/skills` directories do not count as an existing Skill router during a later
install.

## Automated npm releases

The `ci` GitHub Actions workflow uses a dedicated runner for routine validation and publishes
from `main` without an npm token:

1. Python compile and test run on
   `[self-hosted, Linux, X64, herdr-orchestrator]`;
2. after a successful `main` test, the release-plan runner compares both `package.json` and
   `packages/herdr-manager/package.json` with public registry versions;
3. an existing version is a successful no-op and allocates no GitHub-hosted runner;
4. a missing runtime or manager version enables the GitHub-hosted publish job;
5. that job publishes each missing package through npm Trusted Publishing, with the runtime
   before the manager package.

An explicit npm `E404` means a package has no published versions yet. Every other registry lookup
failure stops the job; the workflow never treats an ambiguous network or registry error as a new
version. npm does not support Trusted Publishing from self-hosted runners, so the OIDC publish job
must remain on `ubuntu-latest`. The repository is private, so npm provenance is not supported even
though tokenless Trusted Publishing is.

### Dedicated runner

The trusted release-planning runner is `herdr-orchestrator-185` on `remote-185`. It runs under
a dedicated operating-system account and advertises these labels:

```yaml
runs-on: [self-hosted, Linux, X64, herdr-orchestrator]
```

The release-plan job disables checkout credential persistence. Keep this runner scoped to this
private repository, and do not run untrusted pull request code on it.

### One-time trusted publisher setup

1. Create the GitHub Environment `npm`. Optional required reviewers can protect production
   releases.
2. From an authenticated maintainer terminal, register the exact repository, workflow file,
   and Environment for the existing runtime package:

   ```bash
   npm trust github herdr-orchestrator \
     --file ci.yml \
     --repo oldwinter/herdr-orchestrator \
     --env npm \
     --allow-publish \
     -y

   ```

   Bootstrap the brand-new `herdr-manager` name once, then configure the same trusted publisher:

   ```bash
   npm publish --access public ./packages/herdr-manager

   npm trust github herdr-manager \
     --file ci.yml \
     --repo oldwinter/herdr-orchestrator \
     --env npm \
     --allow-publish \
     -y
   ```

   Perform that one-time bootstrap only as an explicitly authorized release action. All later
   versions remain tokenless through the workflow.

3. Before releasing a new version, ensure GitHub Actions can start hosted runners. Account
   billing or spending-limit failures block only the missing-version publish job; routine
   test and no-op release-plan jobs continue on the dedicated runner.

The publish job grants only `contents: read` and `id-token: write`. Do not add
`NODE_AUTH_TOKEN` or an npm token secret, and do not move this job to a self-hosted runner.

### Releasing a new version

Create a normal pull request that updates the runtime version when runtime behavior changes:

```bash
npm version patch --no-git-tag-version
npm run release:plan
just check
```

When the thin manager package itself changes, also bump its independent version and keep its
`herdr-orchestrator` dependency compatible:

```bash
npm version patch --no-git-tag-version --prefix packages/herdr-manager
```

Use `minor` or `major` instead of `patch` when appropriate. After the version PR merges, the
`main` workflow plans and publishes each missing version independently. npm versions are
immutable, so changing package code without changing the corresponding `package.json` does not
create a release.

For a local package-content preview:

```bash
npm pack --dry-run --json
npm pack --dry-run --json ./packages/herdr-manager
```
