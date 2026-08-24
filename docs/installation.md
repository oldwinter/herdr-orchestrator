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
