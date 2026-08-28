"""Bounded, shell-free child process execution with process-tree ownership."""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import signal
import subprocess
import sys
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from technocore_orchestrator.domain.models import TerminationReason
from technocore_orchestrator.errors import ExecutionError, PreflightError
from technocore_orchestrator.windows_job import WindowsJob

_ENVIRONMENT_KEY_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_READ_CHUNK_BYTES = 64 * 1024
_PROCESS_HOST_MODULE = "technocore_orchestrator.process_host"
_PROCESS_HOST_PROTOCOL_VERSION = 1
MAX_STDIN_BYTES = 4 * 1024 * 1024
MAX_WALL_SECONDS = 86_400.0
MAX_OUTPUT_BYTES = 10 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class TrustedExecutable:
    """Content identity captured at probe time and rechecked before execution."""

    path: Path
    resolved_path: Path
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if not self.path.is_absolute():
            raise ValueError("trusted executable path must be absolute")
        if not self.resolved_path.is_absolute():
            raise ValueError("trusted executable resolved path must be absolute")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise ValueError("trusted executable sha256 must be lowercase hexadecimal")
        if isinstance(self.size_bytes, bool) or self.size_bytes < 1:
            raise ValueError("trusted executable size must be a positive integer")

    @classmethod
    def capture(cls, path: Path) -> TrustedExecutable:
        """Resolve and fingerprint one regular executable without invoking it."""

        try:
            invocation_path = path.absolute()
            resolved = invocation_path.resolve(strict=True)
            stat = resolved.stat()
            if not resolved.is_file():
                raise PreflightError("executable path is not a regular file")
            digest = _sha256_file(resolved)
        except PreflightError:
            raise
        except OSError as exc:
            raise PreflightError(
                "unable to inspect executable", context={"reason": str(exc)}
            ) from exc
        return cls(
            path=invocation_path,
            resolved_path=resolved,
            sha256=digest,
            size_bytes=stat.st_size,
        )

    def assert_unchanged(self) -> None:
        """Fail closed when the probed executable was replaced or modified."""

        try:
            resolved = self.path.resolve(strict=True)
            stat = resolved.stat()
            if resolved != self.resolved_path:
                raise PreflightError("trusted executable target changed after it was probed")
            if not resolved.is_file():
                raise PreflightError("trusted executable is no longer a regular file")
            digest = _sha256_file(resolved)
        except PreflightError:
            raise
        except OSError as exc:
            raise PreflightError(
                "unable to recheck executable", context={"reason": str(exc)}
            ) from exc
        if stat.st_size != self.size_bytes or digest != self.sha256:
            raise PreflightError("trusted executable changed after it was probed")


@dataclass(frozen=True, slots=True)
class ProcessSpec:
    executable: TrustedExecutable
    arguments: tuple[str, ...]
    cwd: Path
    environment: tuple[tuple[str, str], ...] = ()
    stdin: bytes | None = None
    timeout_seconds: float = 600.0
    output_limit_bytes: int = 1024 * 1024
    termination_grace_seconds: float = 2.0

    def __post_init__(self) -> None:
        if any(not isinstance(argument, str) or "\x00" in argument for argument in self.arguments):
            raise ValueError("process arguments must be NUL-free strings")
        if not self.cwd.is_absolute():
            raise ValueError("process cwd must be absolute")
        _validate_environment(self.environment)
        if self.stdin is not None:
            if not isinstance(self.stdin, bytes):
                raise ValueError("process stdin must be bytes")
            if len(self.stdin) > MAX_STDIN_BYTES:
                raise ValueError("process stdin exceeds the 4 MiB limit")
        _validate_positive_float(self.timeout_seconds, "timeout_seconds", MAX_WALL_SECONDS)
        _validate_positive_float(
            self.termination_grace_seconds,
            "termination_grace_seconds",
            30.0,
        )
        if (
            isinstance(self.output_limit_bytes, bool)
            or not isinstance(self.output_limit_bytes, int)
            or not 1 <= self.output_limit_bytes <= MAX_OUTPUT_BYTES
        ):
            raise ValueError("output_limit_bytes must be between 1 byte and 10 MiB")


@dataclass(frozen=True, slots=True)
class ProcessResult:
    termination_reason: TerminationReason
    returncode: int
    stdout: bytes
    stderr: bytes
    started_at: datetime
    ended_at: datetime
    duration_seconds: float

    @property
    def succeeded(self) -> bool:
        return self.termination_reason is TerminationReason.EXITED and self.returncode == 0


