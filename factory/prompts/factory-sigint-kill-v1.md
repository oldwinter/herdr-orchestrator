Make operator interrupt safe and clean.

Contract:
- factory run exits 130 with "interrupted" on stderr, no traceback.
- The in-flight check subprocess is killed rather than orphaned
  (Popen + kill on KeyboardInterrupt/timeout).
