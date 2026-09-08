# Crash recovery verification

Issue [#40](https://github.com/oldwinter/herdr-orchestrator/issues/40) spans queue ownership,
completion evidence, delivery recovery, installation recovery, and quality publication.
The implementations now exist on `main`; the remaining work consolidates recovery verification
and closes the quality publication ownership gap.

## Remaining task graph

- [#63](https://github.com/oldwinter/herdr-orchestrator/issues/63) adds the shared test-only
  crash-matrix driver and compares restarted public operations with uninterrupted runs.
- The quality publication repair preserves durable owner evidence throughout publication.
- Final integration depends on both changes, focused tests, the full gate, and independent
  Standards and Spec reviews.

## Acceptance boundaries

Automated recovery tests use temporary projects and owned adapters. They prove persisted
state, side-effect counts, and conservative attention outcomes. They do not prove live provider
readiness. Runtime state, prompts, terminal transcripts, and raw provider responses stay out
of this document and Git.

Run `just check` for the full repository gate. Run `just readiness-matrix` separately from a
Herdr-managed pane on an operator-controlled host. A result from outside Herdr or without
current evidence is `NOT VERIFIED`. Unit tests and GitHub-hosted CI cannot promote it to
verified compatibility.
