from __future__ import annotations

import asyncio
from pathlib import Path
from typing import cast

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from technocore_orchestrator.collaboration_backend import TechnocoreCollaborationBackend
from technocore_orchestrator.domain.collaboration import CollaborationKind
from technocore_orchestrator.domain.models import PlannerResult, PlanStep, Role
from technocore_orchestrator.errors import ProtocolError
from technocore_orchestrator.identity import RoleIdentity, SignedRoomMessage
from technocore_orchestrator.storage import SQLiteStore, TransportStatus
from technocore_orchestrator.technocore import (
    RoomMessage,
    RoomRead,
    RoomView,
    TechnocoreClient,
    WriteOutcomeUnknownError,
)


class _RoomClient:
    def __init__(self) -> None:
        self.messages: list[RoomMessage] = []

    async def publish_signed(self, _room: str, signed: SignedRoomMessage) -> RoomMessage:
        message = RoomMessage.model_validate(
            {
                "seq": len(self.messages) + 1,
                "ts": "2026-08-27T00:00:00Z",
                "from": signed.did,
                "text": signed.text,
                "nonce": signed.nonce,
            }
        )
        self.messages.append(message)
        return message

    async def read_room(
        self,
        room: str,
        *,
        cursor: int = 0,
        wait_seconds: int = 0,
        limit: int = 50,
        poll_counter: int = 0,
    ) -> RoomRead:
        del wait_seconds, poll_counter
        selected = tuple(message for message in self.messages if message.seq > cursor)[:limit]
        view = RoomView(
            room=room,
            count=len(selected),
            first_seq=selected[0].seq if selected else None,
            last_seq=self.messages[-1].seq if self.messages else 0,
            messages=selected,
        )
        return RoomRead(view=view, gap_detected=False)


class _CommittedUnknownClient(_RoomClient):
    async def publish_signed(self, _room: str, signed: SignedRoomMessage) -> RoomMessage:
        await super().publish_signed(_room, signed)
        raise WriteOutcomeUnknownError("simulated response loss after commit")


def _identity() -> RoleIdentity:
    return RoleIdentity(Ed25519PrivateKey.generate())


def _plan() -> PlannerResult:
    return PlannerResult(
        summary="Validate normalized email addresses before checking duplicates.",
        steps=(
            PlanStep(
                id="validate_email",
                description="Normalize and validate the email.",
                expected_paths=("src/users.py", "tests/test_users.py"),
                criterion_ids=("criterion_1",),
            ),
        ),
        risks=("Normalization policy must be explicit.",),
        verification_suggestions=("Test whitespace-only and case variants.",),
        challenge_dispositions=(),
        blocked_reason=None,
    )


def _store(tmp_path: Path) -> SQLiteStore:
    store = SQLiteStore.open(tmp_path / "state.sqlite3")
    store.create_run(
        run_id="run_12345678",
        task_id="task",
        config_digest="a" * 64,
        repository_path=tmp_path,
        base_commit="b" * 40,
    )
    return store


def test_signed_plan_round_trips_through_room_and_durable_payload(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _RoomClient()
    identities = {
        Role.PLANNER: _identity(),
        Role.IMPLEMENTER: _identity(),
        Role.REVIEWER: _identity(),
    }
    backend = TechnocoreCollaborationBackend(
        client=cast(TechnocoreClient, client),
        store=store,
        room="d-p-orchestrator-room",
        run_id="run_12345678",
        task_id="task",
        identities=identities,
    )

    async def exercise() -> None:
        published = await backend.publish(
            role=Role.PLANNER,
            kind=CollaborationKind.PLAN_PROPOSED,
            payload=_plan(),
            reply_to=None,
        )
        window = await backend.read_new_messages(Role.IMPLEMENTER, 0)
        assert window.cursor_after == published.sequence
        assert len(window.messages) == 1
        assert window.messages[0].event_id == published.event_id
        assert window.messages[0].payload == _plan().model_dump(mode="json")

        acknowledgement = await backend.acknowledge(
            role=Role.IMPLEMENTER,
            event_id=published.event_id,
            sequence=published.sequence,
            payload_sha256=published.payload_sha256,
        )
        assert acknowledgement.sequence == 2

    try:
        asyncio.run(exercise())
        records = store.list_collaboration_messages("run_12345678")
        assert len(records) == 2
        assert all(record.transport_status is TransportStatus.PUBLISHED for record in records)
        assert records[0].technocore_seq == 1
        assert records[1].envelope.reply_to == records[0].envelope.event_id
    finally:
        store.close()


def test_acknowledgement_rejects_a_stale_or_wrong_digest(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _RoomClient()
    identities = {
        Role.PLANNER: _identity(),
        Role.IMPLEMENTER: _identity(),
    }
    backend = TechnocoreCollaborationBackend(
        client=cast(TechnocoreClient, client),
        store=store,
        room="d-p-orchestrator-room",
        run_id="run_12345678",
        task_id="task",
        identities=identities,
    )

    async def exercise() -> None:
        published = await backend.publish(
            role=Role.PLANNER,
            kind=CollaborationKind.PLAN_PROPOSED,
            payload=_plan(),
            reply_to=None,
        )
        with pytest.raises(ProtocolError, match="exact published handoff"):
            await backend.acknowledge(
                role=Role.IMPLEMENTER,
                event_id=published.event_id,
                sequence=published.sequence,
                payload_sha256="0" * 64,
            )

    try:
        asyncio.run(exercise())
    finally:
        store.close()


def test_unknown_collaboration_write_is_reconciled_without_a_duplicate(tmp_path: Path) -> None:
    store = _store(tmp_path)
    client = _CommittedUnknownClient()
    identities = {
        Role.PLANNER: _identity(),
        Role.IMPLEMENTER: _identity(),
        Role.REVIEWER: _identity(),
    }
    backend = TechnocoreCollaborationBackend(
        client=cast(TechnocoreClient, client),
        store=store,
        room="d-p-orchestrator-room",
        run_id="run_12345678",
        task_id="task",
        identities=identities,
    )

    try:
        published = asyncio.run(
            backend.publish(
                role=Role.PLANNER,
                kind=CollaborationKind.PLAN_PROPOSED,
                payload=_plan(),
                reply_to=None,
            )
        )
        assert published.sequence == 1
        assert len(client.messages) == 1
        stored = store.get_collaboration_message(str(published.event_id))
        assert stored.transport_status is TransportStatus.PUBLISHED
        assert stored.technocore_seq == 1
    finally:
        store.close()
