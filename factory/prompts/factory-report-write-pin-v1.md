Pin the report-write failure contract at the CLI level.

Contract:
- When .orchestrator/factory is unwritable, `factory report` exits 2 with
  factory_report_write_failed on stderr and no traceback.
