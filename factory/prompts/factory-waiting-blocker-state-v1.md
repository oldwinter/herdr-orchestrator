Make dependency stalls diagnosable from factory status.

Contract:
- `factory status` backlog.waiting maps each waiting item to a list of
  {dedupe_key, state} objects instead of bare keys, so a terminally
  failed requirement is visibly different from a merely pending one.
