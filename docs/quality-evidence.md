# Quality evidence bundles

Quality commands publish evidence under `.orchestrator/quality/`. The directory is ignored by Git and may contain raw scanner, coverage, test, package, and profiling output. Pull request comments contain only the bounded Markdown summary.

## Run identity

Every invocation has a unique run ID derived from:

- the full Git commit;
- the caller-supplied invocation ID, or a local UUID;
- a SHA-256 source digest.

The source digest binds the base commit, tracked binary diff, and untracked path, mode, and content. `source_clean` distinguishes exact commit evidence from a stable development working tree. The runner samples the source again before publication. A change during the run makes the bundle `NOT VERIFIED`.

CI uses `${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}` as its invocation ID and requires a clean source. Local focused commands may run against a dirty tree because their evidence is bound to that exact source digest rather than attributed only to `HEAD`.

## Publication

Each producer writes into its own temporary directory. The runner reads every artifact once, parses and hashes the same bounded byte snapshot, then copies those bytes into a runner-owned producer directory. Producers never update the manifest.

After all producers settle, the runner writes one completed manifest and atomically renames the pending run to:

```text
.orchestrator/quality/runs/<run-id>/
  manifest.json
  producers/<producer>/producer.json
  producers/<producer>/result.json
  producers/<producer>/<raw-artifacts>
```

The manifest records command argv, start and end time, exit outcome, tool version, bounded input identity, artifact path, SHA-256 digest, and verification status. Failed commands remain visible but cannot mark artifacts verified.

## Validation

The summary and enforcement commands consume the current run result. That result binds the expected commit, invocation ID, run ID, source digest, and manifest path. Validation rejects:

- missing or duplicate manifest keys;
- reused or mismatched run identity;
- changed source state or a dirty source when commit-grade evidence is required;
- missing, extra, escaping, symlinked, corrupt, or schema-invalid artifacts;
- incomplete producer contracts;
- command, timing, tool-version, result-fact, or producer-outcome mismatches.

Missing, failed, stale, and mismatched evidence renders `NOT VERIFIED`. A scanner may report zero findings only when the current command succeeded and every required artifact parsed and matched its digest.

## Commands

The focused commands remain stable:

```bash
just lint
just test
just test-coverage
just test-stability
just security
just build-metrics
just profile-tests
```

`just check` runs one bundle containing lint, coverage, stability, security, build, and profiling producers. It generates a bounded summary and separately enforces the manifest outcomes. `just quality-summary` renders the latest result matching the current source state. `python3 scripts/quality_bundle.py latest-result` prints that result path.

Profiling data is located through the result's manifest at `producers/profiling/tests.pstats`; there is no shared `tests.pstats` filename. CI uploads one run bundle plus its bounded summary, then enforces the manifest independently of summary generation.
