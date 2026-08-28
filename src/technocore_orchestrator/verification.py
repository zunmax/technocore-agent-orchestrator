"""Deterministic verification over supervisor-approved command arrays."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from technocore_orchestrator.execution import (
    ProcessRunner,
    ProcessSpec,
    TerminationReason,
    TrustedExecutable,
)


@dataclass(frozen=True, slots=True)
class VerificationJob:
    id: str
    executable: TrustedExecutable
    arguments: tuple[str, ...]
    environment: tuple[tuple[str, str], ...]
    timeout_seconds: float
    output_limit_bytes: int
    required: bool = True

    def __post_init__(self) -> None:
        if not self.id or any(character in self.id for character in "\x00\r\n"):
            raise ValueError("verification job id must be non-empty single-line text")


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    required: bool
    passed: bool
    termination_reason: TerminationReason
    returncode: int
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True, slots=True)
class VerificationSuiteResult:
    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed or not check.required for check in self.checks)


class Verifier:
    def __init__(self, runner: ProcessRunner | None = None) -> None:
        self._runner = runner or ProcessRunner()

    async def run(
        self, *, worktree: Path, jobs: tuple[VerificationJob, ...]
    ) -> VerificationSuiteResult:
        if not jobs:
            raise ValueError("verification requires at least one configured job")
        if len({job.id for job in jobs}) != len(jobs):
            raise ValueError("verification job ids must be unique")
        results: list[CheckResult] = []
        for job in jobs:
            process = await self._runner.run(
                ProcessSpec(
                    executable=job.executable,
                    arguments=job.arguments,
                    cwd=worktree,
                    environment=job.environment,
                    timeout_seconds=job.timeout_seconds,
                    output_limit_bytes=job.output_limit_bytes,
                )
            )
            passed = (
                process.termination_reason is TerminationReason.EXITED and process.returncode == 0
            )
            results.append(
                CheckResult(
                    id=job.id,
                    required=job.required,
                    passed=passed,
                    termination_reason=process.termination_reason,
                    returncode=process.returncode,
                    started_at=process.started_at,
                    ended_at=process.ended_at,
                    duration_seconds=process.duration_seconds,
                    stdout_sha256=hashlib.sha256(process.stdout).hexdigest(),
                    stderr_sha256=hashlib.sha256(process.stderr).hexdigest(),
                )
            )
        return VerificationSuiteResult(checks=tuple(results))
