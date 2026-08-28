"""Anthropic Claude Code CLI adapter for the pinned print-mode contract."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from technocore_orchestrator.adapters.base import (
    HarnessCapabilities,
    HarnessInvocation,
    HarnessInvocationResult,
)
from technocore_orchestrator.adapters.provider_common import (
    compact_schema,
    parse_probe_version,
    parse_result_wrapper,
    parse_wrapped_model_result_document,
    require_successful_process,
    schema_path_for_invocation,
    validate_adapter_inputs,
)
from technocore_orchestrator.domain.collaboration import CollaborationPhase
from technocore_orchestrator.domain.models import Role
from technocore_orchestrator.domain.usage import ProviderUsage
from technocore_orchestrator.errors import RoleResultValidationError
from technocore_orchestrator.execution import (
    ProcessExecutor,
    ProcessRunner,
    ProcessSpec,
    TrustedExecutable,
)


class ClaudeAdapter:
    def __init__(
        self,
        *,
        executable: TrustedExecutable,
        schema_root: Path,
        model: str,
        expected_version: str,
        environment: tuple[tuple[str, str], ...],
        max_turns: int = 20,
        runner: ProcessExecutor | None = None,
    ) -> None:
        validate_adapter_inputs(
            model=model, expected_version=expected_version, environment=environment
        )
        self._executable = executable
        self._schema_root = schema_root.resolve(strict=True)
        self._model = model
        self._expected_version = expected_version
        self._environment = environment
        if (
            isinstance(max_turns, bool)
            or not isinstance(max_turns, int)
            or not 1 <= max_turns <= 100
        ):
            raise ValueError("Claude max_turns must be between 1 and 100")
        self._max_turns = max_turns
        self._runner = runner or ProcessRunner()

    async def probe(self) -> HarnessCapabilities:
        process = await self._runner.run(
            ProcessSpec(
                executable=self._executable,
                arguments=("--version",),
                cwd=self._executable.path.parent,
                environment=self._environment,
                timeout_seconds=10,
                output_limit_bytes=4_096,
            )
        )
        require_successful_process(process, "Claude Code")
        version = parse_probe_version(process.stdout, prefix="", expected=self._expected_version)
        return HarnessCapabilities(
            name="claude", version=version, structured_output=True, resumable=False
        )

    async def invoke(self, invocation: HarnessInvocation) -> HarnessInvocationResult:
        schema = compact_schema(
            schema_path_for_invocation(self._schema_root, invocation.role, invocation.phase)
        )
        permission_mode, tools, allowed_tools = _role_permissions(invocation.role, invocation.phase)
        mcp_config = {"mcpServers": {}}
        if invocation.gateway is not None:
            gateway = invocation.gateway
            mcp_config["mcpServers"][gateway.server_name] = {
                "type": "http",
                "url": gateway.url,
            }
            mcp_tools = ",".join(
                f"mcp__{gateway.server_name}__{name}" for name in gateway.tool_names
            )
            allowed_tools = ",".join(filter(None, (allowed_tools, mcp_tools)))
        arguments = (
            "--print",
            "--output-format",
            "json",
            "--input-format",
            "text",
            "--json-schema",
            schema,
            "--model",
            self._model,
            "--max-turns",
            str(self._max_turns),
            "--no-session-persistence",
            "--disable-slash-commands",
            "--strict-mcp-config",
            "--mcp-config",
            json.dumps(mcp_config, separators=(",", ":")),
            "--no-chrome",
            "--setting-sources",
            "",
            "--permission-mode",
            permission_mode,
            "--tools",
            tools,
        )
        if allowed_tools:
            arguments += ("--allowedTools", allowed_tools)
        process = await self._runner.run(
            ProcessSpec(
                executable=self._executable,
                arguments=arguments,
                cwd=invocation.worktree,
                environment=self._environment,
                stdin=invocation.prompt.encode("utf-8"),
                timeout_seconds=invocation.timeout_seconds,
                output_limit_bytes=invocation.output_limit_bytes,
            )
        )
        require_successful_process(process, "Claude Code")
        wrapper = parse_result_wrapper(process.stdout)
        return HarnessInvocationResult(
            role_result=parse_wrapped_model_result_document(
                invocation.role, invocation.phase, wrapper
            ),
            process=process,
            usage=_parse_usage(wrapper),
        )


def _role_permissions(role: Role, phase: CollaborationPhase) -> tuple[str, str, str]:
    if phase in {CollaborationPhase.IMPLEMENT, CollaborationPhase.REVISE}:
        if role is not Role.IMPLEMENTER:
            raise ValueError("writable collaboration phases require the implementer role")
        return "acceptEdits", "Read,Glob,Grep,Edit,Write", "Edit,Write"
    if role in {Role.PLANNER, Role.REVIEWER} or (
        role is Role.IMPLEMENTER and phase is CollaborationPhase.CHALLENGE_PLAN
    ):
        return (
            "dontAsk",
            "Read,Glob,Grep,Bash",
            "Read,Glob,Grep,Bash(git diff:*),Bash(git show:*),Bash(git status:*),Bash(git log:*)",
        )
    raise ValueError("Claude role is not model-driven")


def _parse_usage(wrapper: dict[str, Any]) -> ProviderUsage | None:
    raw_usage = wrapper.get("usage")
    if raw_usage is not None and not isinstance(raw_usage, dict):
        raise RoleResultValidationError("Claude usage must be an object when present")
    usage = raw_usage or {}
    input_tokens = _optional_count(usage, "input_tokens")
    output_tokens = _optional_count(usage, "output_tokens")
    cache_read_input_tokens = _optional_count(usage, "cache_read_input_tokens")
    cache_creation_input_tokens = _optional_count(usage, "cache_creation_input_tokens")
    turns = _optional_count(wrapper, "num_turns")
    facts = (
        input_tokens,
        output_tokens,
        cache_read_input_tokens,
        cache_creation_input_tokens,
        turns,
    )
    if all(value is None for value in facts):
        return None
    try:
        return ProviderUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_input_tokens=cache_read_input_tokens,
            cache_creation_input_tokens=cache_creation_input_tokens,
            turns=turns,
        )
    except ValueError as exc:
        raise RoleResultValidationError("Claude usage exceeds the supported bounds") from exc


def _optional_count(document: dict[str, Any], key: str) -> int | None:
    value = document.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RoleResultValidationError(f"Claude {key} must be a non-negative integer")
    return value
