from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path

import pytest

from technocore_orchestrator.adapters.base import HarnessInvocation, McpGatewayConfig
from technocore_orchestrator.adapters.claude import ClaudeAdapter
from technocore_orchestrator.adapters.codex import CodexAdapter
from technocore_orchestrator.domain.collaboration import CollaborationPhase
from technocore_orchestrator.domain.models import Role, TerminationReason
from technocore_orchestrator.execution import ProcessResult, ProcessSpec, TrustedExecutable

_PLANNER_RESULT = {
    "summary": "Implement the accepted task.",
    "steps": [
        {
            "id": "implement",
            "description": "Make the bounded change.",
            "expected_paths": ["src/product.py"],
            "criterion_ids": ["criterion_1"],
        }
    ],
    "risks": [],
    "verification_suggestions": [],
    "challenge_dispositions": [],
    "blocked_reason": None,
}


class _Runner:
    def __init__(self, *, claude: bool) -> None:
        self.claude = claude
        self.spec: ProcessSpec | None = None

    async def run(self, spec: ProcessSpec) -> ProcessResult:
        self.spec = spec
        payload: object = _PLANNER_RESULT
        if self.claude:
            payload = {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "structured_output": _PLANNER_RESULT,
            }
        now = datetime.now(UTC)
        return ProcessResult(
            termination_reason=TerminationReason.EXITED,
            returncode=0,
            stdout=json.dumps(payload).encode(),
            stderr=b"",
            started_at=now,
            ended_at=now,
            duration_seconds=0,
        )


def _gateway() -> McpGatewayConfig:
    return McpGatewayConfig(
        server_name="technocore",
        url="http://127.0.0.1:43123/mcp-0123456789abcdef",
        tool_names=("read_new_messages", "publish_plan"),
    )


def _invocation(tmp_path: Path) -> HarnessInvocation:
    return HarnessInvocation(
        run_id="run_12345678",
        task_id="task",
        role=Role.PLANNER,
        phase=CollaborationPhase.PROPOSE_PLAN,
        attempt=1,
        worktree=tmp_path.resolve(),
        prompt="Return the plan.",
        timeout_seconds=30,
        output_limit_bytes=4096,
        gateway=_gateway(),
    )


def _schema_root() -> Path:
    return Path(str(resources.files("technocore_orchestrator.schemas.v1"))).resolve(strict=True)


def test_codex_receives_only_the_short_lived_gateway_configuration(tmp_path: Path) -> None:
    runner = _Runner(claude=False)
    adapter = CodexAdapter(
        executable=TrustedExecutable.capture(Path(sys.executable)),
        schema_root=_schema_root(),
        model="test-model",
        expected_version="1.2.3",
        environment=(),
        runner=runner,
    )

    asyncio.run(adapter.invoke(_invocation(tmp_path)))

    assert runner.spec is not None
    arguments = runner.spec.arguments
    assert "--ignore-user-config" in arguments
    assert 'mcp_servers.technocore.url="http://127.0.0.1:43123/mcp-0123456789abcdef"' in arguments
    assert 'mcp_servers.technocore.enabled_tools=["read_new_messages", "publish_plan"]' in arguments
    assert "mcp_servers.technocore.required=true" in arguments
    assert 'mcp_servers.technocore.default_tools_approval_mode="approve"' in arguments


def test_claude_loads_only_the_explicit_gateway_and_approved_tools(tmp_path: Path) -> None:
    runner = _Runner(claude=True)
    adapter = ClaudeAdapter(
        executable=TrustedExecutable.capture(Path(sys.executable)),
        schema_root=_schema_root(),
        model="test-model",
        expected_version="1.2.3",
        environment=(),
        runner=runner,
    )

    asyncio.run(adapter.invoke(_invocation(tmp_path)))

    assert runner.spec is not None
    arguments = runner.spec.arguments
    assert "--safe-mode" not in arguments
    assert "--strict-mcp-config" in arguments
    config = json.loads(arguments[arguments.index("--mcp-config") + 1])
    assert config == {
        "mcpServers": {
            "technocore": {
                "type": "http",
                "url": "http://127.0.0.1:43123/mcp-0123456789abcdef",
            }
        }
    }
    allowed = arguments[arguments.index("--allowedTools") + 1]
    assert "mcp__technocore__read_new_messages" in allowed
    assert "mcp__technocore__publish_plan" in allowed


@pytest.mark.parametrize(
    "url",
    (
        "https://127.0.0.1:43123/mcp-token",
        "http://localhost:43123/mcp-token",
        "http://192.168.1.20:43123/mcp-token",
        "http://127.0.0.1:43123/plain-path",
    ),
)
def test_gateway_configuration_rejects_non_local_or_non_random_endpoints(url: str) -> None:
    with pytest.raises(ValueError, match="MCP gateway URL"):
        McpGatewayConfig(server_name="technocore", url=url, tool_names=("read_new_messages",))
