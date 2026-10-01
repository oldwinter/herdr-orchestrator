Pin concurrent-drain safety.

Contract:
- Two simultaneous `factory run` processes on a queue with one pending
  item claim it exactly once total; the job succeeds with a single
  attempt; both drains exit 0.
