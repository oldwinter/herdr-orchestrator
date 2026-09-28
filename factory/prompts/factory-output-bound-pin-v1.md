Pin bounded evidence regardless of check verbosity.

Contract:
- A check emitting ~20k chars records a stdout_tail no longer than
  MAX_CHECK_OUTPUT_CHARS in its evidence record.
