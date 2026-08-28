"""Owned, unlisted room provisioning for real workflows."""

from __future__ import annotations

from collections.abc import Mapping

from technocore_orchestrator.domain.models import Role
from technocore_orchestrator.errors import IdentityError, PreflightError
from technocore_orchestrator.identity import RoleIdentity, validate_room_name
from technocore_orchestrator.storage import SQLiteStore
from technocore_orchestrator.technocore import TechnocoreClient, WriteOutcomeUnknownError

_OWNER_NAMESPACE = "room-owners"
_ALLOW_NAMESPACE = "room-allow"


class OwnedRoomProvisioner:
    """Claim one room and restrict its signed write lane to configured role identities."""

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
        try:
            self._owner = identities[Role.SUPERVISOR]
            role_identities = tuple(
                identities[role] for role in (Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER)
            )
        except KeyError as exc:
            raise IdentityError("owned room provisioning requires every workflow identity") from exc
        self._allowed = " ".join(sorted(identity.public.did for identity in role_identities))

    async def provision(self) -> None:
        owner_did = self._owner.public.did
        current_owner = await self._client.read_note(_OWNER_NAMESPACE, self._room)
        if current_owner is None:
            await self._write(_OWNER_NAMESPACE, owner_did, if_absent=True)
        elif current_owner != owner_did:
            raise PreflightError("Technocore room is owned by an unexpected identity")

        current_allow = await self._client.read_note(_ALLOW_NAMESPACE, self._room)
        if current_allow != self._allowed:
            await self._write(_ALLOW_NAMESPACE, self._allowed)

        verified_owner = await self._client.read_note(_OWNER_NAMESPACE, self._room)
        verified_allow = await self._client.read_note(_ALLOW_NAMESPACE, self._room)
        if verified_owner != owner_did or verified_allow != self._allowed:
            raise PreflightError("Technocore owned-room policy did not verify after provisioning")

    async def _write(self, namespace: str, value: str, *, if_absent: bool = False) -> None:
        nonce = self._store.allocate_nonce(self._owner.public.did, self._room)
        signed = self._owner.sign_note_message(namespace, self._room, nonce, value)
        try:
            await self._client.publish_signed_note(signed, if_absent=if_absent)
        except WriteOutcomeUnknownError:
            if await self._client.read_note(namespace, self._room) != value:
                raise
