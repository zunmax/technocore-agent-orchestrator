"""Short-lived loopback MCP gateway with phase-scoped collaboration tools."""

from __future__ import annotations

import asyncio
import secrets
import socket
from types import TracebackType
from typing import Annotated
from uuid import UUID

import uvicorn
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field

from technocore_orchestrator.adapters.base import McpGatewayConfig
from technocore_orchestrator.domain.collaboration import (
    CandidateSubmission,
    CollaborationBackend,
    CollaborationKind,
    CollaborationPhase,
    FindingsResolutionResult,
    PlanChallengeResult,
)
from technocore_orchestrator.domain.models import PlannerResult, ReviewerResult, Role
from technocore_orchestrator.errors import PreflightError, ProtocolError

_SERVER_NAME = "technocore"
_STARTUP_SECONDS = 5
_SHUTDOWN_SECONDS = 5


class RoleGateway:
    """Own one random-path MCP endpoint for exactly one provider invocation."""

    def __init__(
        self,
        *,
        role: Role,
        phase: CollaborationPhase,
        backend: CollaborationBackend,
    ) -> None:
        self._called_tools: list[str] = []
        self._mcp, self._tool_names = _build_server(role, phase, backend, self._called_tools)
        self._listener: socket.socket | None = None
        self._server: uvicorn.Server | None = None
        self._task: asyncio.Task[None] | None = None
        self._path = "/mcp-" + secrets.token_hex(24)
        self._url: str | None = None

    @property
    def server_name(self) -> str:
        return _SERVER_NAME

    @property
    def tool_names(self) -> tuple[str, ...]:
        return self._tool_names

    @property
    def called_tools(self) -> tuple[str, ...]:
        return tuple(self._called_tools)

    @property
    def url(self) -> str:
        if self._url is None:
            raise RuntimeError("role gateway has not started")
        return self._url

    @property
    def config(self) -> McpGatewayConfig:
        return McpGatewayConfig(
            server_name=self.server_name,
            url=self.url,
            tool_names=self.tool_names,
        )

    async def __aenter__(self) -> RoleGateway:
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(socket.SOMAXCONN)
        port = listener.getsockname()[1]
        app = self._mcp.streamable_http_app(
            streamable_http_path=self._path,
            json_response=True,
            stateless_http=True,
            max_request_body_size=256 * 1024,
        )
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            access_log=False,
            log_config=None,
            server_header=False,
            date_header=False,
            lifespan="on",
        )
        server = uvicorn.Server(config)
        task = asyncio.create_task(
            server.serve(sockets=[listener]), name="technocore-orchestrator-role-gateway"
        )
        self._listener = listener
        self._server = server
        self._task = task
        self._url = f"http://127.0.0.1:{port}{self._path}"
        try:
            async with asyncio.timeout(_STARTUP_SECONDS):
                while not server.started:
                    if task.done():
                        await task
                        raise PreflightError("role gateway stopped during startup")
                    await asyncio.sleep(0.01)
        except Exception:
            await self._close()
            raise
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        await self._close()

    async def _close(self) -> None:
        server, task = self._server, self._task
        self._server = None
        self._task = None
        self._url = None
        if server is not None:
            server.should_exit = True
        if task is not None:
            try:
                async with asyncio.timeout(_SHUTDOWN_SECONDS):
                    await task
            except TimeoutError:
                if server is not None:
                    server.force_exit = True
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._listener is not None:
            self._listener.close()
            self._listener = None


