"""Create an empty internal Git baseline for a generated codebase."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from technocore_orchestrator.domain.models import RUN_ID_RE
from technocore_orchestrator.errors import PreflightError, StorageError
from technocore_orchestrator.worktrees import build_git_environment

_MAX_GIT_OUTPUT_BYTES = 64 * 1024


def create_generated_project_repository(storage_root: Path, run_id: str) -> Path:
    """Create one empty, committed repository owned entirely by this run."""

    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("generated project run id is invalid")
    git_value = shutil.which("git")
    if git_value is None:
        raise PreflightError("Git is required to create the generated project")
    git = Path(git_value).resolve(strict=True)
    root = _prepare_storage_root(storage_root)
    projects = _prepare_child_directory(root, "generated-projects")
    run_root = projects / run_id
    try:
        run_root.mkdir(mode=0o700)
        repository = run_root / "source"
        hooks = run_root / "empty-hooks"
        home = run_root / "git-home"
        repository.mkdir(mode=0o700)
        hooks.mkdir(mode=0o700)
        home.mkdir(mode=0o700)
    except FileExistsError as exc:
        raise StorageError("generated project already exists for this run id") from exc
    except OSError as exc:
        raise StorageError("unable to prepare the generated project directory") from exc

    environment = dict(build_git_environment(git, home))
    _run_git(
        git,
        environment,
        "-c",
        "init.templateDir=",
        "init",
        "--initial-branch=main",
        str(repository),
    )
    _run_git(
        git,
        environment,
        "-C",
        str(repository),
        "-c",
        f"core.hooksPath={hooks}",
        "-c",
        "user.name=Technocore Agent Orchestrator",
        "-c",
        "user.email=technocore-orchestrator@localhost",
        "-c",
        "commit.gpgSign=false",
        "commit",
        "--allow-empty",
        "--no-gpg-sign",
        "--no-verify",
        "--message=Generated project baseline",
    )
    resolved = repository.resolve(strict=True)
    if resolved.parent != run_root.resolve(strict=True):
        raise StorageError("generated project escaped its run directory")
    return resolved


def _prepare_storage_root(storage_root: Path) -> Path:
    absolute = storage_root.absolute()
    try:
        if absolute.is_symlink():
            raise StorageError("workflow storage root must not be a symlink")
        absolute.mkdir(parents=True, exist_ok=True, mode=0o700)
        root = absolute.resolve(strict=True)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("unable to prepare workflow storage") from exc
    return root


def _prepare_child_directory(root: Path, name: str) -> Path:
    directory = root / name
    try:
        if directory.is_symlink():
            raise StorageError(f"{name} directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        resolved = directory.resolve(strict=True)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(f"unable to prepare {name} directory") from exc
    if resolved.parent != root:
        raise StorageError(f"{name} directory escaped workflow storage")
    return resolved


def _run_git(git: Path, environment: dict[str, str], *arguments: str) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - captured Git and fixed internal arguments
            [str(git), *arguments],
            check=False,
            capture_output=True,
            timeout=30,
            env=environment,
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise PreflightError("unable to initialize the generated project") from exc
    if len(completed.stdout) + len(completed.stderr) > _MAX_GIT_OUTPUT_BYTES:
        raise PreflightError("generated project Git output exceeded its limit")
    if completed.returncode != 0:
        reason = completed.stderr.decode("utf-8", errors="replace")[:500]
        raise PreflightError(
            "unable to initialize the generated project",
            context={"reason": reason, "returncode": completed.returncode},
        )
