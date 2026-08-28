from __future__ import annotations

from pathlib import Path

import pytest

from technocore_orchestrator.domain.models import Role
from technocore_orchestrator.errors import StorageError
from technocore_orchestrator.storage import SQLiteStore


def _stored_run(
    store: SQLiteStore, tmp_path: Path, *, run_id: str, external_worktree: bool = False
) -> tuple[Path, Path, Path, Path]:
    previous_storage = (tmp_path / "previous" / "state").resolve()
    current_storage = (tmp_path / "current" / "state").resolve()
    generated_suffix = Path("generated-projects") / run_id / "source"
    previous_repository = (previous_storage / generated_suffix).resolve()
    current_repository = (current_storage / generated_suffix).resolve()
    store.create_run(
        run_id=run_id,
        task_id="task_relocation",
        config_digest="a" * 64,
        repository_path=previous_repository,
        base_commit="b" * 40,
    )
    worktree = (
        (tmp_path / "external-worktree").resolve()
        if external_worktree
        else (previous_storage / "worktrees" / run_id / "task_relocation" / "planner").resolve()
    )
    store.record_worktree(
        run_id=run_id,
        role=Role.PLANNER,
        path=worktree,
        branch=None,
        writable=False,
        initial_commit="b" * 40,
    )
    return previous_storage, current_storage, previous_repository, current_repository


def test_run_and_worktree_paths_relocate_atomically_and_idempotently(tmp_path: Path) -> None:
    run_id = "run_relocate3"
    with SQLiteStore.open(tmp_path / "state.sqlite3") as store:
        previous_storage, current_storage, previous_repository, current_repository = _stored_run(
            store, tmp_path, run_id=run_id
        )

        for _attempt in range(2):
            store.relocate_run_paths(
                run_id=run_id,
                previous_storage_root=previous_storage,
                current_storage_root=current_storage,
                previous_repository_path=previous_repository,
                current_repository_path=current_repository,
            )

        assert store.get_run(run_id).repository_path == current_repository
        assert (
            store.get_worktree(run_id, Role.PLANNER).path
            == (current_storage / "worktrees" / run_id / "task_relocation" / "planner").resolve()
        )


def test_run_path_relocation_rejects_external_worktree_and_rolls_back(tmp_path: Path) -> None:
    run_id = "run_relocate4"
    with SQLiteStore.open(tmp_path / "state.sqlite3") as store:
        previous_storage, current_storage, previous_repository, current_repository = _stored_run(
            store, tmp_path, run_id=run_id, external_worktree=True
        )

        with pytest.raises(StorageError, match="worktree path cannot be safely relocated"):
            store.relocate_run_paths(
                run_id=run_id,
                previous_storage_root=previous_storage,
                current_storage_root=current_storage,
                previous_repository_path=previous_repository,
                current_repository_path=current_repository,
            )

        assert store.get_run(run_id).repository_path == previous_repository
