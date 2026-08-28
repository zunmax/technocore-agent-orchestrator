from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from mcp import Client

from technocore_orchestrator.domain.collaboration import (
    CollaborationKind,
    CollaborationPayload,
    CollaborationPhase,
    ConversationWindow,
    PublicationResult,
    collaboration_payload_sha256,
)
from technocore_orchestrator.domain.models import Role
from technocore_orchestrator.errors import ProtocolError
from technocore_orchestrator.gateway import RoleGateway
from technocore_orchestrator.network import require_loopback_technocore_listener


class _Backend:
    def __init__(self) -> None:
        self.replies: list[UUID | None] = []

    async def read_new_messages(self, role: Role, after_sequence: int) -> ConversationWindow:
        del role
        return ConversationWindow(
            cursor_before=after_sequence,
            cursor_after=after_sequence,
            gap_detected=False,
            messages=(),
        )

    async def publish(
        self,
        *,
        role: Role,
        kind: CollaborationKind,
        payload: CollaborationPayload,
        reply_to: UUID | None,
    ) -> PublicationResult:
        del role, kind
        self.replies.append(reply_to)
        return PublicationResult(
            sequence=1,
            event_id=uuid4(),
            payload_sha256=collaboration_payload_sha256(payload),
        )

    async def acknowledge(
        self,
        *,
        role: Role,
        event_id: UUID,
        sequence: int,
        payload_sha256: str,
    ) -> PublicationResult:
        del role, event_id
        return PublicationResult(
            sequence=sequence + 1,
            event_id=uuid4(),
            payload_sha256=payload_sha256,
        )


class _SlowBackend(_Backend):
    async def publish(
        self,
        *,
        role: Role,
        kind: CollaborationKind,
        payload: CollaborationPayload,
        reply_to: UUID | None,
    ) -> PublicationResult:
        await asyncio.sleep(0.05)
        return await super().publish(
            role=role,
            kind=kind,
            payload=payload,
            reply_to=reply_to,
        )


@pytest.mark.parametrize(
    ("role", "phase", "expected"),
    (
        (Role.PLANNER, CollaborationPhase.PROPOSE_PLAN, {"read_new_messages", "publish_plan"}),
        (
            Role.IMPLEMENTER,
            CollaborationPhase.CHALLENGE_PLAN,
            {"read_new_messages", "acknowledge_handoff", "publish_plan_challenge"},
        ),
        (
            Role.PLANNER,
            CollaborationPhase.FINALIZE_PLAN,
            {"read_new_messages", "acknowledge_handoff", "publish_plan"},
        ),
        (
            Role.IMPLEMENTER,
            CollaborationPhase.IMPLEMENT,
            {"read_new_messages", "acknowledge_handoff", "publish_candidate"},
        ),
        (
            Role.IMPLEMENTER,
            CollaborationPhase.REVISE,
            {
                "read_new_messages",
                "acknowledge_handoff",
                "publish_candidate",
                "resolve_review_findings",
            },
        ),
        (
            Role.REVIEWER,
            CollaborationPhase.REVIEW,
            {"read_new_messages", "acknowledge_handoff", "publish_review"},
        ),
    ),
)
def test_gateway_exposes_only_phase_scoped_tools(
    role: Role, phase: CollaborationPhase, expected: set[str]
) -> None:
    async def exercise() -> None:
        async with RoleGateway(role=role, phase=phase, backend=_Backend()) as gateway:
            require_loopback_technocore_listener(gateway.url)
            assert gateway.url.startswith("http://127.0.0.1:")
            assert "/mcp-" in gateway.url
            assert set(gateway.tool_names) == expected
            async with Client(gateway.url) as client:
                tools = await client.list_tools()
                assert {tool.name for tool in tools.tools} == expected

    asyncio.run(exercise())


def test_gateway_read_tool_returns_a_bounded_cursor_window() -> None:
    async def exercise() -> None:
        async with (
            RoleGateway(
                role=Role.PLANNER,
                phase=CollaborationPhase.PROPOSE_PLAN,
                backend=_Backend(),
            ) as gateway,
            Client(gateway.url) as client,
        ):
            result = await client.call_tool("read_new_messages", {"after_sequence": 7})
            assert result.is_error is False
            assert result.structured_content == {
                "cursor_before": 7,
                "cursor_after": 7,
                "gap_detected": False,
                "messages": [],
            }

    asyncio.run(exercise())


