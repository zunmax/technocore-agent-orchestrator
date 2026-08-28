"""Safe operator actions over retained workflow state."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from technocore_orchestrator.domain.models import RUN_ID_RE, Role
from technocore_orchestrator.errors import StateError, StorageError, WorkflowError
from technocore_orchestrator.storage import SQLiteStore, WorktreeStatus
from technocore_orchestrator.worktrees import ManagedWorktree, RepositorySnapshot


@dataclass(frozen=True, slots=True)
class CleanupResult:
    role: Role
    path: Path
    action: str
    clean: bool | None
    reason: str | None


class CleanupWorktreeManager(Protocol):
    async def prepare(self) -> RepositorySnapshot: ...

    async def is_clean(self, worktree: ManagedWorktree) -> bool: ...

    async def remove(self, worktree: ManagedWorktree) -> None: ...


async def cleanup_run(
    *,
    store: SQLiteStore,
    manager: CleanupWorktreeManager,
    run_id: str,
    dry_run: bool,
) -> tuple[CleanupResult, ...]:
    """Remove only clean, recognized linked worktrees from a terminal run."""

    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("cleanup run id is invalid", context={"run_id": run_id})
    run = store.get_run(run_id)
    if not run.state.is_terminal:
        raise StateError("worktree cleanup requires a terminal run")
    snapshot = await manager.prepare()
    if snapshot.path != run.repository_path or snapshot.base_commit != run.base_commit:
        raise StateError("cleanup repository does not match the durable run record")

    results: list[CleanupResult] = []
    for record in store.list_worktrees(run_id):
        if record.status is WorktreeStatus.REMOVED:
            results.append(CleanupResult(record.role, record.path, "already_removed", None, None))
            continue
        try:
            observation = store.get_latest_worktree_observation(run_id, record.role)
            managed = ManagedWorktree(
                run_id=run.run_id,
                task_id=run.task_id,
                role=record.role,
                path=record.path,
                head_commit=observation.head_commit,
                branch=record.branch,
                writable=record.writable,
            )
            clean = await manager.is_clean(managed)
            if not clean:
                if not dry_run:
                    store.mark_worktree_status(run_id, record.role, WorktreeStatus.RETAINED)
                results.append(
                    CleanupResult(
                        record.role,
                        record.path,
                        "retain",
                        False,
                        "dirty worktree requires operator inspection",
                    )
                )
                continue
            if dry_run:
                results.append(
                    CleanupResult(
                        record.role,
                        record.path,
                        "would_remove",
                        True,
                        "registration is rechecked during removal",
                    )
                )
                continue
            await manager.remove(managed)
            store.mark_worktree_status(run_id, record.role, WorktreeStatus.REMOVED)
            results.append(CleanupResult(record.role, record.path, "removed", True, None))
        except WorkflowError as exc:
            if not dry_run:
                store.mark_worktree_status(run_id, record.role, WorktreeStatus.RETAINED)
            results.append(
                CleanupResult(
                    record.role,
                    record.path,
                    "retain",
                    None,
                    f"{exc.category.value}: {exc.message}",
                )
            )
    return tuple(results)
