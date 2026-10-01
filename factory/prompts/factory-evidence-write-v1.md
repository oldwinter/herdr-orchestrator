Harden evidence persistence in the factory local dispatcher.

Contract:
- `_write_evidence` never raises OSError into the coordinator; it returns the
  stable error code `factory_evidence_write_failed`.
- Success path: a failed evidence write converts to that stable failure code —
  a job cannot record success without evidence on disk.
- Failure path: a failed evidence write keeps the original error_code and only
  annotates error_summary with `evidence_write_failed`, so the durable record
  still names the true failure cause instead of dispatcher_unhandled_error.
