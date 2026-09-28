# Work item: pin dedupe_key projection and document the lane

`Store.jobs()` gained `dedupe_key` so operator status and the factory
report can identify work items; pin the projection at the store layer,
not only through factory tests. The lane also belongs in AGENTS.md's
mode table so agents pick it deliberately instead of improvising on the
general queue.

## Acceptance criteria

- `tests/test_store.py` asserts `jobs()` returns `dedupe_key`.
- `AGENTS.md` lists the `just factory-*` recipe family as its own mode.
- `scripts/check_docs.py` still passes.
