"""Secret-free validation and projection of the shared Technocore timeline."""

from __future__ import annotations

import json
import unicodedata
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import ValidationError

from technocore_orchestrator.domain.collaboration import (
    CollaborationEnvelope,
    canonical_collaboration_envelope,
)
from technocore_orchestrator.domain.models import EventEnvelope, Role, canonical_event_json
from technocore_orchestrator.errors import ProtocolError
from technocore_orchestrator.storage import SQLiteStore, TransportStatus
from technocore_orchestrator.technocore import MAX_MESSAGES_PER_READ, RoomMessage, TechnocoreClient


@dataclass(frozen=True, slots=True)
class TimelineEntry:
    sequence: int
    created_at: datetime
    sender: Role
    kind: str
    reply_to: UUID | None
    text: str


@dataclass(frozen=True, slots=True)
class TimelineWindow:
    cursor_after: int
    entries: tuple[TimelineEntry, ...]
    at_limit: bool


class TechnocoreTimeline:
    """Validate every room record against its durable SQLite fact."""

    def __init__(
        self,
        *,
        client: TechnocoreClient,
        store: SQLiteStore,
        room: str,
        run_id: str,
    ) -> None:
        run = store.get_run(run_id)
        self._client = client
        self._store = store
        self._room = room
        self._run_id = run_id
        self._task_id = run.task_id
        self._did_by_role = {
            participant.role: participant.did for participant in store.list_participants(run_id)
        }
        required = {Role.SUPERVISOR, Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER, Role.VERIFIER}
        if set(self._did_by_role) != required:
            raise ProtocolError("live viewer requires the complete real-run participant set")

    async def read(self, after_sequence: int, *, wait_seconds: int) -> TimelineWindow:
        read = await self._client.read_room(
            self._room,
            cursor=after_sequence,
            wait_seconds=wait_seconds,
            limit=MAX_MESSAGES_PER_READ,
        )
        if read.gap_detected:
            raise ProtocolError(
                "Technocore live timeline has a sequence gap",
                context={
                    "after_sequence": after_sequence,
                    "first_sequence": read.view.first_seq,
                },
            )
        entries = tuple(
            entry
            for message in read.view.messages
            if (entry := self._validated_entry(message)) is not None
        )
        return TimelineWindow(
            cursor_after=read.view.last_seq,
            entries=entries,
            at_limit=len(read.view.messages) == MAX_MESSAGES_PER_READ,
        )

    def _validated_entry(self, message: RoomMessage) -> TimelineEntry | None:
        sequence = message.seq
        sender_did = message.sender
        text = message.text
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolError("Technocore timeline contains non-JSON content") from exc
        if not isinstance(document, dict):
            raise ProtocolError("Technocore timeline entry is not a JSON object")
        if document.get("channel") == "collaboration":
            return self._collaboration_entry(sequence, sender_did, text, document)
        return self._workflow_entry(sequence, sender_did, text, document)

    def _collaboration_entry(
        self,
        sequence: int,
        sender_did: str,
        text: str,
        document: dict[str, object],
    ) -> TimelineEntry:
        try:
            envelope = CollaborationEnvelope.model_validate(document)
        except ValidationError as exc:
            raise ProtocolError("Technocore collaboration timeline entry is invalid") from exc
        if canonical_collaboration_envelope(envelope) != text:
            raise ProtocolError("Technocore collaboration timeline entry is not canonical")
        if envelope.run_id != self._run_id or envelope.task_id != self._task_id:
            raise ProtocolError("Technocore collaboration timeline entry belongs to another run")
        if self._did_by_role.get(envelope.sender) != sender_did:
            raise ProtocolError("Technocore collaboration timeline sender is not authorized")
        stored = self._store.get_collaboration_message(str(envelope.event_id))
        if (
            stored.envelope != envelope
            or stored.transport_status is not TransportStatus.PUBLISHED
            or stored.technocore_seq != sequence
        ):
            raise ProtocolError("Technocore collaboration timeline differs from durable state")
        return TimelineEntry(
            sequence=sequence,
            created_at=envelope.created_at,
            sender=envelope.sender,
            kind=envelope.kind.value,
            reply_to=envelope.reply_to,
            text=_safe_text(envelope.text),
        )

    def _workflow_entry(
        self,
        sequence: int,
        sender_did: str,
        text: str,
        document: dict[str, object],
    ) -> TimelineEntry | None:
        try:
            event = EventEnvelope.model_validate(document)
        except ValidationError as exc:
            raise ProtocolError("Technocore workflow timeline entry is invalid") from exc
        if canonical_event_json(event) != text:
            raise ProtocolError("Technocore workflow timeline entry is not canonical")
        if event.run_id != self._run_id or event.task_id != self._task_id:
            raise ProtocolError("Technocore workflow timeline entry belongs to another run")
        if self._did_by_role.get(event.sender) != sender_did:
            raise ProtocolError("Technocore workflow timeline sender is not authorized")
        matches = tuple(
            record
            for record in self._store.list_event_records(self._run_id)
            if record.event.event_id == event.event_id
        )
        if (
            len(matches) != 1
            or matches[0].event != event
            or matches[0].transport_status is not TransportStatus.PUBLISHED
            or matches[0].technocore_seq != sequence
        ):
            raise ProtocolError("Technocore workflow timeline differs from durable state")
        if event.sender in {Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER}:
            return None
        return TimelineEntry(
            sequence=sequence,
            created_at=event.created_at,
            sender=event.sender,
            kind=event.kind.value,
            reply_to=event.reply_to,
            text=_safe_text(event.summary),
        )


def _safe_text(value: str) -> str:
    return "".join(
        " " if unicodedata.category(character).startswith("C") else character for character in value
    )
