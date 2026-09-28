Pin exec-failure exit codes in check evidence.

Contract:
- A check whose argv[0] cannot be resolved records exit_code=127
  (executable_not_found) and fails the job as factory_check_failed.
- An executable-bit file that cannot be exec'd records exit_code=126
  and likewise fails closed.