class ProcessExecutor(Protocol):
    async def run(self, spec: ProcessSpec) -> ProcessResult: ...


@dataclass(slots=True)
class _OutputLimiter:
    limit: int
    used: int = 0

    def retain(self, chunk: bytes) -> tuple[bytes, bool]:
        remaining = self.limit - self.used
        retained = chunk[:remaining]
        self.used += len(retained)
        return retained, len(retained) != len(chunk)


class ProcessRunner:
    """Own child lifetime, output draining and deterministic termination."""

    def __init__(self) -> None:
        self._host_executable = TrustedExecutable.capture(Path(sys.executable))

    async def run(self, spec: ProcessSpec) -> ProcessResult:
        spec.executable.assert_unchanged()
        self._host_executable.assert_unchanged()
        cwd = _resolve_cwd(spec.cwd)
        environment = dict(spec.environment)
        host_stdin = _build_process_host_frame(spec, cwd)
        started_at = datetime.now(UTC)
        started_monotonic = asyncio.get_running_loop().time()
        try:
            process = await asyncio.create_subprocess_exec(
                str(self._host_executable.path),
                "-I",
                "-m",
                _PROCESS_HOST_MODULE,
                cwd=cwd,
                env=environment,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                close_fds=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
            )
        except (OSError, ValueError) as exc:
            raise ExecutionError(
                "unable to start child process", context={"reason": str(exc)}
            ) from exc

        try:
            windows_job = WindowsJob.create_for_process(process.pid)
        except Exception:
            process.kill()
            await process.wait()
            raise

        try:
            return await _collect_process_result(
                process,
                spec,
                host_stdin=host_stdin,
                started_at=started_at,
                started_monotonic=started_monotonic,
                windows_job=windows_job,
            )
        finally:
            windows_job.close()


