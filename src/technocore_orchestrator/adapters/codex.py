"""OpenAI Codex CLI adapter for the pinned non-interactive contract."""

from __future__ import annotations

import json
from pathlib import Path

from technocore_orchestrator.adapters.base import (
    HarnessCapabilities,
    HarnessInvocation,
    HarnessInvocationResult,
)
from technocore_orchestrator.adapters.provider_common import (
    parse_direct_model_result,
    parse_probe_version,
    require_successful_process,
    schema_path_for_invocation,
    validate_adapter_inputs,
)
from technocore_orchestrator.domain.collaboration import CollaborationPhase
from technocore_orchestrator.execution import (
    ProcessExecutor,
    ProcessRunner,
    ProcessSpec,
    TrustedExecutable,
)


class CodexAdapter:
    def __init__(
        self,
        *,
        executable: TrustedExecutable,
        schema_root: Path,
        model: str,
        expected_version: str,
        environment: tuple[tuple[str, str], ...],
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
        require_successful_process(process, "Codex")
        version = parse_probe_version(
            process.stdout, prefix="codex-cli ", expected=self._expected_version
        )
        return HarnessCapabilities(
            name="codex", version=version, structured_output=True, resumable=False
        )

    async def invoke(self, invocation: HarnessInvocation) -> HarnessInvocationResult:
        schema_path = schema_path_for_invocation(
            self._schema_root, invocation.role, invocation.phase
        )
        sandbox = (
            "workspace-write"
            if invocation.phase in {CollaborationPhase.IMPLEMENT, CollaborationPhase.REVISE}
            else "read-only"
        )
        arguments = (
            "exec",
            "--strict-config",
            "--ignore-user-config",
            "--ephemeral",
            "--config",
            'windows.sandbox="elevated"',
            "--sandbox",
            sandbox,
            "--model",
            self._model,
            "--cd",
            str(invocation.worktree),
            "--output-schema",
            str(schema_path),
            "--color",
            "never",
        )
        if invocation.gateway is not None:
            gateway = invocation.gateway
            prefix = f"mcp_servers.{gateway.server_name}"
            arguments += (
                "--config",
                f"{prefix}.url={json.dumps(gateway.url)}",
                "--config",
                f"{prefix}.enabled_tools={json.dumps(gateway.tool_names)}",
                "--config",
                f"{prefix}.required=true",
                "--config",
                f'{prefix}.default_tools_approval_mode="approve"',
            )
        arguments += ("-",)
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
        require_successful_process(process, "Codex")
        return HarnessInvocationResult(
            role_result=parse_direct_model_result(
                invocation.role, invocation.phase, process.stdout
            ),
            process=process,
        )
