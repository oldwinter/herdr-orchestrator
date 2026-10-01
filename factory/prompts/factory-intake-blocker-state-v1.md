Align intake waiting output with status blocker visibility.

Contract:
- `factory intake` waiting entries use `waiting_on` with
  {dedupe_key, state} objects matching the status waiting semantics.
- The same-pass state map is refreshed so items enqueued earlier in the
  current intake report `pending`, not `unqueued`, as blockers.