def test_gateway_serializes_concurrent_state_changing_calls() -> None:
    plan = {
        "summary": "Build the requested product.",
        "steps": [
            {
                "id": "build",
                "description": "Implement and verify the product.",
                "expected_paths": ["index.html"],
                "criterion_ids": ["criterion_1"],
            }
        ],
        "risks": [],
        "verification_suggestions": ["Run the configured checks."],
        "challenge_dispositions": [],
        "blocked_reason": None,
    }

    async def exercise() -> None:
        backend = _SlowBackend()
        async with (
            RoleGateway(
                role=Role.PLANNER,
                phase=CollaborationPhase.PROPOSE_PLAN,
                backend=backend,
            ) as gateway,
            Client(gateway.url) as first,
            Client(gateway.url) as second,
        ):
            results = await asyncio.gather(
                first.call_tool("publish_plan", {"plan": plan}),
                second.call_tool("publish_plan", {"plan": plan}),
            )
            assert sum(not result.is_error for result in results) == 1
            assert sum(result.is_error for result in results) == 1
            assert len(backend.replies) == 1

    asyncio.run(exercise())


def test_gateway_rejects_a_phase_assigned_to_another_role() -> None:
    with pytest.raises(ProtocolError, match="assigned to another role"):
        RoleGateway(
            role=Role.REVIEWER,
            phase=CollaborationPhase.IMPLEMENT,
            backend=_Backend(),
        )


def test_gateway_binds_a_publish_to_the_acknowledged_event() -> None:
    async def exercise() -> None:
        backend = _Backend()
        target = uuid4()
        async with (
            RoleGateway(
                role=Role.IMPLEMENTER,
                phase=CollaborationPhase.CHALLENGE_PLAN,
                backend=backend,
            ) as gateway,
            Client(gateway.url) as client,
        ):
            acknowledged = await client.call_tool(
                "acknowledge_handoff",
                {
                    "event_id": str(target),
                    "sequence": 4,
                    "payload_sha256": "a" * 64,
                },
            )
            assert acknowledged.is_error is False
            published = await client.call_tool(
                "publish_plan_challenge",
                {
                    "challenge": {
                        "decision": "approved",
                        "summary": "The bounded plan is complete.",
                        "issues": [],
                    }
                },
            )
            assert published.is_error is False
            assert backend.replies == [target]

    asyncio.run(exercise())


def _revision_candidate(*, include_inherited_paths: bool) -> dict[str, object]:
    inherited = ["index.html", "styles.css", "script.js"]
    current = ["script.js"]
    return {
        "outcome": "changes_ready",
        "summary": "Validated malformed dates before calculating the age.",
        "candidate_commit": None,
        "declared_changed_paths": current,
        "focused_checks": [{"id": "git_diff_check", "stated_outcome": "passed"}],
        "criterion_evidence": [
            {
                "criterion_id": "criterion_1",
                "changed_paths": inherited if include_inherited_paths else current,
                "verification_command_ids": ["git_diff_check"],
                "evidence": ["The revised validation preserves calculator behavior."],
            },
            {
                "criterion_id": "criterion_2",
                "changed_paths": inherited if include_inherited_paths else [],
                "verification_command_ids": ["git_diff_check"],
                "evidence": ["No current revision file is needed for this criterion."],
            },
            {
                "criterion_id": "criterion_3",
                "changed_paths": current,
                "verification_command_ids": ["git_diff_check"],
                "evidence": ["script.js now rejects malformed calendar components."],
            },
        ],
        "remaining_concerns": [],
        "blocked_reason": None,
    }


def test_revision_gateway_rejects_inconsistent_paths_before_publish_and_allows_retry() -> None:
    async def exercise() -> None:
        backend = _Backend()
        target = uuid4()
        async with (
            RoleGateway(
                role=Role.IMPLEMENTER,
                phase=CollaborationPhase.REVISE,
                backend=backend,
            ) as gateway,
            Client(gateway.url) as client,
        ):
            acknowledged = await client.call_tool(
                "acknowledge_handoff",
                {
                    "event_id": str(target),
                    "sequence": 17,
                    "payload_sha256": "b" * 64,
                },
            )
            assert acknowledged.is_error is False
            resolved = await client.call_tool(
                "resolve_review_findings",
                {
                    "resolution": {
                        "summary": "Resolved the malformed-date finding.",
                        "resolutions": [
                            {
                                "finding_id": "finding_1",
                                "status": "fixed",
                                "evidence": ["script.js validates calendar components."],
                            }
                        ],
                    }
                },
            )
            assert resolved.is_error is False
            assert len(backend.replies) == 1

            rejected = await client.call_tool(
                "publish_candidate",
                {"candidate": _revision_candidate(include_inherited_paths=True)},
            )
            assert rejected.is_error is True
            assert "must exactly match" in str(rejected.content)
            assert len(backend.replies) == 1
            assert "publish_candidate" not in gateway.called_tools

            published = await client.call_tool(
                "publish_candidate",
                {"candidate": _revision_candidate(include_inherited_paths=False)},
            )
            assert published.is_error is False
            assert len(backend.replies) == 2
            assert gateway.called_tools.count("publish_candidate") == 1

    asyncio.run(exercise())
