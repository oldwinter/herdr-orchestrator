Normalize evidence filename timestamps.

Contract:
- Evidence files are named with a clean UTC stamp
  (%Y-%m-%dT%H-%M-%S.%fZ) instead of the offset-mangled form that
  produced "Z00-00".
