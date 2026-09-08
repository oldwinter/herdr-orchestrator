from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from herdr_orchestrator.model import AttemptPhase, AttemptTransition
from herdr_orchestrator.runner import OperationInterrupted


@dataclass(slots=True)
class CrashAfterTransition:
    target: AttemptPhase
    observed: list[AttemptPhase] = field(default_factory=list)

    def __call__(self, transition: AttemptTransition) -> None:
        self.observed.append(transition.phase)
        if transition.phase is self.target:
            raise OperationInterrupted(transition.phase.value)


class CrashInjected(Exception):
    """An adapter proved interruption at this durable boundary."""

    def __init__(self, boundary: str) -> None:
        super().__init__(boundary)
        self.boundary = boundary


def run_public_operation_crash_matrix[C, T](
    transitions: Iterable[str],
    *,
    setup: Callable[[Path], C],
    run: Callable[[C, str | None], None],
    restart: Callable[[C], None],
    observe: Callable[[C], T],
    compare: Callable[[str, T, T], None] | None = None,
) -> dict[str, T]:
    """Compare fresh uninterrupted and interrupted/restarted public operations.

    Adapters own domain fault injection and must raise CrashInjected only after
    observing the selected interruption. Restart reopens the same durable state;
    observations retain business outcomes and side-effect counts. A domain may
    supply a stricter safety comparator for an intentionally non-convergent state
    such as queue attention when live turn ownership cannot be proved.
    """
    boundaries = tuple(transitions)
    if not boundaries or len(set(boundaries)) != len(boundaries):
        raise ValueError("crash matrix requires distinct named boundaries")
    results: dict[str, T] = {}
    with tempfile.TemporaryDirectory(prefix="herdr-crash-matrix-") as temporary:
        root = Path(temporary).resolve()
        baseline_root = root / "baseline"
        baseline_root.mkdir()
        baseline = setup(baseline_root)
        run(baseline, None)
        expected = observe(baseline)
        for index, boundary in enumerate(boundaries):
            case_root = root / f"case-{index}"
            case_root.mkdir()
            case = setup(case_root)
            try:
                run(case, boundary)
            except CrashInjected as exc:
                if exc.boundary != boundary:
                    raise AssertionError(
                        f"{boundary}: interrupted at unexpected boundary {exc.boundary}"
                    ) from exc
            else:
                raise AssertionError(f"{boundary}: selected interruption was not observed")
            restart(case)
            actual = observe(case)
            if compare is None:
                assert actual == expected, f"{boundary}: recovered observation diverged"
            else:
                compare(boundary, expected, actual)
            results[boundary] = actual
    return results
