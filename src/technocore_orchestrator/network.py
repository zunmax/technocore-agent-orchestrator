"""Windows listener inspection for the localhost-only Technocore boundary."""

from __future__ import annotations

import ctypes
import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

from technocore_orchestrator.errors import PreflightError

_AF_INET = 2
_AF_INET6 = 23
_ERROR_INSUFFICIENT_BUFFER = 122
_NO_ERROR = 0
_TCP_TABLE_OWNER_PID_LISTENER = 3


class _IPv4ListenerRow(ctypes.Structure):
    _fields_ = (
        ("state", ctypes.c_ulong),
        ("local_address", ctypes.c_ulong),
        ("local_port", ctypes.c_ulong),
        ("remote_address", ctypes.c_ulong),
        ("remote_port", ctypes.c_ulong),
        ("owning_process", ctypes.c_ulong),
    )


class _IPv6ListenerRow(ctypes.Structure):
    _fields_ = (
        ("local_address", ctypes.c_ubyte * 16),
        ("local_scope_id", ctypes.c_ulong),
        ("local_port", ctypes.c_ulong),
        ("remote_address", ctypes.c_ubyte * 16),
        ("remote_scope_id", ctypes.c_ulong),
        ("remote_port", ctypes.c_ulong),
        ("state", ctypes.c_ulong),
        ("owning_process", ctypes.c_ulong),
    )


@dataclass(frozen=True, slots=True)
class ListenerBinding:
    address: ipaddress.IPv4Address | ipaddress.IPv6Address
    port: int
    owning_process: int


def require_loopback_technocore_listener(base_url: str) -> tuple[ListenerBinding, ...]:
    """Fail unless every Windows TCP listener on the configured port is loopback-only."""

    parsed = urlsplit(base_url)
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise PreflightError("Technocore base URL contains an invalid port") from exc
    bindings = tuple(binding for binding in list_tcp_listeners() if binding.port == port)
    if not bindings:
        raise PreflightError(
            "Technocore does not have a TCP listener on its configured port",
            context={"port": port},
        )
    exposed = tuple(str(binding.address) for binding in bindings if not binding.address.is_loopback)
    if exposed:
        raise PreflightError(
            "Technocore listener is exposed beyond the local computer",
            context={"port": port, "unsafe_bindings": sorted(set(exposed))},
        )
    return bindings


def list_tcp_listeners() -> tuple[ListenerBinding, ...]:
    """Return IPv4 and IPv6 listener bindings from the Windows IP Helper API."""

    api = ctypes.WinDLL("iphlpapi", use_last_error=True)
    raw_get_table = api.GetExtendedTcpTable
    raw_get_table.argtypes = (
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_ulong),
        ctypes.c_bool,
        ctypes.c_ulong,
        ctypes.c_int,
        ctypes.c_ulong,
    )
    raw_get_table.restype = ctypes.c_ulong
    get_table: Callable[..., int] = raw_get_table
    bindings = [*_read_table(get_table, _AF_INET), *_read_table(get_table, _AF_INET6)]
    return tuple(sorted(bindings, key=lambda item: (item.port, item.address.version, item.address)))


def _read_table(get_table: Callable[..., int], family: int) -> tuple[ListenerBinding, ...]:
    size = ctypes.c_ulong(0)
    result = get_table(
        None,
        ctypes.byref(size),
        False,
        family,
        _TCP_TABLE_OWNER_PID_LISTENER,
        0,
    )
    if result != _ERROR_INSUFFICIENT_BUFFER or size.value < ctypes.sizeof(ctypes.c_ulong):
        raise PreflightError(
            "Windows could not size its TCP listener table",
            context={"family": family, "win32_error": result},
        )
    buffer = ctypes.create_string_buffer(size.value)
    result = get_table(
        buffer,
        ctypes.byref(size),
        False,
        family,
        _TCP_TABLE_OWNER_PID_LISTENER,
        0,
    )
    if result != _NO_ERROR:
        raise PreflightError(
            "Windows could not read its TCP listener table",
            context={"family": family, "win32_error": result},
        )
    count = ctypes.c_ulong.from_buffer_copy(buffer.raw).value
    row_type = _IPv4ListenerRow if family == _AF_INET else _IPv6ListenerRow
    offset = ctypes.sizeof(ctypes.c_ulong)
    required = offset + count * ctypes.sizeof(row_type)
    if required > size.value:
        raise PreflightError("Windows returned a truncated TCP listener table")
    rows = (row_type * count).from_buffer_copy(buffer.raw, offset)
    return tuple(_binding_from_row(row, family) for row in rows)


def _binding_from_row(row: _IPv4ListenerRow | _IPv6ListenerRow, family: int) -> ListenerBinding:
    if family == _AF_INET:
        address = ipaddress.IPv4Address(int(row.local_address).to_bytes(4, "little"))
    else:
        address = ipaddress.IPv6Address(bytes(row.local_address))
    return ListenerBinding(
        address=address,
        port=socket.ntohs(int(row.local_port)),
        owning_process=int(row.owning_process),
    )
