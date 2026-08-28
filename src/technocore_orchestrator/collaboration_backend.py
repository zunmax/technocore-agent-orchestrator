"""Durable signed collaboration over one owned Technocore room."""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from uuid import UUID, uuid4

from pydantic import ValidationError

from technocore_orchestrator.domain.collaboration import (
    CandidateHandoff,
    CollaborationEnvelope,
    CollaborationKind,
    CollaborationPayload,
    ConversationMessage,
    ConversationWindow,
    HandoffAcknowledgement,
    PublicationResult,
    canonical_collaboration_envelope,
    collaboration_payload_sha256,
)
from technocore_orchestrator.domain.models import RUN_ID_RE, TASK_ID_RE, Role
from technocore_orchestrator.errors import IdentityError, ProtocolError
from technocore_orchestrator.identity import RoleIdentity, clean_text, validate_room_name
from technocore_orchestrator.storage import SQLiteStore, TransportStatus
from technocore_orchestrator.technocore import (
    RoomMessage,
    TechnocoreClient,
    WriteOutcomeUnknownError,
)

_MAX_ENVELOPE_BYTES = 3_072


class TechnocoreCollaborationBackend:
    """Publish small signed envelopes while SQLite retains their exact full payloads."""

    def __init__(
        self,
        *,
        client: TechnocoreClient,
        store: SQLiteStore,
        room: str,
        run_id: str,
        task_id: str,
        identities: Mapping[Role, RoleIdentity],
    ) -> None:
        if not RUN_ID_RE.fullmatch(run_id) or not TASK_ID_RE.fullmatch(task_id):
            raise ProtocolError("collaboration backend received an invalid run or task id")
        self._client = client
        self._store = store
        self._room = validate_room_name(room)
        self._run_id = run_id
        self._task_id = task_id
        self._identities = dict(identities)
        self._did_roles = {identity.public.did: role for role, identity in identities.items()}
        if len(self._did_roles) != len(self._identities):
            raise IdentityError("collaboration roles must use distinct Technocore identities")

    async def read_new_messages(self, role: Role, after_sequence: int) -> ConversationWindow:
        self._require_identity(role)
        read = await self._client.read_room(
            self._room,
            cursor=after_sequence,
            wait_seconds=0,
            limit=200,
        )
        if read.gap_detected:
            raise ProtocolError(
                "Technocore collaboration history has a sequence gap",
                context={"after_sequence": after_sequence, "first_sequence": read.view.first_seq},
            )
        messages: list[ConversationMessage] = []
        for room_message in read.view.messages:
            envelope = self._parse_room_envelope(room_message.text)
            if envelope is None:
                continue
            expected_role = self._did_roles.get(room_message.sender)
            if expected_role is None or expected_role is not envelope.sender:
                raise ProtocolError(
                    "Technocore collaboration sender identity does not match its role"
                )
            if envelope.run_id != self._run_id or envelope.task_id != self._task_id:
                raise ProtocolError("Technocore collaboration message belongs to another run")
            stored = self._store.get_collaboration_message(str(envelope.event_id))
            if (
                stored.envelope != envelope
                or stored.transport_status is not TransportStatus.PUBLISHED
                or stored.technocore_seq != room_message.seq
            ):
                raise ProtocolError("Technocore collaboration message does not match durable state")
            messages.append(
                ConversationMessage(
                    sequence=room_message.seq,
                    event_id=envelope.event_id,
                    sender=envelope.sender,
                    kind=envelope.kind,
                    reply_to=envelope.reply_to,
                    text=envelope.text,
                    payload_sha256=envelope.payload_sha256,
                    payload=stored.payload.model_dump(mode="json"),
                )
            )
        return ConversationWindow(
            cursor_before=after_sequence,
            cursor_after=read.view.last_seq,
            gap_detected=False,
            messages=tuple(messages),
        )

    async def publish(
        self,
        *,
        role: Role,
        kind: CollaborationKind,
        payload: CollaborationPayload,
        reply_to: UUID | None,
    ) -> PublicationResult:
        identity = self._require_identity(role)
        digest = collaboration_payload_sha256(payload)
        envelope = CollaborationEnvelope(
            v=1,
            event_id=uuid4(),
            run_id=self._run_id,
            task_id=self._task_id,
            kind=kind,
            sender=role,
            created_at=datetime.now(UTC),
            reply_to=reply_to,
            text=_payload_text(payload),
            payload_sha256=digest,
        )
        self._store.record_collaboration_message(envelope, payload)
        text = clean_text(canonical_collaboration_envelope(envelope))
        encoded_bytes = len(text.encode("utf-8"))
        if encoded_bytes > _MAX_ENVELOPE_BYTES:
            self._store.mark_collaboration_transport(str(envelope.event_id), status="failed")
            raise ProtocolError(
                "collaboration envelope exceeds its Technocore transport limit",
                context={"bytes": encoded_bytes},
            )
        nonce = self._store.allocate_nonce(identity.public.did, self._room)
        signed = identity.sign_room_message(self._room, nonce, text)
        try:
            posted = await self._client.publish_signed(self._room, signed)
        except WriteOutcomeUnknownError as exc:
            self._store.mark_collaboration_transport(str(envelope.event_id), status="uncertain")
            posted = await self._reconcile_unknown_write(identity, text)
            if posted is None:
                raise exc
        except Exception:
            self._store.mark_collaboration_transport(str(envelope.event_id), status="failed")
            raise
        self._store.mark_collaboration_transport(
            str(envelope.event_id), status="published", technocore_seq=posted.seq
        )
        return PublicationResult(
            sequence=posted.seq,
            event_id=envelope.event_id,
            payload_sha256=digest,
        )

    async def _reconcile_unknown_write(
        self, identity: RoleIdentity, text: str
    ) -> RoomMessage | None:
        read = await self._client.read_room(self._room, cursor=0, wait_seconds=0, limit=200)
        if read.gap_detected:
            raise WriteOutcomeUnknownError(
                "Technocore history gap prevents collaboration-write reconciliation"
            )
        matches = tuple(
            message
            for message in read.view.messages
            if message.sender == identity.public.did and message.text == text
        )
        if len(matches) > 1:
            raise ProtocolError("Technocore contains duplicate copies of one collaboration message")
        return matches[0] if matches else None

    async def acknowledge(
        self,
        *,
        role: Role,
        event_id: UUID,
        sequence: int,
        payload_sha256: str,
    ) -> PublicationResult:
        target = self._store.get_collaboration_message(str(event_id))
        if (
            target.transport_status is not TransportStatus.PUBLISHED
            or target.technocore_seq != sequence
            or target.envelope.payload_sha256 != payload_sha256
        ):
            raise ProtocolError("acknowledgement does not match the exact published handoff")
        acknowledgement = HandoffAcknowledgement(
            event_id=event_id,
            sequence=sequence,
            payload_sha256=payload_sha256,
        )
        return await self.publish(
            role=role,
            kind=CollaborationKind.HANDOFF_ACKNOWLEDGED,
            payload=acknowledgement,
            reply_to=event_id,
        )

    def _require_identity(self, role: Role) -> RoleIdentity:
        identity = self._identities.get(role)
        if identity is None:
            raise IdentityError(
                "collaboration role does not have a signing identity",
                context={"role": role},
            )
        return identity

    @staticmethod
    def _parse_room_envelope(text: str) -> CollaborationEnvelope | None:
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProtocolError("Technocore room contains a non-JSON signed message") from exc
        if not isinstance(document, dict):
            raise ProtocolError("Technocore room message is not a JSON object")
        if document.get("channel") != "collaboration":
            return None
        try:
            envelope = CollaborationEnvelope.model_validate(document)
        except ValidationError as exc:
            raise ProtocolError("Technocore collaboration envelope failed validation") from exc
        if canonical_collaboration_envelope(envelope) != text:
            raise ProtocolError("Technocore collaboration envelope is not canonical")
        return envelope


def _payload_text(payload: CollaborationPayload) -> str:
    if isinstance(payload, HandoffAcknowledgement):
        return f"Acknowledged event {payload.event_id} at Technocore sequence {payload.sequence}."
    if isinstance(payload, CandidateHandoff):
        return f"Verified candidate {payload.candidate_commit} is ready for exact-commit review."
    summary = getattr(payload, "summary", None)
    if not isinstance(summary, str):
        raise ProtocolError("collaboration payload does not provide readable text")
    normalized = " ".join(summary.split())
    if not normalized:
        raise ProtocolError("collaboration payload summary became empty")
    return normalized[:600]
