Make factory artifacts atomic for concurrent readers.

Contract:
- Receipts, per-attempt evidence, and report.md are written via
  sibling-temp-file + os.replace — never a partial JSON on disk.
- Orphaned .*.tmp files inside an evidence directory are swept on write.
- A failed report write surfaces stable factory_report_write_failed via
  FactoryError -> exit 2, not a traceback.
