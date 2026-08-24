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
3. installs the portable Skill at `.agents/skills/herdr-orchestrator/`;
4. writes an ownership manifest with a SHA-256 hash for every managed file;
5. carries the Python package and invokes it with its packaged `src/` on `PYTHONPATH`.

Python is not copied or downloaded. The target machine must provide Python 3.12+ and Herdr.
No global install or elevated permission is required.

To bypass automatic harness detection:

```bash
npx --yes herdr-orchestrator install --project . \
  --harness droid \
  --harness codex
```

Supported names are `droid`, `grok`, `codex`, `pi`, `claude`, and `hermes`.

## Managed project surface

| Path | Ownership |
| --- | --- |
| `.herdr-orchestrator/manifest.json` | Installer ownership and content hashes |
| `.herdr-orchestrator/workflows/` | Portable project-relative workflow and prompts |
| `.herdr-orchestrator/profiles/` | Profiles for selected harnesses |
| `.agents/skills/herdr-orchestrator/` | Portable agent Skill |
| `.orchestrator/.gitignore` | Keeps durable runtime state out of Git |

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
- `runtime`: Python, Herdr, Git, selected harness, and profile checks;
- top-level `ok`: true only when both layers are healthy.

Exit code `1` means the installation or runtime needs attention. In particular, real dispatch
must run inside a Herdr pane with the expected `HERDR_*` environment.

## Runtime commands

The wrapper supplies the installed workflow path, so callers only identify the project:

```bash
npx --yes herdr-orchestrator catalog --project .
npx --yes herdr-orchestrator status --project .
npx --yes herdr-orchestrator run --project . --once
npx --yes herdr-orchestrator dashboard --project .
```

Arguments not consumed by the wrapper are passed to the Python CLI. For example:

```bash
npx --yes herdr-orchestrator enqueue --project . \
  --harness codex \
  --title "Review architecture" \
  --prompt-file prompts/review.md \
  --dedupe-key review-architecture-v1
```

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

## Maintainer release check

Before publishing the npm package:

```bash
npm pack --dry-run --json
just check
```

Publishing is intentionally separate from the repository test and install flows.
