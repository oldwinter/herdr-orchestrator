from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

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


def run_public_operation_crash_matrix[T](
    transitions: Iterable[AttemptPhase],
    exercise: Callable[[AttemptPhase], T],
) -> dict[AttemptPhase, T]:
    return {transition: exercise(transition) for transition in transitions}
