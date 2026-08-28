from __future__ import annotations

import asyncio
import shutil
import subprocess
from pathlib import Path

import pytest

from technocore_orchestrator.domain.models import Role
from technocore_orchestrator.errors import StorageError
from technocore_orchestrator.execution import TrustedExecutable
from technocore_orchestrator.managed_project import create_generated_project_repository
from technocore_orchestrator.worktrees import WorktreeManager


def _git(repository: Path, *arguments: str) -> str:
    git = shutil.which("git")
    assert git is not None
    completed = subprocess.run(  # noqa: S603 - resolved test fixture executable
        [git, "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def test_generated_project_starts_from_a_clean_empty_commit(tmp_path: Path) -> None:
    repository = create_generated_project_repository(tmp_path / "state", "run_generated01")

    assert repository == tmp_path / "state" / "generated-projects" / "run_generated01" / "source"
    assert _git(repository, "branch", "--show-current") == "main"
    assert _git(repository, "status", "--short") == ""
    assert _git(repository, "ls-tree", "--name-only", "HEAD") == ""


def test_generated_project_never_reuses_a_run_directory(tmp_path: Path) -> None:
    create_generated_project_repository(tmp_path / "state", "run_generated01")

    with pytest.raises(StorageError, match="already exists"):
        create_generated_project_repository(tmp_path / "state", "run_generated01")


def test_pending_changes_accept_a_dot_slash_declared_path(tmp_path: Path) -> None:
    repository = create_generated_project_repository(tmp_path / "state", "run_generated01")
    git_path = shutil.which("git")
    assert git_path is not None
    manager = WorktreeManager(
        repository=repository.resolve(),
        root=(tmp_path / "worktrees").resolve(),
        base_commit=_git(repository, "rev-parse", "HEAD"),
        git=TrustedExecutable.capture(Path(git_path)),
    )

    asyncio.run(manager.prepare())
    worktree = asyncio.run(
        manager.create(
            run_id="run_generated01",
            task_id="task",
            role=Role.IMPLEMENTER,
            commit=_git(repository, "rev-parse", "HEAD"),
        )
    )
    (worktree.path / "index.html").write_text("<!doctype html>\n", encoding="utf-8")

    candidate = asyncio.run(
        manager.commit_pending_changes(
            worktree,
            declared_paths=("./index.html",),
            allowed_paths=(".",),
        )
    )

    assert candidate.changed_paths == ("index.html",)
    assert _git(worktree.path, "status", "--short") == ""
