SIGTERM to an owned factory-run PID must abort the in-flight check's whole
process group promptly (same dispatcher.abort path as SIGINT): the runner
exits 143, the check's descendant never writes its marker, the claimed
attempt stays running until lease expiry for canonical reclaim, and both
signal handlers are restored afterwards.
