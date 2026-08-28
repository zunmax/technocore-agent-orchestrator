"""Event publication port and signed Technocore implementation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol

from technocore_orchestrator.domain.models import EventEnvelope, Role, canonical_event_json
from technocore_orchestrator.errors import IdentityError, ProtocolError
from technocore_orchestrator.identity import RoleIdentity, clean_text, validate_room_name
from technocore_orchestrator.storage import SQLiteStore
from technocore_orchestrator.technocore import TechnocoreClient, WriteOutcomeUnknownError

MAX_EVENT_TRANSPORT_BYTES = 3_072


@dataclass(frozen=True, slots=True)
class PublicationReceipt:
    sequence: int
    timestamp: str


class EventPublisher(Protocol):
    async def publish(self, event: EventEnvelope) -> PublicationReceipt: ...

    async def reconcile(self, event: EventEnvelope) -> PublicationReceipt | None: ...


class LocalEventPublisher:
    """Restart-safe local transport used only by credential-free fake workflows."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    async def publish(self, event: EventEnvelope) -> PublicationReceipt:
        records = self._store.list_event_records(event.run_id)
        matching = [record for record in records if record.event.event_id == event.event_id]
        if len(matching) != 1:
            raise ProtocolError("local publisher requires one accepted event")
        published = [
            record.technocore_seq
            for record in records
            if record.technocore_seq is not None and record.event.event_id != event.event_id
        ]
        sequence = max(published, default=0) + 1
        return PublicationReceipt(sequence=sequence, timestamp=event.created_at.isoformat())

    async def reconcile(self, event: EventEnvelope) -> PublicationReceipt | None:
        matches = [
            record
            for record in self._store.list_event_records(event.run_id)
            if record.event.event_id == event.event_id
        ]
        if len(matches) != 1:
            raise ProtocolError("local publisher requires one accepted event")
        record = matches[0]
        if record.technocore_seq is None:
            return None
        return PublicationReceipt(
            sequence=record.technocore_seq,
            timestamp=record.event.created_at.isoformat(),
        )


class TechnocoreEventPublisher:
    """Sign canonical accepted events using the identity assigned to their role."""

    def __init__(
        self,
        *,
        client: TechnocoreClient,
        store: SQLiteStore,
        room: str,
        identities: Mapping[Role, RoleIdentity],
    ) -> None:
        self._client = client
        self._store = store
        self._room = validate_room_name(room)
        self._identities = dict(identities)

    async def publish(self, event: EventEnvelope) -> PublicationReceipt:
        identity, text = self._event_identity_and_text(event)
        nonce = self._store.allocate_nonce(identity.public.did, self._room)
        signed = identity.sign_room_message(self._room, nonce, text)
        posted = await self._client.publish_signed(self._room, signed)
        return PublicationReceipt(sequence=posted.seq, timestamp=posted.ts)

    async def reconcile(self, event: EventEnvelope) -> PublicationReceipt | None:
        identity, text = self._event_identity_and_text(event)
        cursor = self._store.published_sequence_before(str(event.event_id))
        read = await self._client.read_room(self._room, cursor=cursor, limit=200)
        if read.gap_detected:
            raise WriteOutcomeUnknownError(
                "Technocore history gap prevents signed-write reconciliation"
            )
        matches = tuple(
            message
            for message in read.view.messages
            if message.sender == identity.public.did and message.text == text
        )
        if len(matches) > 1:
            raise ProtocolError("Technocore contains duplicate copies of one logical event")
        if not matches:
            return None
        return PublicationReceipt(sequence=matches[0].seq, timestamp=matches[0].ts)

    def _event_identity_and_text(self, event: EventEnvelope) -> tuple[RoleIdentity, str]:
        identity = self._identities.get(event.sender)
        if identity is None:
            raise IdentityError(
                "no Technocore identity is configured for the event sender",
                context={"sender": event.sender},
            )
        text = clean_text(canonical_event_json(event))
        encoded_bytes = len(text.encode("utf-8"))
        if encoded_bytes > MAX_EVENT_TRANSPORT_BYTES:
            raise ProtocolError(
                "canonical event exceeds the 3072-byte transport envelope",
                context={"bytes": encoded_bytes},
            )
        return identity, text
