# Work item: macOS portability for verify-herdr-orchestrator helpers

The skill helpers assumed Linux: `doctor.sh`/`cleanup.sh` read
`/proc/<pid>/cmdline` and `capture-dashboard.sh` only probed Linux Chrome
paths, so the isolated verification lane could not run on macOS even
though the repo targets darwin. `common.sh` gains `pid_cmdline` (/proc
when present, `ps` fallback), and the capture helper probes
`/Applications/*.app/Contents/MacOS/` binaries.

## Acceptance criteria

- `doctor.sh` passes on macOS against a live isolated dashboard.
- `capture-dashboard.sh` finds `/Applications/Google Chrome.app`.
- `cleanup.sh` matches and kills only our dashboard pid on macOS.
- All three helpers parse cleanly (`bash -n`).
