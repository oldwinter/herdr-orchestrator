Make factory status explain why unqueued items are unqueued.

Contract:
- `factory status` backlog section gains `waiting`: a map of unqueued
  dedupe_key -> list of unsatisfied `requires` blockers.
- Items whose requires are all satisfied but not yet intaken are unqueued
  but must NOT appear in `waiting` (only truly blocked items do).
