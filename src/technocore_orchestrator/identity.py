"""DPAPI-protected Technocore identities and protocol-compatible signing."""

from __future__ import annotations

import base64
import ctypes
import os
import re
import secrets
import stat
import unicodedata
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from technocore_orchestrator.errors import IdentityError, ProtocolError

MULTICODEC_ED25519 = b"\xed\x01"
BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
INVISIBLE_CATEGORIES = frozenset({"Cc", "Cf", "Cs", "Co", "Zl", "Zp"})
ROOM_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,47}")
DID_RE = re.compile(r"did:key:z6Mk[1-9A-HJ-NP-Za-km-z]{44}")
PRIVATE_KEY_BYTES = 32
MAX_NONCE = (1 << 63) - 1
PROTECTED_IDENTITY_HEADER = "TCORE-DPAPI-1"
MAX_PROTECTED_IDENTITY_BYTES = 4096
_CRYPTPROTECT_UI_FORBIDDEN = 0x1
# Versioned application entropy binds DPAPI ciphertext to this identity format.
_DPAPI_ENTROPY = b"technocore-agent-orchestrator:identity:v1"


class _DataBlob(ctypes.Structure):
    _fields_ = (("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte)))


@dataclass(frozen=True, slots=True)
class PublicIdentity:
    did: str


@dataclass(frozen=True, slots=True)
class SignedRoomMessage:
    did: str
    signature: str
    nonce: int
    text: str


@dataclass(frozen=True, slots=True)
class SignedNoteMessage:
    namespace: str
    key: str
    did: str
    signature: str
    nonce: int
    value: str


class RoleIdentity:
    """Non-serializable private key wrapper kept outside domain/report models."""

    __slots__ = ("_key", "_public")

    def __init__(self, key: Ed25519PrivateKey) -> None:
        self._key = key
        self._public = PublicIdentity(did=did_from_public_key(key.public_key()))

    @property
    def public(self) -> PublicIdentity:
        return self._public

    def sign_room_message(self, room: str, nonce: int, text: str) -> SignedRoomMessage:
        validate_room_name(room)
        if isinstance(nonce, bool) or not isinstance(nonce, int) or not 0 <= nonce <= MAX_NONCE:
            raise ProtocolError("nonce must be an integer from 0 through the signed 64-bit maximum")
        cleaned = clean_text(text)
        canonical = f"{room}|{nonce}|{cleaned}".encode()
        encoded = base64.urlsafe_b64encode(self._key.sign(canonical)).decode("ascii").rstrip("=")
        if len(encoded) != 86:
            raise IdentityError("Ed25519 signer returned an unexpected signature length")
        return SignedRoomMessage(
            did=self._public.did,
            signature=encoded,
            nonce=nonce,
            text=cleaned,
        )

    def sign_note_message(
        self, namespace: str, key: str, nonce: int, value: str
    ) -> SignedNoteMessage:
        if namespace not in {"room-owners", "room-allow"}:
            raise ProtocolError("signed note namespace is not a room ownership namespace")
        validate_room_name(key)
        if isinstance(nonce, bool) or not isinstance(nonce, int) or not 0 <= nonce <= MAX_NONCE:
            raise ProtocolError("nonce must be an integer from 0 through the signed 64-bit maximum")
        cleaned = clean_text(value, limit=8192)
        canonical = f"{namespace}|{key}|{nonce}|{cleaned}".encode()
        encoded = base64.urlsafe_b64encode(self._key.sign(canonical)).decode("ascii").rstrip("=")
        if len(encoded) != 86:
            raise IdentityError("Ed25519 signer returned an unexpected signature length")
        return SignedNoteMessage(
            namespace=namespace,
            key=key,
            did=self._public.did,
            signature=encoded,
            nonce=nonce,
            value=cleaned,
        )


def create_protected_identity(path: Path) -> PublicIdentity:
    """Create a user-bound DPAPI-protected Ed25519 identity."""

    private_key_bytes = secrets.token_bytes(PRIVATE_KEY_BYTES)
    identity = RoleIdentity(Ed25519PrivateKey.from_private_bytes(private_key_bytes))
    _write_protected_identity(path, _protect_current_user(private_key_bytes))
    return identity.public


def load_protected_identity(path: Path) -> RoleIdentity:
    """Load one bounded, integrity-checked, current-user DPAPI identity file."""

    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise IdentityError("protected identity path must be a regular file, not a symlink")
        if not 1 <= info.st_size <= MAX_PROTECTED_IDENTITY_BYTES:
            raise IdentityError("protected identity file has an invalid size")
        lines = path.read_text(encoding="ascii").splitlines()
        if len(lines) != 2 or lines[0] != PROTECTED_IDENTITY_HEADER or not lines[1]:
            raise IdentityError("protected identity file has an invalid format")
        encrypted = base64.b64decode(lines[1], validate=True)
    except IdentityError:
        raise
    except (OSError, UnicodeError, ValueError) as exc:
        raise IdentityError("unable to read protected identity file") from exc
    private_key_bytes = _unprotect_current_user(encrypted)
    if len(private_key_bytes) != PRIVATE_KEY_BYTES:
        raise IdentityError("protected identity plaintext has an invalid size")
    return RoleIdentity(Ed25519PrivateKey.from_private_bytes(private_key_bytes))


def _write_protected_identity(path: Path, encrypted: bytes) -> None:
    target = path.absolute()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists() or target.is_symlink():
        raise IdentityError(
            "identity path already exists; existing identities are never overwritten"
        )
    content = PROTECTED_IDENTITY_HEADER + "\n" + base64.b64encode(encrypted).decode("ascii") + "\n"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise IdentityError("unable to create protected identity file") from exc


def _protect_current_user(value: bytes) -> bytes:
    return _dpapi_transform(value, protect=True)


def _unprotect_current_user(value: bytes) -> bytes:
    return _dpapi_transform(value, protect=False)


def _dpapi_transform(value: bytes, *, protect: bool) -> bytes:
    if not value:
        raise IdentityError("DPAPI input must not be empty")
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    input_blob, input_buffer = _data_blob(value)
    entropy_blob, entropy_buffer = _data_blob(_DPAPI_ENTROPY)
    output_blob = _DataBlob()
    if protect:
        function = crypt32.CryptProtectData
        function.argtypes = (
            ctypes.POINTER(_DataBlob),
            wintypes.LPCWSTR,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        function.restype = wintypes.BOOL
        succeeded = function(
            ctypes.byref(input_blob),
            "Technocore Agent Orchestrator identity",
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    else:
        function = crypt32.CryptUnprotectData
        function.argtypes = (
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.POINTER(_DataBlob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(_DataBlob),
        )
        function.restype = wintypes.BOOL
        succeeded = function(
            ctypes.byref(input_blob),
            None,
            ctypes.byref(entropy_blob),
            None,
            None,
            _CRYPTPROTECT_UI_FORBIDDEN,
            ctypes.byref(output_blob),
        )
    if not succeeded:
        error = ctypes.get_last_error()
        raise IdentityError(
            "Windows DPAPI could not protect the identity",
            context={"winerror": error},
        )
    del input_buffer, entropy_buffer
    kernel32.LocalFree.argtypes = (ctypes.c_void_p,)
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def _data_blob(value: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_ubyte]]:
    buffer = (ctypes.c_ubyte * len(value)).from_buffer_copy(value)
    return _DataBlob(len(value), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer


def did_from_public_key(key: Ed25519PublicKey) -> str:
    raw = key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    did = "did:key:z" + _base58_encode(MULTICODEC_ED25519 + raw)
    if not DID_RE.fullmatch(did):
        raise IdentityError("generated public key does not have the canonical Ed25519 did:key form")
    return did


def clean_text(text: str, *, limit: int = 4096) -> str:
    """Apply Technocore v0.8.0's exact single-line sweep before signing."""

    if not isinstance(text, str):
        raise ProtocolError("Technocore message text must be a string")
    cleaned = "".join(
        " " if unicodedata.category(character) in INVISIBLE_CATEGORIES else character
        for character in text
    ).strip()
    if not cleaned:
        raise ProtocolError("Technocore message is empty after the single-line sweep")
    if len(cleaned) > limit:
        raise ProtocolError(
            "Technocore message exceeds the configured character limit",
            context={"characters": len(cleaned), "limit": limit},
        )
    return cleaned


def validate_room_name(room: str) -> str:
    if not isinstance(room, str) or not ROOM_RE.fullmatch(room):
        raise ProtocolError("Technocore room name does not match the v0.8.0 grammar")
    return room


def _base58_encode(raw: bytes) -> str:
    zeroes = len(raw) - len(raw.lstrip(b"\0"))
    value = int.from_bytes(raw, "big")
    encoded = ""
    while value:
        value, remainder = divmod(value, 58)
        encoded = BASE58_ALPHABET[remainder] + encoded
    return "1" * zeroes + encoded
