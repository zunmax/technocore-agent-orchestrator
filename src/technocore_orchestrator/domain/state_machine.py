"""Pure workflow transition rules."""

from __future__ import annotations

from dataclasses import dataclass

from technocore_orchestrator.domain.models import EventEnvelope, EventKind, Role, RunState
from technocore_orchestrator.errors import StateError


@dataclass(frozen=True, slots=True)
class Transition:
    previous: RunState
    current: RunState


_EXPECTED_SENDERS: dict[EventKind, Role] = {
    EventKind.RUN_STARTED: Role.SUPERVISOR,
    EventKind.PLAN_PROPOSED: Role.PLANNER,
    EventKind.PLAN_CHALLENGED: Role.IMPLEMENTER,
    EventKind.PLAN_FINALIZED: Role.PLANNER,
    EventKind.IMPLEMENTATION_STARTED: Role.SUPERVISOR,
    EventKind.IMPLEMENTATION_READY: Role.IMPLEMENTER,
    EventKind.REVISION_REQUIRED: Role.REVIEWER,
    EventKind.REVIEW_APPROVED: Role.REVIEWER,
    EventKind.VERIFICATION_PASSED: Role.VERIFIER,
    EventKind.VERIFICATION_FAILED: Role.VERIFIER,
    EventKind.RUN_FAILED: Role.SUPERVISOR,
    EventKind.RUN_CANCELED: Role.SUPERVISOR,
    EventKind.TRANSPORT_WARNING: Role.SUPERVISOR,
    EventKind.SECURITY_WARNING: Role.SUPERVISOR,
}

_TRANSITIONS: dict[tuple[RunState, EventKind], RunState] = {
    (RunState.CREATED, EventKind.RUN_STARTED): RunState.PLANNING,
    (RunState.PLANNING, EventKind.PLAN_PROPOSED): RunState.CHALLENGING,
    (RunState.CHALLENGING, EventKind.PLAN_CHALLENGED): RunState.FINALIZING,
    (RunState.FINALIZING, EventKind.PLAN_FINALIZED): RunState.READY,
    (RunState.READY, EventKind.IMPLEMENTATION_STARTED): RunState.IMPLEMENTING,
    (RunState.IMPLEMENTING, EventKind.IMPLEMENTATION_READY): RunState.REVIEWING,
    (RunState.REVIEWING, EventKind.REVISION_REQUIRED): RunState.IMPLEMENTING,
    (RunState.REVIEWING, EventKind.REVIEW_APPROVED): RunState.VERIFYING,
    (RunState.VERIFYING, EventKind.VERIFICATION_PASSED): RunState.COMPLETED,
    (RunState.VERIFYING, EventKind.VERIFICATION_FAILED): RunState.FAILED,
}


def apply_transition(state: RunState, event: EventEnvelope) -> Transition:
    """Validate role authority and return the single legal next state."""

    if state.is_terminal:
        raise StateError(
            "terminal run state cannot accept another event",
            context={"state": state, "kind": event.kind},
        )
    expected_sender = _EXPECTED_SENDERS[event.kind]
    if event.sender is not expected_sender:
        raise StateError(
            "event sender is not authorized for event kind",
            context={"kind": event.kind, "sender": event.sender, "expected": expected_sender},
        )
    if event.kind is EventKind.RUN_FAILED:
        target = RunState.FAILED
    elif event.kind is EventKind.RUN_CANCELED:
        target = RunState.CANCELED
    elif event.kind in {EventKind.TRANSPORT_WARNING, EventKind.SECURITY_WARNING}:
        target = state
    else:
        target = _TRANSITIONS.get((state, event.kind))
        if target is None:
            raise StateError(
                "event is not legal from current state",
                context={"state": state, "kind": event.kind},
            )
    return Transition(previous=state, current=target)
