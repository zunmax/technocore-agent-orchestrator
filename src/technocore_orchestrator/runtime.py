"""Credential-free runtime assembly and cooperative CLI cancellation."""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import shutil
import sys
from collections.abc import Coroutine
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

from technocore_orchestrator.adapters import FakeHarnessAdapter, FakeScenario, HarnessAdapter
from technocore_orchestrator.config import LoadedConfig
from technocore_orchestrator.domain.models import RUN_ID_RE, HarnessKind
from technocore_orchestrator.errors import PreflightError, StateError, StorageError
from technocore_orchestrator.execution import TrustedExecutable
from technocore_orchestrator.orchestrator import WorkflowOrchestrator
from technocore_orchestrator.publication import LocalEventPublisher
from technocore_orchestrator.storage import SQLiteStore
from technocore_orchestrator.verification import VerificationJob, Verifier
from technocore_orchestrator.windows_lock import WindowsFileLease, try_acquire_windows_file_lock
from technocore_orchestrator.worktrees import WorktreeManager

_CONTROL_POLL_SECONDS = 0.2


@dataclass(frozen=True, slots=True)
class RunControlPaths:
    lock: Path
    cancel: Path


def new_run_id() -> str:
    return f"run_{secrets.token_hex(8)}"


def build_fake_orchestrator(
    loaded: LoadedConfig,
    store: SQLiteStore,
    *,
    scenario: FakeScenario = FakeScenario.SUCCESS,
) -> WorkflowOrchestrator:
    """Assemble the zero-credential alpha runtime from validated configuration."""

    config = loaded.config
    assignments = {config.roles.planner, config.roles.implementer, config.roles.reviewer}
    if assignments != {HarnessKind.FAKE}:
        raise PreflightError(
            "the credential-free runtime requires all-fake role assignments; "
            "real adapters use the explicit real-run runtime"
        )
    git = _capture_executable("git")
    python = TrustedExecutable.capture(Path(sys.executable))
    adapter: HarnessAdapter = FakeHarnessAdapter(
        python=python,
        git=git,
        scenario=scenario,
        edit_path=_fake_edit_path(config.repository.allowed_paths),
    )
    return WorkflowOrchestrator(
        loaded_config=loaded,
        store=store,
        worktrees=WorktreeManager(
            repository=config.repository.path,
            root=(config.storage.root / "worktrees").resolve(),
            base_commit=config.repository.base_commit,
            git=git,
        ),
        adapters={HarnessKind.FAKE: adapter},
        publisher=LocalEventPublisher(store),
        verifier=Verifier(),
        verification_jobs=_verification_jobs(loaded),
    )


async def run_with_control[OperationResult](
    operation: Coroutine[Any, Any, OperationResult],
    *,
    storage_root: Path,
    run_id: str,
) -> OperationResult:
    """Own one run lock and turn a separate cancel request into task cancellation."""

    paths = _control_paths(storage_root, run_id)
    lease: WindowsFileLease | None = None
    try:
        lease = _acquire_control_lock(paths.lock, run_id)
        _remove_cancel_request(paths.cancel)
    except Exception:
        if lease is not None:
            lease.close()
        operation.close()
        raise
    task = asyncio.create_task(operation, name=f"technocore-orchestrator-{run_id}")
    watcher = asyncio.create_task(
        _watch_for_cancel(paths), name=f"technocore-orchestrator-cancel-{run_id}"
    )
    try:
        done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
        if task in done:
            return await task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await watcher
        raise asyncio.CancelledError
    finally:
        watcher.cancel()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await asyncio.gather(watcher, return_exceptions=True)
        _remove_cancel_request(paths.cancel, suppress_errors=True)
        lease.close()