def _build_server(
    role: Role,
    phase: CollaborationPhase,
    backend: CollaborationBackend,
    called_tools: list[str],
) -> tuple[MCPServer[None], tuple[str, ...]]:
    _validate_phase_role(role, phase)
    server: MCPServer[None] = MCPServer(
        name=_SERVER_NAME,
        title="Technocore Agent Orchestrator Conversation",
        instructions=(
            "Read the shared engineering conversation before acting. Use only the phase-scoped "
            "tools exposed by this server. Never request or reveal room capabilities or keys."
        ),
        log_level="ERROR",
    )
    tools = ["read_new_messages"]
    mutation_lock = asyncio.Lock()
    acknowledged_event_id: UUID | None = None
    resolution_event_id: UUID | None = None

    def require_acknowledged_event() -> UUID:
        if acknowledged_event_id is None:
            raise ProtocolError("publish requires one successful exact-handoff acknowledgement")
        return acknowledged_event_id

    def require_not_called(tool_name: str) -> None:
        if tool_name in called_tools:
            raise ProtocolError(f"{tool_name} may be called only once in this phase")

    @server.tool(
        annotations=ToolAnnotations(read_only_hint=True, open_world_hint=False),
        structured_output=True,
    )
    async def read_new_messages(
        after_sequence: Annotated[
            int,
            Field(ge=0, description="Last Technocore sequence already processed; use zero first."),
        ] = 0,
    ) -> dict[str, object]:
        """Read verified shared-room messages after a sequence cursor."""

        window = await backend.read_new_messages(role, after_sequence)
        called_tools.append("read_new_messages")
        return window.model_dump(mode="json")

    if phase is not CollaborationPhase.PROPOSE_PLAN:
        tools.append("acknowledge_handoff")

        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=True,
                open_world_hint=False,
            ),
            structured_output=True,
        )
        async def acknowledge_handoff(
            event_id: UUID,
            sequence: Annotated[int, Field(ge=1)],
            payload_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")],
        ) -> dict[str, object]:
            """Acknowledge the exact signed message and payload digest being acted on."""

            nonlocal acknowledged_event_id
            async with mutation_lock:
                require_not_called("acknowledge_handoff")
                result = await backend.acknowledge(
                    role=role,
                    event_id=event_id,
                    sequence=sequence,
                    payload_sha256=payload_sha256,
                )
                acknowledged_event_id = event_id
                called_tools.append("acknowledge_handoff")
                return result.model_dump(mode="json")

    if phase in {CollaborationPhase.PROPOSE_PLAN, CollaborationPhase.FINALIZE_PLAN}:
        tools.append("publish_plan")
        kind = (
            CollaborationKind.PLAN_PROPOSED
            if phase is CollaborationPhase.PROPOSE_PLAN
            else CollaborationKind.PLAN_FINALIZED
        )

        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
            structured_output=True,
        )
        async def publish_plan(plan: PlannerResult) -> dict[str, object]:
            """Publish the proposed or finalized engineering plan to the shared room."""

            async with mutation_lock:
                require_not_called("publish_plan")
                reply_to = (
                    None
                    if phase is CollaborationPhase.PROPOSE_PLAN
                    else require_acknowledged_event()
                )
                result = await backend.publish(
                    role=role,
                    kind=kind,
                    payload=plan,
                    reply_to=reply_to,
                )
                called_tools.append("publish_plan")
                return result.model_dump(mode="json")

    if phase is CollaborationPhase.CHALLENGE_PLAN:
        tools.append("publish_plan_challenge")

        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
            structured_output=True,
        )
        async def publish_plan_challenge(
            challenge: PlanChallengeResult,
        ) -> dict[str, object]:
            """Publish an independent challenge or approval of the proposed plan."""

            async with mutation_lock:
                require_not_called("publish_plan_challenge")
                result = await backend.publish(
                    role=role,
                    kind=CollaborationKind.PLAN_CHALLENGED,
                    payload=challenge,
                    reply_to=require_acknowledged_event(),
                )
                called_tools.append("publish_plan_challenge")
                return result.model_dump(mode="json")

    if phase in {CollaborationPhase.IMPLEMENT, CollaborationPhase.REVISE}:
        tools.append("publish_candidate")

        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
            structured_output=True,
        )
        async def publish_candidate(candidate: CandidateSubmission) -> dict[str, object]:
            """Submit one current-diff result whose evidence exactly covers its declared paths."""

            async with mutation_lock:
                require_not_called("publish_candidate")
                reply_to = (
                    resolution_event_id
                    if phase is CollaborationPhase.REVISE
                    else require_acknowledged_event()
                )
                if reply_to is None:
                    raise ProtocolError("revised candidate requires a findings resolution first")
                result = await backend.publish(
                    role=role,
                    kind=CollaborationKind.IMPLEMENTATION_SUBMITTED,
                    payload=candidate,
                    reply_to=reply_to,
                )
                called_tools.append("publish_candidate")
                return result.model_dump(mode="json")

    if phase is CollaborationPhase.REVISE:
        tools.append("resolve_review_findings")

        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
            structured_output=True,
        )
        async def resolve_review_findings(
            resolution: FindingsResolutionResult,
        ) -> dict[str, object]:
            """Resolve every review finding before submitting a revised candidate."""

            nonlocal resolution_event_id
            async with mutation_lock:
                require_not_called("resolve_review_findings")
                result = await backend.publish(
                    role=role,
                    kind=CollaborationKind.FINDINGS_RESOLVED,
                    payload=resolution,
                    reply_to=require_acknowledged_event(),
                )
                resolution_event_id = result.event_id
                called_tools.append("resolve_review_findings")
                return result.model_dump(mode="json")

    if phase is CollaborationPhase.REVIEW:
        tools.append("publish_review")

        @server.tool(
            annotations=ToolAnnotations(
                read_only_hint=False,
                destructive_hint=False,
                idempotent_hint=False,
                open_world_hint=False,
            ),
            structured_output=True,
        )
        async def publish_review(review: ReviewerResult) -> dict[str, object]:
            """Publish structured review findings for the acknowledged candidate."""

            async with mutation_lock:
                require_not_called("publish_review")
                result = await backend.publish(
                    role=role,
                    kind=CollaborationKind.REVIEW_SUBMITTED,
                    payload=review,
                    reply_to=require_acknowledged_event(),
                )
                called_tools.append("publish_review")
                return result.model_dump(mode="json")

    return server, tuple(tools)


def _validate_phase_role(role: Role, phase: CollaborationPhase) -> None:
    expected = {
        CollaborationPhase.PROPOSE_PLAN: Role.PLANNER,
        CollaborationPhase.CHALLENGE_PLAN: Role.IMPLEMENTER,
        CollaborationPhase.FINALIZE_PLAN: Role.PLANNER,
        CollaborationPhase.IMPLEMENT: Role.IMPLEMENTER,
        CollaborationPhase.REVISE: Role.IMPLEMENTER,
        CollaborationPhase.REVIEW: Role.REVIEWER,
    }[phase]
    if role is not expected:
        raise ProtocolError(
            "collaboration phase is assigned to another role",
            context={"phase": phase, "role": role, "expected": expected},
        )
