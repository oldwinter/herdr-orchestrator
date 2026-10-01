Fix the aggregate check deadline overrun in the factory local dispatcher.

Contract:
- `LocalDispatcher._run_check` must preserve the global remaining budget:
  `remaining = min(check.timeout_seconds, deadline - now)` with no artificial floor.
- Once the dispatch deadline has expired, subsequent checks must not spawn a
  subprocess; they return a `deadline_exceeded` timeout result instead.
- In-flight checks must be killed at the deadline, never beyond it.
- A job whose checks cannot fit inside `agent_timeout_seconds` must fail with
  `factory_check_timeout`; it must never record `task_verified` success.
- Regression coverage: 16x0.75s checks under a 10s budget fail bounded near the
  deadline; a within-budget single-check control still succeeds.