def request_active_cancellation(storage_root: Path, run_id: str) -> bool:
    """Create a bounded marker only while a supervisor owns the run's kernel lock."""

    paths = _control_paths(storage_root, run_id)
    if not _control_lock_is_active(paths.lock, run_id):
        _remove_cancel_request(paths.cancel)
        return False
    if paths.cancel.exists() or paths.cancel.is_symlink():
        if paths.cancel.is_symlink() or not paths.cancel.is_file():
            raise StorageError("cancellation request path is not a regular file")
    else:
        try:
            descriptor = os.open(paths.cancel, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            if paths.cancel.is_symlink() or not paths.cancel.is_file():
                raise StorageError("cancellation request path is not a regular file") from None
        except OSError as exc:
            raise StorageError("unable to create cancellation request") from exc
        else:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump({"run_id": run_id, "requested_at": datetime.now(UTC).isoformat()}, handle)
                handle.flush()
                os.fsync(handle.fileno())
    if _control_lock_is_active(paths.lock, run_id):
        return True
    _remove_cancel_request(paths.cancel)
    return False


def _control_paths(storage_root: Path, run_id: str) -> RunControlPaths:
    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("control run id is invalid", context={"run_id": run_id})
    try:
        root = storage_root.resolve()
        control_path = root / "control"
        if control_path.is_symlink():
            raise StorageError("run control directory must not be a symlink")
        control_path.mkdir(parents=True, exist_ok=True, mode=0o700)
        control = control_path.resolve(strict=True)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("unable to prepare run control directory") from exc
    if control.parent != root or control.is_symlink():
        raise StorageError("run control directory escaped its storage root")
    return RunControlPaths(
        lock=control / f"{run_id}.lock",
        cancel=control / f"{run_id}.cancel",
    )


def _acquire_control_lock(path: Path, run_id: str) -> WindowsFileLease:
    lease = _try_control_lock(path, run_id, record_owner=True)
    if lease is None:
        raise StateError("another supervisor owns this run")
    return lease


def _control_lock_is_active(path: Path, run_id: str) -> bool:
    lease = _try_control_lock(path, run_id, record_owner=False)
    if lease is None:
        return True
    lease.close()
    return False


def _try_control_lock(
    path: Path,
    run_id: str,
    *,
    record_owner: bool,
) -> WindowsFileLease | None:
    lease = try_acquire_windows_file_lock(path, label="run control lock")
    if lease is None:
        return None
    try:
        if record_owner:
            payload = json.dumps(
                {"run_id": run_id, "pid": os.getpid()},
                ensure_ascii=True,
                separators=(",", ":"),
            ).encode("ascii")
            lease.handle.seek(0)
            lease.handle.truncate()
            lease.handle.write(payload)
            lease.handle.flush()
            os.fsync(lease.handle.fileno())
        return lease
    except OSError as exc:
        lease.close()
        raise StorageError("unable to record the run control owner") from exc


async def _watch_for_cancel(paths: RunControlPaths) -> None:
    while True:
        try:
            requested = _has_cancel_request(paths.cancel)
        except (OSError, StorageError) as exc:
            raise StorageError("unable to inspect the cancellation request") from exc
        if requested:
            return
        await asyncio.sleep(_CONTROL_POLL_SECONDS)


def _has_cancel_request(path: Path) -> bool:
    if not path.exists() and not path.is_symlink():
        return False
    if path.is_symlink() or not path.is_file():
        raise StorageError("cancellation request path is not a regular file")
    return True


def _remove_cancel_request(path: Path, *, suppress_errors: bool = False) -> None:
    try:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_symlink() or not path.is_file():
            raise StorageError("cancellation request path is not a regular file")
        path.unlink()
    except (OSError, StorageError):
        if not suppress_errors:
            raise


def _verification_jobs(loaded: LoadedConfig) -> tuple[VerificationJob, ...]:
    jobs: list[VerificationJob] = []
    environment = _safe_child_environment()
    for command in loaded.config.verification.commands:
        executable = _capture_executable(command.argv[0])
        jobs.append(
            VerificationJob(
                id=command.id,
                executable=executable,
                arguments=command.argv[1:],
                environment=environment,
                timeout_seconds=command.timeout_seconds,
                output_limit_bytes=loaded.config.limits.max_output_bytes,
                required=command.required,
            )
        )
    return tuple(jobs)


def _capture_executable(value: str) -> TrustedExecutable:
    candidate = Path(value)
    discovered = str(candidate) if candidate.is_absolute() else shutil.which(value)
    if discovered is None:
        raise PreflightError("configured executable is not available", context={"tool": value})
    return TrustedExecutable.capture(Path(discovered))


def _safe_child_environment() -> tuple[tuple[str, str], ...]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "HOME",
        "USERPROFILE",
        "LOCALAPPDATA",
        "APPDATA",
        "XDG_CONFIG_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
        "LANG",
        "LC_ALL",
        "TERM",
    }
    environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    environment.update(
        {
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
        }
    )
    return tuple(sorted(environment.items()))


def _fake_edit_path(allowed_paths: tuple[str, ...]) -> str:
    selected = allowed_paths[0]
    if selected == ".":
        return "product.txt"
    path = PurePosixPath(selected)
    if path.suffix:
        return path.as_posix()
    return (path / "product.txt").as_posix()
