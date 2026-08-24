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
3. installs the portable Skill at `.agents/skills/herdr-orchestrator/` only when the target
   has no existing Skill router, or when `--install-skill` is explicit;
4. writes an ownership manifest with a SHA-256 hash for every managed file;
5. adds installer-managed roots to this repository's Git-local `info/exclude` without editing
   a tracked `.gitignore` or hiding an unmanaged Skill;
6. carries the Python package and invokes it with its packaged `src/` on `PYTHONPATH`.

Python is not copied or downloaded. The target machine must provide Python 3.12+ and Herdr.
No global install or elevated permission is required.

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

## Runtime commands

The wrapper supplies the installed workflow path, so callers only identify the project:

```bash
npx --yes herdr-orchestrator catalog --project .
npx --yes herdr-orchestrator status --project .
npx --yes herdr-orchestrator run --project . --once
npx --yes herdr-orchestrator run --project . --until-idle
npx --yes herdr-orchestrator retry --project . --job-id 42
npx --yes herdr-orchestrator gc --project . --succeeded-agents
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
`--until-idle` repeats waves until the selected worker pool has no pending/running work or the
bounded drain timeout expires. Read `worker_pool_idle` and `queue_idle` separately when the
worker pool is narrowed. Retry retains job identity and adds attempt budget only to a failed
job. GC is dry-run unless `--apply` is present and never removes worktrees; cleanup requires a
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

The `ci` GitHub Actions workflow publishes from `main` without an npm token:

1. the Python compile and test job must pass;
2. the publish job runs only for a push to `main`;
3. `npm run release:plan` compares `package.json` with public registry versions;
4. an existing version is a successful no-op;
5. a missing version is published with npm Trusted Publishing and provenance.

Registry lookup failures stop the job. The workflow never guesses that a failed lookup means
a new package version.

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

3. Ensure GitHub Actions can start hosted runners. Account billing or spending-limit failures
   prevent both CI and publishing before any step executes.

The publish job grants only `contents: read` and `id-token: write`. Do not add
`NODE_AUTH_TOKEN` or an npm token secret.

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
