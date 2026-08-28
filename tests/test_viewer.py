from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import cast
from uuid import uuid4

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from technocore_orchestrator.domain.collaboration import (
    CollaborationEnvelope,
    CollaborationKind,
    canonical_collaboration_envelope,
    collaboration_payload_sha256,
)
from technocore_orchestrator.domain.models import (
    EventEnvelope,
    EventKind,
    HarnessKind,
    PlannerResult,
    PlanStep,
    Role,
    canonical_event_json,
)
from technocore_orchestrator.errors import ProtocolError
from technocore_orchestrator.identity import RoleIdentity
from technocore_orchestrator.storage import ParticipantEvidence, SQLiteStore
from technocore_orchestrator.technocore import RoomMessage, RoomRead, RoomView, TechnocoreClient
from technocore_orchestrator.viewer import TechnocoreTimeline


class _RoomClient:
    def __init__(self, messages: tuple[RoomMessage, ...], *, gap: bool = False) -> None:
        self.messages = messages
        self.gap = gap

    async def read_room(
        self,
        _room: str,
        *,
        cursor: int,
        wait_seconds: int,
        limit: int,
    ) -> RoomRead:
        del wait_seconds, limit
        selected = tuple(message for message in self.messages if message.seq > cursor)
        return RoomRead(
            view=RoomView(
                room="d-p-orchestrator-room",
                count=len(selected),
                first_seq=selected[0].seq if selected else None,
                last_seq=selected[-1].seq if selected else cursor,
                messages=selected,
            ),
            gap_detected=self.gap,
        )


def _identity() -> RoleIdentity:
    return RoleIdentity(Ed25519PrivateKey.generate())


def _plan() -> PlannerResult:
    return PlannerResult(
        summary="Implement the scoped change.",
        steps=(
            PlanStep(
                id="implement",
                description="Implement and verify the scoped change.",
                expected_paths=("product.txt",),
                criterion_ids=("criterion_1",),
            ),
        ),
        risks=(),
        verification_suggestions=("Run the configured check.",),
        challenge_dispositions=(),
        blocked_reason=None,
    )


def _participant(role: Role, did: str, tmp_path) -> ParticipantEvidence:
    if role not in {Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER}:
        return ParticipantEvidence(role=role, did=did)
    return ParticipantEvidence(
        role=role,
        did=did,
        harness=HarnessKind.CODEX,
        model="test-model",
        cli_name="codex",
        cli_version="1.2.3",
        executable_path=(tmp_path / "codex.exe").resolve(),
        executable_sha256="c" * 64,
        executable_size_bytes=1,
        structured_output=True,
        resumable=False,
    )


def test_viewer_projects_only_verified_safe_timeline_entries(tmp_path) -> None:
    identities = {
        Role.SUPERVISOR: _identity(),
        Role.PLANNER: _identity(),
        Role.IMPLEMENTER: _identity(),
        Role.REVIEWER: _identity(),
    }
    participants = (
        _participant(Role.SUPERVISOR, identities[Role.SUPERVISOR].public.did, tmp_path),
        _participant(Role.PLANNER, identities[Role.PLANNER].public.did, tmp_path),
        _participant(Role.IMPLEMENTER, identities[Role.IMPLEMENTER].public.did, tmp_path),
        _participant(Role.REVIEWER, identities[Role.REVIEWER].public.did, tmp_path),
        _participant(Role.VERIFIER, identities[Role.SUPERVISOR].public.did, tmp_path),
    )
    now = datetime.now(UTC)
    with SQLiteStore.open(tmp_path / "state.sqlite3") as store:
        store.create_run(
            run_id="run_12345678",
            task_id="task",
            config_digest="a" * 64,
            repository_path=tmp_path,
            base_commit="b" * 40,
            participants=participants,
        )
        workflow = EventEnvelope(
            v=1,
            event_id=uuid4(),
            run_id="run_12345678",
            task_id="task",
            kind=EventKind.RUN_STARTED,
            sender=Role.SUPERVISOR,
            attempt=1,
            created_at=now,
            summary="The verified run started.",
        )
        store.accept_event(workflow)
        store.mark_event_transport(str(workflow.event_id), status="published", technocore_seq=1)
        plan = _plan()
        collaboration = CollaborationEnvelope(
            v=1,
            event_id=uuid4(),
            run_id="run_12345678",
            task_id="task",
            kind=CollaborationKind.PLAN_PROPOSED,
            sender=Role.PLANNER,
            created_at=now,
            reply_to=None,
            text=plan.summary,
            payload_sha256=collaboration_payload_sha256(plan),
        )
        store.record_collaboration_message(collaboration, plan)
        store.mark_collaboration_transport(
            str(collaboration.event_id), status="published", technocore_seq=2
        )
        planner_event = EventEnvelope(
            v=1,
            event_id=uuid4(),
            run_id="run_12345678",
            task_id="task",
            kind=EventKind.PLAN_PROPOSED,
            sender=Role.PLANNER,
            attempt=1,
            created_at=now,
            summary=plan.summary,
        )
        store.accept_event(planner_event)
        store.mark_event_transport(
            str(planner_event.event_id), status="published", technocore_seq=3
        )
        client = _RoomClient(
            (
                RoomMessage(
                    seq=1,
                    ts=now.isoformat(),
                    **{
                        "from": identities[Role.SUPERVISOR].public.did,
                        "text": canonical_event_json(workflow),
                    },
                ),
                RoomMessage(
                    seq=2,
                    ts=now.isoformat(),
                    **{
                        "from": identities[Role.PLANNER].public.did,
                        "text": canonical_collaboration_envelope(collaboration),
                    },
                ),
                RoomMessage(
                    seq=3,
                    ts=now.isoformat(),
                    **{
                        "from": identities[Role.PLANNER].public.did,
                        "text": canonical_event_json(planner_event),
                    },
                ),
            )
        )
        viewer = TechnocoreTimeline(
            client=cast(TechnocoreClient, client),
            store=store,
            room="d-p-orchestrator-room",
            run_id="run_12345678",
        )
        window = asyncio.run(viewer.read(0, wait_seconds=0))
        assert tuple(entry.kind for entry in window.entries) == (
            "run_started",
            "plan_proposed",
        )
        assert tuple(entry.text for entry in window.entries) == (
            "The verified run started.",
            "Implement the scoped change.",
        )
        assert window.cursor_after == 3


def test_viewer_fails_closed_on_a_room_sequence_gap(tmp_path) -> None:
    identities = {role: _identity() for role in Role if role is not Role.VERIFIER}
    participants = tuple(
        _participant(
            role,
            (
                identities[Role.SUPERVISOR].public.did
                if role is Role.VERIFIER
                else identities[role].public.did
            ),
            tmp_path,
        )
        for role in Role
    )
    with SQLiteStore.open(tmp_path / "state.sqlite3") as store:
        store.create_run(
            run_id="run_12345678",
            task_id="task",
            config_digest="a" * 64,
            repository_path=tmp_path,
            base_commit="b" * 40,
            participants=participants,
        )
        viewer = TechnocoreTimeline(
            client=cast(TechnocoreClient, _RoomClient((), gap=True)),
            store=store,
            room="d-p-orchestrator-room",
            run_id="run_12345678",
        )
        with pytest.raises(ProtocolError, match="sequence gap"):
            asyncio.run(viewer.read(4, wait_seconds=0))
