"""Deterministic external harness used for control-plane tests."""

from __future__ import annotations

import json
import os
import unicodedata
from enum import StrEnum
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

from technocore_orchestrator.adapters.base import (
    HarnessCapabilities,
    HarnessInvocation,
    HarnessInvocationResult,
)
from technocore_orchestrator.domain.collaboration import (
    CollaborationPhase,
    ModelResult,
    PlanChallengeResult,
)
from technocore_orchestrator.domain.models import (
    ImplementerResult,
    PlannerResult,
    ReviewerResult,
    Role,
)
from technocore_orchestrator.errors import ExecutionError, ProtocolError, RoleResultValidationError
from technocore_orchestrator.execution import (
    ProcessExecutor,
    ProcessRunner,
    ProcessSpec,
    TerminationReason,
    TrustedExecutable,
)

_FAKE_PROTOCOL_VERSION = "1"


class FakeScenario(StrEnum):
    SUCCESS = "success"
    REVISION_CYCLE = "revision_cycle"


class FakeHarnessAdapter:
    """Invoke the packaged fake through the same process boundary as real CLIs."""

    def __init__(
        self,
        *,
        python: TrustedExecutable,
        git: TrustedExecutable,
        scenario: FakeScenario = FakeScenario.SUCCESS,
        edit_path: str = "product.txt",
        runner: ProcessExecutor | None = None,
    ) -> None:
        self._python = python
        self._git = git
        self._scenario = scenario
        self._edit_path = _validate_edit_path(edit_path)
        self._runner = runner or ProcessRunner()

    async def probe(self) -> HarnessCapabilities:
        result = await self._runner.run(
            ProcessSpec(
                executable=self._python,
                arguments=("-I", "-m", "technocore_orchestrator.fake_harness", "--probe"),
                cwd=self._python.path.parent,
                environment=_fake_environment(self._git.path),
                timeout_seconds=10,
                output_limit_bytes=4_096,
            )
        )
        if not result.succeeded:
            raise ExecutionError("fake harness probe failed")
        try:
            payload = json.loads(result.stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProtocolError("fake harness probe returned invalid JSON") from exc
        expected = {
            "name": "technocore-orchestrator-fake-harness",
            "protocol": _FAKE_PROTOCOL_VERSION,
        }
        if payload != expected:
            raise ProtocolError("fake harness probe contract does not match")
        return HarnessCapabilities(
            name=expected["name"],
            version=expected["protocol"],
            structured_output=True,
            resumable=False,
        )

    async def invoke(self, invocation: HarnessInvocation) -> HarnessInvocationResult:
        payload = json.dumps(
            {
                "v": 1,
                "run_id": invocation.run_id,
                "task_id": invocation.task_id,
                "role": invocation.role,
                "phase": invocation.phase,
                "attempt": invocation.attempt,
                "prompt": invocation.prompt,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        arguments = (
            "-I",
            "-m",
            "technocore_orchestrator.fake_harness",
            "--role",
            invocation.role.value,
            "--phase",
            invocation.phase.value,
            "--scenario",
            self._scenario.value,
            "--git",
            str(self._git.path),
            "--edit-path",
            self._edit_path,
        )
        process = await self._runner.run(
            ProcessSpec(
                executable=self._python,
                arguments=arguments,
                cwd=invocation.worktree,
                environment=_fake_environment(self._git.path),
                stdin=payload,
                timeout_seconds=invocation.timeout_seconds,
                output_limit_bytes=invocation.output_limit_bytes,
                termination_grace_seconds=1,
            )
        )
        if process.termination_reason is not TerminationReason.EXITED:
            raise ExecutionError(
                "fake harness did not exit normally",
                context={"termination": process.termination_reason},
            )
        if process.returncode != 0:
            raise ExecutionError(
                "fake harness exited unsuccessfully", context={"returncode": process.returncode}
            )
        return HarnessInvocationResult(
            role_result=_parse_model_result(invocation.role, invocation.phase, process.stdout),
            process=process,
        )


def _parse_model_result(role: Role, phase: CollaborationPhase, raw: bytes) -> ModelResult:
    model: (
        type[PlannerResult]
        | type[PlanChallengeResult]
        | type[ImplementerResult]
        | type[ReviewerResult]
    )
    if phase is CollaborationPhase.CHALLENGE_PLAN:
        if role is not Role.IMPLEMENTER:
            raise ProtocolError("fake plan challenge requires the implementer role")
        model = PlanChallengeResult
    elif role is Role.PLANNER:
        model = PlannerResult
    elif role is Role.IMPLEMENTER:
        model = ImplementerResult
    elif role is Role.REVIEWER:
        model = ReviewerResult
    else:
        raise ProtocolError("fake harness returned a result for an unsupported role")
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise RoleResultValidationError(
            "fake harness result failed its closed role schema",
            context={"validation_errors": exc.error_count()},
        ) from exc


def _validate_edit_path(value: str) -> str:
    if (
        not value
        or "\\" in value
        or ":" in value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("fake edit path must be a canonical repository-relative POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("fake edit path must not escape its worktree")
    return path.as_posix()


def _fake_environment(git: Path) -> tuple[tuple[str, str], ...]:
    path_entries = [str(git.parent)]
    values = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "LANG": "C",
        "LC_ALL": "C",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
    }
    system_root = os.environ.get("SYSTEMROOT")
    if not system_root:
        raise ExecutionError("SystemRoot is required for the fake harness on Windows")
    values["SYSTEMROOT"] = system_root
    path_entries.append(str(Path(system_root) / "System32"))
    values["PATH"] = os.pathsep.join(dict.fromkeys(path_entries))
    return tuple(sorted(values.items()))
