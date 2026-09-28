Pin the single-wave exit contract.

Contract:
- `factory run --once` exits 1 when the claimed batch fails, and the
  jobs[] entry exposes state=failed with the stable error code.
