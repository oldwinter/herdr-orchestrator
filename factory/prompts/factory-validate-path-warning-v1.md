Warn at dry-run time about checks whose executable cannot resolve.

Contract:
- `factory validate` warnings include any bare-name argv[0] absent from
  PATH, noting the check would fail with exit_code=127.
- Warnings stay non-fatal.
