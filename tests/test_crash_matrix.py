from pathlib import Path

import pytest
from crash_matrix import CrashInjected, run_public_operation_crash_matrix


def test_driver_runs_isolated_baseline_and_restarts_same_durable_state() -> None:
    roots = []

    def setup(root: Path) -> Path:
        roots.append(root)
        (root / "count").write_text("0")
        return root

    def run(root: Path, boundary: str | None) -> None:
        (root / "count").write_text("1")
        if boundary is not None:
            raise CrashInjected(boundary)

    def restart(root: Path) -> None:
        assert (root / "count").read_text() == "1"
        run(root, None)

    results = run_public_operation_crash_matrix(
        ("published", "confirmed"),
        setup=setup,
        run=run,
        restart=restart,
        observe=lambda root: (root / "count").read_text(),
    )
    assert results == {"published": "1", "confirmed": "1"}
    assert len(set(roots)) == 3
    assert all(not root.exists() for root in roots)


@pytest.mark.parametrize("failure", ("missed", "wrong_boundary", "restart", "divergent"))
def test_driver_rejects_incomplete_or_divergent_recovery(failure: str) -> None:
    def run(root: Path, boundary: str | None) -> None:
        (root / "state").write_text("complete")
        if boundary is not None and failure != "missed":
            raise CrashInjected("other" if failure == "wrong_boundary" else boundary)

    def restart(root: Path) -> None:
        if failure == "restart":
            raise RuntimeError("restart failed")
        if failure == "divergent":
            (root / "state").write_text("duplicate side effect")

    message = {
        "missed": "interruption was not observed",
        "wrong_boundary": "unexpected boundary",
        "restart": "restart failed",
        "divergent": "observation diverged",
    }[failure]
    with pytest.raises((AssertionError, RuntimeError), match=message):
        run_public_operation_crash_matrix(
            ("published",),
            setup=lambda root: root,
            run=run,
            restart=restart,
            observe=lambda root: (root / "state").read_text(),
        )
