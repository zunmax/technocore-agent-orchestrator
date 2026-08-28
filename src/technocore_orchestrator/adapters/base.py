"""Provider-neutral harness contracts."""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from technocore_orchestrator.domain.collaboration import CollaborationPhase, ModelResult
from technocore_orchestrator.domain.models import (
    RUN_ID_RE,
    TASK_ID_RE,
    Role,
)
from technocore_orchestrator.domain.usage import ProviderUsage
from technocore_orchestrator.execution import ProcessResult

_HARNESS_ROLES = frozenset({Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER})
_MAX_PROMPT_BYTES = 1024 * 1024
_MCP_NAME_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")


@dataclass(frozen=True, slots=True)
class McpGatewayConfig:
    server_name: str
    url: str
    tool_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not _MCP_NAME_RE.fullmatch(self.server_name):
            raise ValueError("MCP server name is invalid")
        if not self.tool_names or len(set(self.tool_names)) != len(self.tool_names):
            raise ValueError("MCP gateway tools must be non-empty and unique")
        if any(not _MCP_NAME_RE.fullmatch(name) for name in self.tool_names):
            raise ValueError("MCP gateway tool name is invalid")
        parsed = urlsplit(self.url)
        if (
            parsed.scheme != "http"
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or not parsed.hostname
            or not parsed.port
            or not parsed.path.startswith("/mcp-")
        ):
            raise ValueError("MCP gateway URL must be a random-path loopback HTTP endpoint")
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ValueError("MCP gateway URL must use a literal loopback IP address")


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    name: str
    version: str
    structured_output: bool
    resumable: bool


@dataclass(frozen=True, slots=True)
class HarnessInvocation:
    run_id: str
    task_id: str
    role: Role
    phase: CollaborationPhase
    attempt: int
    worktree: Path
    prompt: str
    timeout_seconds: float
    output_limit_bytes: int
    gateway: McpGatewayConfig | None = None

    def __post_init__(self) -> None:
        if not RUN_ID_RE.fullmatch(self.run_id) or not TASK_ID_RE.fullmatch(self.task_id):
            raise ValueError("harness invocation has an invalid run or task identifier")
        if self.role not in _HARNESS_ROLES:
            raise ValueError("harness invocation role is not model-driven")
        if isinstance(self.attempt, bool) or not 1 <= self.attempt <= 100:
            raise ValueError("harness invocation attempt must be between 1 and 100")
        if not self.worktree.is_absolute():
            raise ValueError("harness invocation worktree must be absolute")
        if not isinstance(self.prompt, str) or not self.prompt:
            raise ValueError("harness invocation prompt must be non-empty text")
        if len(self.prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
            raise ValueError("harness invocation prompt exceeds the 1 MiB limit")
        if (
            isinstance(self.timeout_seconds, bool)
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 7_200
        ):
            raise ValueError("harness invocation timeout must be finite and at most two hours")
        if (
            isinstance(self.output_limit_bytes, bool)
            or not 1 <= self.output_limit_bytes <= 10 * 1024 * 1024
        ):
            raise ValueError("harness invocation output limit must be between 1 byte and 10 MiB")


@dataclass(frozen=True, slots=True)
class HarnessInvocationResult:
    role_result: ModelResult
    process: ProcessResult
    usage: ProviderUsage | None = None


class HarnessAdapter(Protocol):
    async def probe(self) -> HarnessCapabilities: ...

    async def invoke(self, invocation: HarnessInvocation) -> HarnessInvocationResult: ...
