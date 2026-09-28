Align report backlog coverage with status blocker visibility.

Contract:
- report.md waiting entries render only UNMET requirements, each as
  `key=state` (e.g. `beta (waiting on: alpha=failed)`), matching the
  status waiting map's semantics.
