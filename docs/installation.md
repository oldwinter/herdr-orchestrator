# Distribution and installation

Herdr Orchestrator uses two distribution layers with one project-local runtime contract.

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
7. carries the Python package and invokes it with its packaged `src/` on `PYTHONPATH`.

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
| `.herdr-orchestrator/manifest.json` | Installer ownership and content hashes |
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

## Diagnostics

```bash
npx --yes herdr-orchestrator doctor --project .
```

`doctor` returns one JSON document with:

- `installation`: missing or modified managed files;
- `runtime`: Python, Herdr, Git, profile checks, and a bounded real readiness turn for every
  selected harness;
- top-level `ok`: true only when both layers are healthy.

Exit code `1` means the installation or runtime needs attention. In particular, real dispatch
must run inside a Herdr pane with the expected `HERDR_*` environment. A harness readiness
status is one of `ready`, `auth_required`, `model_invalid`, `timeout`, `unavailable`, or
`error`; an executable in `PATH` alone is not ready. Doctor closes only probe agents it created.

## Manual manager

For interactive oversight of the current Herdr session, install the frequent-use command once
from a source checkout:

```bash
just install-manager
herdr-manager         # defaults to Grok
herdr-manager claude  # selects Claude
```

The Just recipe invokes npm from a non-interactive shell, bypassing interactive wrappers that
rewrite `npm install --global .` as an invalid `mise use -g npm:.` package request. Once version
`0.1.3` or newer is published, `npm install --global herdr-orchestrator` is also supported.

From a source checkout, use `just manager` or `just manager claude`. For a one-off invocation,
use `npx --yes herdr-orchestrator manager claude`.

The command fails unless `HERDR_ENV=1` and starts the selected harness with no extra arguments
in the package's fixed manager workspace. Grok is the default. The backward-compatible
`herdr-orchestrator manager --project . --harness claude` form explicitly selects the manager
workspace installed in a target project and validates the harness against that installation.

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
place and are listed in the JSON result; they are no longer managed after that point.

## Automated npm releases

The `ci` GitHub Actions workflow uses a dedicated runner for routine validation and publishes
from `main` without an npm token:

1. Python compile and test run on
   `[self-hosted, Linux, X64, herdr-orchestrator]`;
2. after a successful `main` test, the same runner compares `package.json` with public
   registry versions;
3. an existing version is a successful no-op and allocates no GitHub-hosted runner;
4. a missing version enables the GitHub-hosted publish job;
5. that job publishes through npm Trusted Publishing.

Registry lookup failures stop the job. The workflow never guesses that a failed lookup means
a new package version. npm does not support Trusted Publishing from self-hosted runners, so
the OIDC publish job must remain on `ubuntu-latest`. The repository is private, so npm
provenance is not supported even though tokenless Trusted Publishing is.

### Dedicated runner

The repository-specific runner is `herdr-orchestrator-185` on `remote-185`. It runs under a
dedicated operating-system account and advertises these labels:

```yaml
runs-on: [self-hosted, Linux, X64, herdr-orchestrator]
```

Both self-hosted jobs disable checkout credential persistence. Keep this runner scoped to
this private repository, and do not run untrusted pull request code on it.

### One-time trusted publisher setup

1. Create the GitHub Environment `npm`. Optional required reviewers can protect production
   releases.
2. From an authenticated maintainer terminal, register the exact repository, workflow file,
   and Environment:

   ```bash
   npm trust github herdr-orchestrator \
     --file ci.yml \
     --repo oldwinter/herdr-orchestrator \
     --env npm \
     --allow-publish \
     -y
   ```

3. Before releasing a new version, ensure GitHub Actions can start hosted runners. Account
   billing or spending-limit failures block only the missing-version publish job; routine
   test and no-op release-plan jobs continue on the dedicated runner.

The publish job grants only `contents: read` and `id-token: write`. Do not add
`NODE_AUTH_TOKEN` or an npm token secret, and do not move this job to a self-hosted runner.

### Releasing a new version

Create a normal pull request that updates both npm manifests:

```bash
npm version patch --no-git-tag-version
npm run release:plan
just check
```

Use `minor` or `major` instead of `patch` when appropriate. After the version PR merges,
the `main` workflow publishes that exact version. npm versions are immutable, so changing
runtime code without changing `package.json` does not create a release.

For a local package-content preview:

```bash
npm pack --dry-run --json
```
