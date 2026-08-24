# Workflow contract

## Stage order

1. **Route**: `wayfinder=auto` asks the controller whether the effort exceeds one context
   and still contains decision fog. Clear or small work goes straight to specification.
2. **Wayfinder**: chart a decision map, then resolve one frontier question per fresh
   controller turn. The map plans. It never implements the destination. Completion requires
   every decision resolved and `not_yet_specified` empty.
3. **Specification**: synthesize the accepted goal and resolved decisions without reopening
   the interview. Name observable testing seams.
4. **Tickets**: create dependency-ordered tracer-bullet vertical slices. A ticket must fit
   one fresh context and carry complete acceptance criteria.
5. **Implementation**: claim up to three frontier tickets. Create one git worktree and branch
   per ticket from the current integration head. Each worker uses TDD at the accepted seams,
   runs full validation once, commits, and emits a criterion-by-criterion receipt.
6. **Integration**: validate a clean committed worktree, merge each ticket branch into the
   isolated integration branch, reconcile acceptance criteria, and close the ticket. Closed
   blockers expose the next frontier.
7. **Review**: after all tickets, run fresh Standards and Spec reviewers in parallel against
   the original base commit. Reviewers perform their axes directly and cannot delegate.
8. **Adjudication**: the controller checks every cited finding. Accepted must-fix findings
   enter a repair turn. Repeat review after repair, for at most two repair rounds.

## Invariants

- The deterministic coordinator owns stage transitions, DAG frontier, concurrency, artifact
  validation, git integration, retry bounds, and terminal success.
- Controller and workers emit strict JSON artifacts. Prose replies never advance state.
- A settled turn with a missing required artifact is retried once on the same ready agent;
  the second miss fails the stage.
- Controller turns see only the enabled compact harness catalog. A worker receives only its
  selected full profile.
- Parallel tickets never share a working directory, index, branch, or HEAD. They never use
  `git stash`.
- Review happens after committed implementation and outside authoring contexts.
- Findings are hypotheses until the controller verifies their citations.
- The decision ledger records routes and decisions without storing worker answers or secrets.
- Worktrees are checkout isolation, not a security sandbox.

## Tracker backends

`local-markdown` is the default and writes a spec plus one file per ticket beneath the
configured tracker root. `github` creates and closes issues in the configured repository.
Choosing the GitHub backend is explicit authorization for those issue mutations only. It
does not authorize pull requests, push, merge, release, or deployment.
