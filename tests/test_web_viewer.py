from __future__ import annotations

import threading

import httpx
import pytest

from technocore_orchestrator.errors import ConfigurationError
from technocore_orchestrator.web_viewer import ConversationViewerServer, render_conversation_page


def test_browser_viewer_serves_only_its_random_loopback_url() -> None:
    observed_cursors: list[int] = []

    def snapshot_reader(cursor: int) -> dict[str, object]:
        observed_cursors.append(cursor)
        return {
            "state": "planning",
            "terminal": False,
            "cursor": 7,
            "at_limit": False,
            "entries": [
                {
                    "sequence": 7,
                    "created_at": "2026-08-27T12:00:00+00:00",
                    "sender": "planner",
                    "agent": "Codex",
                    "kind": "plan_proposed",
                    "reply_to": None,
                    "text": "Build the accessible product.",
                }
            ],
        }

    with ConversationViewerServer("run_12345678", snapshot_reader) as server:
        serving = threading.Thread(target=server.serve_until_closed, daemon=True)
        serving.start()
        with httpx.Client(timeout=5) as client:
            page = client.get(server.url)
            assert page.status_code == 200
            assert "Technocore Agent Orchestrator" in page.text
            assert "default-src 'none'" in page.headers["content-security-policy"]
            assert page.headers["cache-control"] == "no-store"

            timeline = client.get(server.url + "api/timeline?after=4")
            assert timeline.status_code == 200
            assert timeline.json()["entries"][0]["agent"] == "Codex"
            assert observed_cursors == [4]

            wrong_host = client.get(server.url, headers={"Host": "attacker.example"})
            assert wrong_host.status_code == 421

            closed = client.post(server.url + "api/close", headers={"Origin": server.origin})
            assert closed.status_code == 200
        serving.join(timeout=5)
        assert not serving.is_alive()


def test_browser_page_never_interpolates_timeline_content() -> None:
    document = render_conversation_page("run_12345678", "test-nonce")

    assert "api/timeline" in document
    assert "textContent = entry.text" in document
    assert 'id="codexLogo"' in document
    assert 'id="claudeLogo"' in document
    assert "system-event" in document
    assert "createElementNS" in document
    assert 'id="count">0 events<' in document
    assert "innerHTML" not in document


def test_browser_viewer_closes_if_the_run_never_starts() -> None:
    def waiting(cursor: int) -> dict[str, object]:
        return {
            "state": "waiting_for_run",
            "terminal": False,
            "cursor": cursor,
            "at_limit": False,
            "entries": [],
        }

    with ConversationViewerServer(
        "run_12345678",
        waiting,
        startup_timeout_seconds=0.05,
    ) as server:
        serving = threading.Thread(target=server.serve_until_closed, daemon=True)
        serving.start()
        serving.join(timeout=2)
        assert not serving.is_alive()


def test_browser_viewer_startup_timeout_is_bounded() -> None:
    def waiting(_cursor: int) -> dict[str, object]:
        return {}

    for value in (-1, float("inf"), float("nan"), 3601, True):
        with pytest.raises(ConfigurationError, match="startup timeout"):
            ConversationViewerServer(
                "run_12345678",
                waiting,
                startup_timeout_seconds=value,
            )
