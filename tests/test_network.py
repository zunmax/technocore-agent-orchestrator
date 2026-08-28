from __future__ import annotations

import ipaddress
import socket

import pytest

from technocore_orchestrator import network
from technocore_orchestrator.errors import PreflightError
from technocore_orchestrator.network import ListenerBinding


def _binding(address: str, port: int = 8080) -> ListenerBinding:
    return ListenerBinding(
        address=ipaddress.ip_address(address),
        port=port,
        owning_process=123,
    )


def test_loopback_listener_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network, "list_tcp_listeners", lambda: (_binding("127.0.0.1"),))

    assert network.require_loopback_technocore_listener("http://127.0.0.1:8080") == (
        _binding("127.0.0.1"),
    )


@pytest.mark.parametrize("address", ("0.0.0.0", "192.168.1.20", "::"))
def test_non_loopback_listener_fails_closed(monkeypatch: pytest.MonkeyPatch, address: str) -> None:
    monkeypatch.setattr(network, "list_tcp_listeners", lambda: (_binding(address),))

    with pytest.raises(PreflightError, match="exposed beyond the local computer"):
        network.require_loopback_technocore_listener("http://127.0.0.1:8080")


def test_missing_listener_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(network, "list_tcp_listeners", tuple)

    with pytest.raises(PreflightError, match="does not have a TCP listener"):
        network.require_loopback_technocore_listener("http://127.0.0.1:8080")


def test_windows_listener_table_reports_a_real_loopback_socket() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        address, port = listener.getsockname()

        matches = tuple(item for item in network.list_tcp_listeners() if item.port == port)

    assert matches
    assert all(item.address.is_loopback for item in matches)
    assert any(str(item.address) == address for item in matches)
