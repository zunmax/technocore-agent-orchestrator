"""Bounded async client for the pinned Technocore v0.8.0 room contract."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Annotated, Any

import httpx
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    ValidationError,
    model_validator,
)

from technocore_orchestrator.config import TechnocoreConfig
from technocore_orchestrator.errors import TransportError
from technocore_orchestrator.identity import (
    SignedNoteMessage,
    SignedRoomMessage,
    validate_room_name,
)

MAX_RESPONSE_BYTES = 1 << 20
MAX_ERROR_PREVIEW = 500
MAX_MESSAGES_PER_READ = 200


class TechnocoreRateLimitError(TransportError):
    def __init__(self, retry_after_seconds: int | None, preview: str) -> None:
        super().__init__(
            "Technocore rate limit was reached",
            context={"retry_after_seconds": retry_after_seconds, "response": preview},
        )
        self.retry_after_seconds = retry_after_seconds


class WriteOutcomeUnknownError(TransportError):
    """The request may have committed; callers must reconcile before retrying."""


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class RoomMessage(ClosedModel):
    seq: Annotated[StrictInt, Field(ge=1)]
    ts: Annotated[StrictStr, Field(min_length=1, max_length=64)]
    sender: Annotated[StrictStr, Field(alias="from", min_length=1, max_length=128)]
    text: Annotated[StrictStr, Field(min_length=1, max_length=4096)]
    nonce: Annotated[StrictInt, Field(ge=0)] | None = None


class RoomView(ClosedModel):
    room: Annotated[StrictStr, Field(min_length=1, max_length=48)]
    count: Annotated[StrictInt, Field(ge=0, le=MAX_MESSAGES_PER_READ)]
    first_seq: Annotated[StrictInt, Field(ge=1)] | None
    last_seq: Annotated[StrictInt, Field(ge=0)]
    messages: tuple[RoomMessage, ...]
    posted: RoomMessage | None = None

    @model_validator(mode="after")
    def validate_sequence_window(self) -> RoomView:
        if self.count != len(self.messages):
            raise ValueError("room count does not match the number of messages")
        sequences = tuple(message.seq for message in self.messages)
        if sequences != tuple(sorted(sequences)) or len(set(sequences)) != len(sequences):
            raise ValueError("room messages must have unique increasing sequences")
        if self.messages:
            if self.first_seq != self.messages[0].seq or self.last_seq != self.messages[-1].seq:
                raise ValueError("room sequence bounds do not match the returned messages")
        elif self.first_seq is not None:
            raise ValueError("an empty room response must have first_seq=null")
        return self


@dataclass(frozen=True, slots=True)
class RoomRead:
    view: RoomView
    gap_detected: bool


class TechnocoreClient:
    """One explicitly bounded client instance; it never follows redirects or environment proxies."""

    def __init__(
        self,
        config: TechnocoreConfig,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        max_response_bytes: int = MAX_RESPONSE_BYTES,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        read_timeout = float(config.long_poll_seconds + 5)
        timeout = httpx.Timeout(connect=5.0, read=read_timeout, write=5.0, pool=5.0)
        limits = httpx.Limits(max_connections=4, max_keepalive_connections=2)
        self._max_response_bytes = max_response_bytes
        self._client = httpx.AsyncClient(
            base_url=config.base_url.rstrip("/") + "/",
            follow_redirects=False,
            trust_env=False,
            timeout=timeout,
            limits=limits,
            transport=transport,
            headers={
                "Accept": "application/json",
                "User-Agent": "technocore-agent-orchestrator/0.1",
            },
        )

    async def __aenter__(self) -> TechnocoreClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._client.aclose()

    async def health(self) -> None:
        status, _headers, body = await self._request("GET", "healthz")
        if status != 200 or body not in {b"ok", b"ok\n", b"ok\r\n"}:
            raise TransportError(
                "Technocore health contract failed",
                context={"status": status, "response": _safe_preview(body)},
            )

    async def manifest_version(self) -> str:
        status, headers, body = await self._request("GET", ".well-known/agent.json")
        self._raise_for_status(status, headers, body)
        try:
            document = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransportError("Technocore manifest is not valid UTF-8 JSON") from exc
        if not isinstance(document, dict) or document.get("name") != "technocore-chat":
            raise TransportError("Technocore manifest identifies an unexpected service")
        version = document.get("version")
        if not isinstance(version, str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version):
            raise TransportError("Technocore manifest version is invalid")
        return version

    async def read_room(
        self,
        room: str,
        *,
        cursor: int = 0,
        wait_seconds: int = 0,
        limit: int = 50,
        poll_counter: int = 0,
    ) -> RoomRead:
        validate_room_name(room)
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise ValueError("cursor must be a non-negative integer")
        if (
            isinstance(wait_seconds, bool)
            or not isinstance(wait_seconds, int)
            or not 0 <= wait_seconds <= 10
        ):
            raise ValueError("wait_seconds must be an integer from 0 through 10")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit must be an integer from 1 through 200")
        if isinstance(poll_counter, bool) or not isinstance(poll_counter, int) or poll_counter < 0:
            raise ValueError("poll_counter must be a non-negative integer")
        params: dict[str, str] = {
            "format": "json",
            "since": str(cursor),
            "limit": str(limit),
            "n": str(poll_counter),
        }
        if wait_seconds:
            params["wait"] = str(wait_seconds)
        status, headers, body = await self._request(
            "GET", f"r/{room}", params=params, is_write=False
        )
        self._raise_for_status(status, headers, body)
        view = _parse_room_view(body)
        if view.room != room:
            raise TransportError(
                "Technocore response named a different room",
                context={"expected": room, "received": view.room},
            )
        if view.last_seq < cursor:
            raise TransportError(
                "Technocore cursor regressed; the room may have been reaped and recreated",
                context={"cursor": cursor, "last_seq": view.last_seq},
            )
        gap = view.first_seq is not None and view.first_seq > cursor + 1
        return RoomRead(view=view, gap_detected=gap)

    async def publish_signed(
        self,
        room: str,
        signed: SignedRoomMessage,
    ) -> RoomMessage:
        validate_room_name(room)
        payload = {
            "did": signed.did,
            "sig": signed.signature,
            "nonce": str(signed.nonce),
            "text": signed.text,
        }
        status, headers, body = await self._request(
            "POST",
            f"r/{room}",
            params={"format": "json"},
            json_body=payload,
            is_write=True,
        )
        self._raise_for_status(status, headers, body)
        try:
            view = _parse_room_view(body)
            posted = view.posted
            if view.room != room or posted is None:
                raise TransportError("Technocore signed write response omitted the posted record")
            if (
                posted.sender != signed.did
                or posted.text != signed.text
                or posted.nonce != signed.nonce
            ):
                raise TransportError("Technocore posted record does not match the signed request")
        except TransportError as exc:
            raise WriteOutcomeUnknownError(
                "Technocore accepted a signed write but returned an unverifiable record"
            ) from exc
        return posted

    async def publish_signed_note(
        self,
        signed: SignedNoteMessage,
        *,
        if_absent: bool = False,
    ) -> None:
        if signed.namespace not in {"room-owners", "room-allow"}:
            raise ValueError("signed note namespace is unsupported")
        validate_room_name(signed.key)
        payload: dict[str, Any] = {
            "value": signed.value,
            "did": signed.did,
            "sig": signed.signature,
            "nonce": str(signed.nonce),
        }
        if if_absent:
            payload["if_absent"] = True
        status, headers, body = await self._request(
            "POST",
            f"kv/{signed.namespace}/{signed.key}",
            params={"format": "json"},
            json_body=payload,
            is_write=True,
        )
        self._raise_for_status(status, headers, body)

    async def read_note(self, namespace: str, key: str) -> str | None:
        if namespace not in {"room-owners", "room-allow"}:
            raise ValueError("note namespace is unsupported")
        validate_room_name(key)
        status, headers, body = await self._request("GET", f"kv/{namespace}/{key}")
        if status == 404:
            return None
        self._raise_for_status(status, headers, body)
        try:
            document = body.decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise TransportError("Technocore returned a non-UTF-8 note") from exc
        marker = "\n\n"
        if marker not in document:
            raise TransportError("Technocore returned an invalid note document")
        value = document.split(marker, 1)[1].split("\n# budget:", 1)[0].strip()
        if not value:
            raise TransportError("Technocore returned an empty ownership note")
        return value

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
        is_write: bool = False,
    ) -> tuple[int, httpx.Headers, bytes]:
        try:
            async with self._client.stream(method, path, params=params, json=json_body) as response:
                declared = response.headers.get("content-length")
                if declared is not None:
                    try:
                        declared_bytes = int(declared)
                    except ValueError as exc:
                        raise TransportError(
                            "Technocore returned an invalid Content-Length"
                        ) from exc
                    if declared_bytes < 0 or declared_bytes > self._max_response_bytes:
                        raise TransportError(
                            "Technocore response exceeds the configured byte limit",
                            context={"declared_bytes": declared_bytes},
                        )
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_response_bytes:
                        raise TransportError(
                            "Technocore response exceeds the configured byte limit",
                            context={"received_bytes": len(body)},
                        )
                return response.status_code, response.headers, bytes(body)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout) as exc:
            raise TransportError(
                "Technocore connection failed before a known write outcome"
            ) from exc
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.RemoteProtocolError) as exc:
            if is_write:
                raise WriteOutcomeUnknownError(
                    "Technocore signed write has an unknown outcome; reconcile before retrying"
                ) from exc
            raise TransportError("Technocore read failed") from exc
        except httpx.HTTPError as exc:
            if is_write:
                raise WriteOutcomeUnknownError(
                    "Technocore signed write has an unknown outcome; reconcile before retrying"
                ) from exc
            raise TransportError("Technocore request failed") from exc

    @staticmethod
    def _raise_for_status(status: int, headers: httpx.Headers, body: bytes) -> None:
        if status == 200:
            return
        preview = _safe_preview(body)
        if status == 429:
            raw_retry = headers.get("retry-after")
            retry = int(raw_retry) if raw_retry and re.fullmatch(r"[0-9]+", raw_retry) else None
            raise TechnocoreRateLimitError(retry, preview)
        raise TransportError(
            "Technocore request was refused",
            context={"status": status, "response": preview},
        )


def _parse_room_view(body: bytes) -> RoomView:
    try:
        payload = json.loads(body)
        return RoomView.model_validate(payload)
    except (UnicodeDecodeError, json.JSONDecodeError, ValidationError) as exc:
        raise TransportError("Technocore returned an invalid room JSON document") from exc


def _safe_preview(body: bytes) -> str:
    decoded = body.decode("utf-8", errors="replace")[:MAX_ERROR_PREVIEW]
    return "".join(
        " " if unicodedata.category(character).startswith("C") else character
        for character in decoded
    ).strip()
