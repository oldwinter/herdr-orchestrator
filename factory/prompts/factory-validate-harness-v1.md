Make validate fail closed on harnesses the workflow cannot serve.

Contract:
- Items whose `harness` is valid but has no `[[workers]]` entry are reported
  per-item via `harness_supported` AND listed under `unsupported_harnesses`.
- `valid` must be false and the exit code 2 — intake would otherwise crash
  with `factory_harness_has_no_worker` on a backlog validate called valid.