async def _collect_process_result(
    process: asyncio.subprocess.Process,
    spec: ProcessSpec,
    *,
    host_stdin: bytes,
    started_at: datetime,
    started_monotonic: float,
    windows_job: WindowsJob | None,
) -> ProcessResult:

    if process.stdout is None or process.stderr is None:
        await _terminate_process(process, spec.termination_grace_seconds, windows_job=windows_job)
        raise ExecutionError("child process pipes were not created")

    overflow = asyncio.Event()
    limiter = _OutputLimiter(spec.output_limit_bytes)
    stdout_buffer = bytearray()
    stderr_buffer = bytearray()
    stdout_task = asyncio.create_task(
        _drain_stream(process.stdout, stdout_buffer, limiter, overflow),
        name="technocore-orchestrator-stdout-drain",
    )
    stderr_task = asyncio.create_task(
        _drain_stream(process.stderr, stderr_buffer, limiter, overflow),
        name="technocore-orchestrator-stderr-drain",
    )
    stdin_task = asyncio.create_task(
        _feed_stdin(process, host_stdin), name="technocore-orchestrator-stdin-feed"
    )
    wait_task = asyncio.create_task(process.wait(), name="technocore-orchestrator-process-wait")
    overflow_task = asyncio.create_task(
        overflow.wait(), name="technocore-orchestrator-output-limit"
    )

    termination_reason = TerminationReason.EXITED
    try:
        done, _ = await asyncio.wait(
            {wait_task, overflow_task},
            timeout=spec.timeout_seconds,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if overflow_task in done and overflow.is_set():
            termination_reason = TerminationReason.OUTPUT_LIMIT_EXCEEDED
            await _terminate_process(
                process,
                spec.termination_grace_seconds,
                wait_task=wait_task,
                windows_job=windows_job,
            )
        elif wait_task not in done:
            termination_reason = TerminationReason.TIMED_OUT
            await _terminate_process(
                process,
                spec.termination_grace_seconds,
                wait_task=wait_task,
                windows_job=windows_job,
            )
        else:
            await wait_task

        await asyncio.gather(stdout_task, stderr_task)
        await stdin_task
        if overflow.is_set():
            termination_reason = TerminationReason.OUTPUT_LIMIT_EXCEEDED
    except asyncio.CancelledError:
        await asyncio.shield(
            _terminate_process(
                process,
                spec.termination_grace_seconds,
                wait_task=wait_task,
                windows_job=windows_job,
            )
        )
        await asyncio.shield(_settle_io_tasks(stdout_task, stderr_task, stdin_task))
        raise
    finally:
        overflow_task.cancel()
        await asyncio.gather(overflow_task, return_exceptions=True)

    ended_at = datetime.now(UTC)
    returncode = process.returncode
    if returncode is None:
        raise ExecutionError("child process did not reach a terminal state")
    return ProcessResult(
        termination_reason=termination_reason,
        returncode=returncode,
        stdout=bytes(stdout_buffer),
        stderr=bytes(stderr_buffer),
        started_at=started_at,
        ended_at=ended_at,
        duration_seconds=asyncio.get_running_loop().time() - started_monotonic,
    )


async def _drain_stream(
    stream: asyncio.StreamReader,
    output: bytearray,
    limiter: _OutputLimiter,
    overflow: asyncio.Event,
) -> None:
    while chunk := await stream.read(_READ_CHUNK_BYTES):
        retained, exceeded = limiter.retain(chunk)
        output.extend(retained)
        if exceeded:
            overflow.set()


async def _feed_stdin(process: asyncio.subprocess.Process, data: bytes | None) -> None:
    if data is None or process.stdin is None:
        return
    try:
        process.stdin.write(data)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass
    finally:
        process.stdin.close()
        with suppress(BrokenPipeError, ConnectionResetError):
            await process.stdin.wait_closed()


def _build_process_host_frame(spec: ProcessSpec, cwd: Path) -> bytes:
    provider_input = spec.stdin or b""
    header = json.dumps(
        {
            "version": _PROCESS_HOST_PROTOCOL_VERSION,
            "executable": str(spec.executable.path),
            "resolved_executable": str(spec.executable.resolved_path),
            "executable_sha256": spec.executable.sha256,
            "executable_size_bytes": spec.executable.size_bytes,
            "arguments": list(spec.arguments),
            "cwd": str(cwd),
            "stdin_length": len(provider_input),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("ascii")
    if len(header) > 1 << 20:
        raise ExecutionError("trusted process launch header exceeds its 1 MiB limit")
    return len(header).to_bytes(4, "big") + header + provider_input


async def _settle_io_tasks(*tasks: asyncio.Task[None]) -> None:
    await asyncio.gather(*tasks, return_exceptions=True)


async def _terminate_process(
    process: asyncio.subprocess.Process,
    grace_seconds: float,
    *,
    wait_task: asyncio.Task[int] | None = None,
    windows_job: WindowsJob | None = None,
) -> None:
    if process.returncode is not None:
        return
    owns_wait_task = wait_task is None
    wait_task = wait_task or asyncio.create_task(process.wait())
    _send_graceful_termination(process)
    done, _ = await asyncio.wait({wait_task}, timeout=grace_seconds)
    if wait_task in done:
        await wait_task
        return

    try:
        if windows_job is None:
            process.kill()
        else:
            windows_job.terminate()
    except ProcessLookupError:
        pass
    done, _ = await asyncio.wait({wait_task}, timeout=max(grace_seconds, 1.0))
    if wait_task not in done:
        if owns_wait_task:
            wait_task.cancel()
            await asyncio.gather(wait_task, return_exceptions=True)
        raise ExecutionError("unable to stop child process tree", context={"pid": process.pid})
    await wait_task


def _send_graceful_termination(process: asyncio.subprocess.Process) -> None:
    try:
        ctrl_break_event = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break_event is not None:
            process.send_signal(ctrl_break_event)
        else:
            process.terminate()
    except (OSError, ProcessLookupError):
        if process.returncode is None:
            with suppress(ProcessLookupError):
                process.terminate()


def _resolve_cwd(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PreflightError(
            "unable to resolve process working directory", context={"reason": str(exc)}
        ) from exc
    if not resolved.is_dir():
        raise PreflightError("process working directory is not a directory")
    return resolved


def _validate_environment(environment: tuple[tuple[str, str], ...]) -> None:
    seen: set[str] = set()
    for item in environment:
        if not isinstance(item, tuple) or len(item) != 2:
            raise ValueError("process environment must contain key/value pairs")
        key, value = item
        if not isinstance(key, str) or not _ENVIRONMENT_KEY_RE.fullmatch(key):
            raise ValueError("process environment contains an invalid key")
        if not isinstance(value, str) or "\x00" in value:
            raise ValueError("process environment values must be NUL-free strings")
        normalized_key = key.casefold()
        if normalized_key in seen:
            raise ValueError("process environment contains a duplicate key")
        seen.add(normalized_key)


def _validate_positive_float(value: float, label: str, maximum: float) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0 < value <= maximum
    ):
        raise ValueError(f"{label} must be finite, positive and at most {maximum:g}")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
