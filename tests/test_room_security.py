from __future__ import annotations

import asyncio
import base64
from pathlib import Path
from typing import Any, cast

import httpx
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from technocore_orchestrator.config import TechnocoreConfig
from technocore_orchestrator.domain.models import Role
from technocore_orchestrator.errors import PreflightError
from technocore_orchestrator.identity import RoleIdentity
from technocore_orchestrator.real_runtime import _room_for_run
from technocore_orchestrator.room_security import OwnedRoomProvisioner
from technocore_orchestrator.storage import SQLiteStore
from technocore_orchestrator.technocore import TechnocoreClient


def _identity() -> RoleIdentity:
    return RoleIdentity(Ed25519PrivateKey.generate())


def test_note_signature_covers_the_ownership_canonical_form() -> None:
    key = Ed25519PrivateKey.generate()
    identity = RoleIdentity(key)

    signed = identity.sign_note_message(
        "room-owners", "d-p-orchestrator-room", 1, identity.public.did
    )

    signature = base64.urlsafe_b64decode(signed.signature + "==")
    canonical = f"room-owners|d-p-orchestrator-room|1|{identity.public.did}".encode()
    key.public_key().verify(signature, canonical)


def test_new_run_room_is_owned_and_unlisted(tmp_path: Path) -> None:
    room = _room_for_run(tmp_path, "run_12345678", resume=False)

    assert room.startswith("d-p-orchestrator-")
    assert len(room) <= 48
    assert _room_for_run(tmp_path, "run_12345678", resume=True) == room


def test_client_writes_and_reads_signed_ownership_notes() -> None:
    identity = _identity()
    signed = identity.sign_note_message(
        "room-owners", "d-p-orchestrator-room", 1, identity.public.did
    )
    requests: list[httpx.Request] = []

    async def exercise() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            if request.method == "POST":
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(200, text=f"banner\n\n{identity.public.did}\n")

        client = TechnocoreClient(TechnocoreConfig(), transport=httpx.MockTransport(handler))
        try:
            await client.publish_signed_note(signed, if_absent=True)
            assert (
                await client.read_note("room-owners", "d-p-orchestrator-room")
                == identity.public.did
            )
        finally:
            await client.aclose()

    asyncio.run(exercise())
    assert requests[0].url.path == "/kv/room-owners/d-p-orchestrator-room"
    assert requests[0].url.params["format"] == "json"
    assert b'"if_absent":true' in requests[0].content


class _MemoryStore:
    def __init__(self) -> None:
        self.nonce = 0

    def allocate_nonce(self, _did: str, _room: str) -> int:
        self.nonce += 1
        return self.nonce


class _MemoryClient:
    def __init__(self) -> None:
        self.notes: dict[tuple[str, str], str] = {}

    async def read_note(self, namespace: str, key: str) -> str | None:
        return self.notes.get((namespace, key))

    async def publish_signed_note(self, signed: Any, *, if_absent: bool = False) -> None:
        note = (signed.namespace, signed.key)
        if if_absent and note in self.notes:
            raise AssertionError("test attempted to replace an existing ownership claim")
        self.notes[note] = signed.value


def test_provisioner_claims_room_and_allows_exact_role_dids() -> None:
    identities = {
        Role.SUPERVISOR: _identity(),
        Role.PLANNER: _identity(),
        Role.IMPLEMENTER: _identity(),
        Role.REVIEWER: _identity(),
    }
    client = _MemoryClient()
    provisioner = OwnedRoomProvisioner(
        client=cast(TechnocoreClient, client),
        store=cast(SQLiteStore, _MemoryStore()),
        room="d-p-orchestrator-room",
        identities=identities,
    )

    asyncio.run(provisioner.provision())

    assert (
        client.notes[("room-owners", "d-p-orchestrator-room")]
        == identities[Role.SUPERVISOR].public.did
    )
    assert set(client.notes[("room-allow", "d-p-orchestrator-room")].split()) == {
        identities[role].public.did for role in (Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER)
    }


def test_provisioner_rejects_a_room_claimed_by_another_identity() -> None:
    identities = {
        Role.SUPERVISOR: _identity(),
        Role.PLANNER: _identity(),
        Role.IMPLEMENTER: _identity(),
        Role.REVIEWER: _identity(),
    }
    client = _MemoryClient()
    client.notes[("room-owners", "d-p-orchestrator-room")] = _identity().public.did
    provisioner = OwnedRoomProvisioner(
        client=cast(TechnocoreClient, client),
        store=cast(SQLiteStore, _MemoryStore()),
        room="d-p-orchestrator-room",
        identities=identities,
    )

    with pytest.raises(PreflightError, match="unexpected identity"):
        asyncio.run(provisioner.provision())
