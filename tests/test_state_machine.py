from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from technocore_orchestrator.domain.models import EventEnvelope, EventKind, Role, RunState
from technocore_orchestrator.domain.state_machine import apply_transition
from technocore_orchestrator.errors import StateError


def _event(kind: EventKind, sender: Role) -> EventEnvelope:
    return EventEnvelope(
        v=1,
        event_id=uuid4(),
        run_id="run_12345678",
        task_id="task",
        kind=kind,
        sender=sender,
        attempt=1,
        created_at=datetime.now(UTC),
        summary="Durable workflow checkpoint.",
    )


def test_plan_must_be_challenged_and_finalized_before_implementation() -> None:
    proposed = apply_transition(RunState.PLANNING, _event(EventKind.PLAN_PROPOSED, Role.PLANNER))
    assert proposed.current is RunState.CHALLENGING
    challenged = apply_transition(
        proposed.current, _event(EventKind.PLAN_CHALLENGED, Role.IMPLEMENTER)
    )
    assert challenged.current is RunState.FINALIZING
    finalized = apply_transition(challenged.current, _event(EventKind.PLAN_FINALIZED, Role.PLANNER))
    assert finalized.current is RunState.READY


def test_implementation_cannot_start_from_a_proposed_plan() -> None:
    with pytest.raises(StateError, match="not legal"):
        apply_transition(
            RunState.CHALLENGING,
            _event(EventKind.IMPLEMENTATION_STARTED, Role.SUPERVISOR),
        )
