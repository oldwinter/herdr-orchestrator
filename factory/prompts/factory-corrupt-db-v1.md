Map sqlite3.DatabaseError to a stable CLI failure.

Contract:
- Any verb touching state.db (status/report/intake/run) must exit 2 with
  `factory_state_db_unreadable: <detail>` on stderr instead of a traceback
  when the database file is corrupt or unreadable.
