"""Git worktree isolation and candidate validation."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path

from technocore_orchestrator.domain.models import (
    FULL_SHA_RE,
    RUN_ID_RE,
    TASK_ID_RE,
    Role,
    canonical_repository_path,
)
from technocore_orchestrator.errors import ExecutionError, PreflightError, ProtocolError, StateError
from technocore_orchestrator.execution import (
    ProcessRunner,
    ProcessSpec,
    TerminationReason,
    TrustedExecutable,
)

_SAFE_REF_COMPONENT_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
_GIT_OUTPUT_LIMIT_BYTES = 10 * 1024 * 1024
_WORKTREE_ROLES = frozenset({Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER, Role.VERIFIER})
_REGULAR_GIT_MODES = frozenset({"000000", "100644", "100755"})


@dataclass(frozen=True, slots=True)
class RepositorySnapshot:
    path: Path
    git_directory: Path
    base_commit: str


@dataclass(frozen=True, slots=True)
class ManagedWorktree:
    run_id: str
    task_id: str
    role: Role
    path: Path
    head_commit: str
    branch: str | None
    writable: bool


@dataclass(frozen=True, slots=True)
class CandidateValidation:
    commit: str
    changed_paths: tuple[str, ...]
    diff_sha256: str


@dataclass(frozen=True, slots=True)
class _RawDiffEntry:
    new_mode: str
    path: str


@dataclass(frozen=True, slots=True)
class _StatusEntry:
    index_status: str
    worktree_status: str
    path: str


class WorktreeManager:
    """Create and inspect linked worktrees without force-removing user data."""

    def __init__(
        self,
        *,
        repository: Path,
        root: Path,
        base_commit: str,
        git: TrustedExecutable,
        runner: ProcessRunner | None = None,
    ) -> None:
        if not repository.is_absolute() or not root.is_absolute():
            raise ValueError("repository and worktree root paths must be absolute")
        if not FULL_SHA_RE.fullmatch(base_commit):
            raise ValueError("base_commit must be a lowercase full Git SHA")
        self._repository_input = repository
        self._root_input = root
        self._base_commit = base_commit
        self._git = git
        self._runner = runner or ProcessRunner()
        self._snapshot: RepositorySnapshot | None = None
        self._root: Path | None = None
        self._git_environment: tuple[tuple[str, str], ...] | None = None
        self._hooks_path: Path | None = None

    async def prepare(self) -> RepositorySnapshot:
        """Validate one clean repository and its immutable configured base commit."""

        repository = _resolve_directory(self._repository_input, "repository")
        _validate_host_path(repository, "repository")
        try:
            self._root_input.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PreflightError(
                "unable to create worktree root", context={"reason": str(exc)}
            ) from exc
        root = _resolve_directory(self._root_input, "worktree root")
        _validate_host_path(root, "worktree root")
        if _paths_overlap(repository, root):
            raise PreflightError("worktree root must not overlap the source repository")

        control_root = root / ".control"
        hooks_path = control_root / "empty-hooks"
        home_path = control_root / "git-home"
        try:
            hooks_path.mkdir(parents=True, exist_ok=True)
            home_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise PreflightError(
                "unable to create Git control directories", context={"reason": str(exc)}
            ) from exc

        self._root = root
        self._hooks_path = hooks_path.resolve(strict=True)
        self._git_environment = build_git_environment(self._git.path, home_path)

        top_level = _decode_single_line(
            (await self._git_command("inspect repository", "rev-parse", "--show-toplevel")).stdout,
            "repository root",
        )
        reported_root = _resolve_directory(Path(top_level), "reported repository")
        if reported_root != repository:
            raise PreflightError("configured repository path must be the Git top-level directory")

        git_directory_text = _decode_single_line(
            (
                await self._git_command("inspect Git directory", "rev-parse", "--absolute-git-dir")
            ).stdout,
            "Git directory",
        )
        git_directory = _resolve_directory(Path(git_directory_text), "Git directory")
        commit = _decode_single_line(
            (
                await self._git_command(
                    "validate base commit",
                    "rev-parse",
                    "--verify",
                    f"{self._base_commit}^{{commit}}",
                )
            ).stdout,
            "base commit",
        )
        if commit != self._base_commit:
            raise PreflightError("configured base commit did not resolve exactly")

        status_result = await self._git_command(
            "inspect source cleanliness",
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
        )
        if status_result.stdout:
            raise PreflightError("source repository must be clean before a run")

        self._snapshot = RepositorySnapshot(
            path=repository,
            git_directory=git_directory,
            base_commit=commit,
        )
        return self._snapshot

    async def create(
        self, *, run_id: str, task_id: str, role: Role, commit: str
    ) -> ManagedWorktree:
        """Create one role worktree at an exact, verified commit."""

        self._require_prepared()
        _validate_run_task_role(run_id, task_id, role)
        await self._validate_commit(commit)
        target = self._target_path(run_id, task_id, role)
        branch: str | None = None
        if role is Role.IMPLEMENTER:
            if commit != self._base_commit:
                raise StateError("implementer worktree must begin at the configured base commit")
            branch = _candidate_branch(run_id, task_id)
            arguments = ("worktree", "add", "-b", branch, str(target), commit)
        else:
            arguments = ("worktree", "add", "--detach", str(target), commit)

        await self._git_command("create role worktree", *arguments)
        resolved_target = _resolve_directory(target, "created worktree")
        if resolved_target != target:
            raise ExecutionError("Git created the worktree at an unexpected path")
        head = await self._head_commit(target)
        if head != commit:
            raise ExecutionError("created worktree does not have the requested commit")

        writable = role is Role.IMPLEMENTER
        if not writable:
            _set_tree_owner_writable(target, writable=False)
        return ManagedWorktree(
            run_id=run_id,
            task_id=task_id,
            role=role,
            path=target,
            head_commit=head,
            branch=branch,
            writable=writable,
        )

    async def validate_candidate(
        self,
        worktree: ManagedWorktree,
        candidate_commit: str,
        *,
        allowed_paths: tuple[str, ...],
    ) -> CandidateValidation:
        """Verify ancestry, branch ownership, cleanliness, modes and changed paths."""

        self._validate_managed_worktree(worktree, expected_role=Role.IMPLEMENTER)
        if not FULL_SHA_RE.fullmatch(candidate_commit):
            raise StateError("candidate commit must be a lowercase full Git SHA")
        if not allowed_paths:
            raise ValueError("candidate validation requires at least one allowed path")
        await self._validate_commit(candidate_commit)

        ancestry = await self._git_command(
            "validate candidate ancestry",
            "merge-base",
            "--is-ancestor",
            self._base_commit,
            candidate_commit,
            accepted_returncodes=frozenset({0, 1}),
        )
        if ancestry.returncode == 1:
            raise StateError("candidate commit does not descend from the configured base")

        head = await self._head_commit(worktree.path)
        if head != candidate_commit:
            raise StateError("candidate commit is not the implementer worktree HEAD")
        if worktree.branch is None:
            raise StateError("implementer worktree has no owned candidate branch")
        branch_head = _decode_single_line(
            (
                await self._git_command(
                    "validate candidate branch",
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{worktree.branch}",
                )
            ).stdout,
            "candidate branch",
        )
        if branch_head != candidate_commit:
            raise StateError("candidate branch does not point to the candidate commit")

        if not await self.is_clean(worktree):
            raise StateError("implementer worktree contains uncommitted changes")
        diff = await self._git_command(
            "inspect candidate diff",
            "diff",
            "--raw",
            "-z",
            "--no-renames",
            self._base_commit,
            candidate_commit,
        )
        entries = _parse_raw_diff(diff.stdout)
        if not entries:
            raise StateError("candidate commit contains no product changes")
        changed_paths = tuple(sorted({entry.path for entry in entries}))
        invalid_mode = next(
            (entry for entry in entries if entry.new_mode not in _REGULAR_GIT_MODES), None
        )
        if invalid_mode is not None:
            raise StateError(
                "candidate introduces a symlink, submodule or special file",
                context={"path": invalid_mode.path, "mode": invalid_mode.new_mode},
            )
        out_of_scope = tuple(
            path for path in changed_paths if not _path_is_allowed(path, allowed_paths)
        )
        if out_of_scope:
            raise StateError(
                "candidate changes paths outside the configured scope",
                context={"paths": out_of_scope},
            )
        return CandidateValidation(
            commit=candidate_commit,
            changed_paths=changed_paths,
            diff_sha256=hashlib.sha256(diff.stdout).hexdigest(),
        )

    async def commit_pending_changes(
        self,
        worktree: ManagedWorktree,
        *,
        declared_paths: tuple[str, ...],
        allowed_paths: tuple[str, ...],
    ) -> CandidateValidation:
        """Validate uncommitted model edits, then create the candidate as the supervisor."""

        self._validate_managed_worktree(worktree, expected_role=Role.IMPLEMENTER)
        canonical_declared_paths = tuple(canonical_repository_path(path) for path in declared_paths)
        if not canonical_declared_paths or len(canonical_declared_paths) != len(
            set(canonical_declared_paths)
        ):
            raise StateError("implementer must declare a non-empty unique changed-path set")
        await self._assert_implementer_head_unchanged(worktree)
        status = await self._git_command(
            "inspect pending implementation",
            "-C",
            str(worktree.path),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            "--no-renames",
            include_repository=False,
        )
        entries = _parse_status(status.stdout)
        if not entries:
            raise StateError("implementer produced no pending product changes")
        if any(entry.index_status not in {" ", "?"} for entry in entries):
            raise StateError("implementer staged changes even though the supervisor owns commits")
        if any("U" in {entry.index_status, entry.worktree_status} for entry in entries):
            raise StateError("implementer worktree contains an unresolved Git conflict")
        changed_paths = tuple(sorted({entry.path for entry in entries}))
        if set(canonical_declared_paths) != set(changed_paths):
            raise ProtocolError("implementer declared paths do not match pending changes")
        out_of_scope = tuple(
            path for path in changed_paths if not _path_is_allowed(path, allowed_paths)
        )
        if out_of_scope:
            raise StateError(
                "pending implementation changes paths outside the configured scope",
                context={"paths": out_of_scope},
            )
        for entry in entries:
            if entry.worktree_status == "D":
                continue
            _validate_pending_file(worktree.path, entry.path)

        pathspecs = tuple(allowed_paths)
        await self._git_command(
            "stage validated implementation",
            "-C",
            str(worktree.path),
            "add",
            "-A",
            "--",
            *pathspecs,
            include_repository=False,
        )
        staged = await self._git_command(
            "revalidate staged implementation",
            "-C",
            str(worktree.path),
            "diff",
            "--cached",
            "--raw",
            "-z",
            "--no-renames",
            worktree.head_commit,
            include_repository=False,
        )
        staged_entries = _parse_raw_diff(staged.stdout)
        if {entry.path for entry in staged_entries} != set(changed_paths):
            raise StateError("staged candidate differs from the validated pending changes")
        invalid_mode = next(
            (entry for entry in staged_entries if entry.new_mode not in _REGULAR_GIT_MODES), None
        )
        if invalid_mode is not None:
            raise StateError(
                "staged candidate contains a symlink, submodule or special file",
                context={"path": invalid_mode.path, "mode": invalid_mode.new_mode},
            )
        await self._git_command(
            "commit validated implementation",
            "-C",
            str(worktree.path),
            "-c",
            "user.name=Technocore Agent Orchestrator",
            "-c",
            "user.email=technocore-orchestrator@example.invalid",
            "commit",
            "-m",
            f"Apply supervised workflow candidate for {worktree.task_id}",
            include_repository=False,
        )
        candidate = await self._head_commit(worktree.path)
        return await self.validate_candidate(
            worktree,
            candidate,
            allowed_paths=allowed_paths,
        )

    async def is_clean(self, worktree: ManagedWorktree) -> bool:
        self._validate_managed_worktree(worktree)
        result = await self._git_command(
            "inspect worktree cleanliness",
            "-C",
            str(worktree.path),
            "status",
            "--porcelain=v1",
            "-z",
            "--untracked-files=all",
            include_repository=False,
        )
        return not result.stdout

    async def refresh(self, worktree: ManagedWorktree) -> ManagedWorktree:
        """Re-read a managed worktree's authoritative HEAD without changing it."""

        self._validate_managed_worktree(worktree)
        head = await self._head_commit(worktree.path)
        if worktree.role is Role.IMPLEMENTER:
            if worktree.branch is None:
                raise StateError("implementer worktree has no owned candidate branch")
            branch_head = _decode_single_line(
                (
                    await self._git_command(
                        "refresh candidate branch",
                        "rev-parse",
                        "--verify",
                        f"refs/heads/{worktree.branch}",
                    )
                ).stdout,
                "candidate branch",
            )
            if branch_head != head:
                raise StateError("implementer HEAD diverged from its owned candidate branch")
        return ManagedWorktree(
            run_id=worktree.run_id,
            task_id=worktree.task_id,
            role=worktree.role,
            path=worktree.path,
            head_commit=head,
            branch=worktree.branch,
            writable=worktree.writable,
        )

    async def retarget_readonly(self, worktree: ManagedWorktree, commit: str) -> ManagedWorktree:
        """Move a clean read-only role snapshot to another verified commit."""

        self._validate_managed_worktree(worktree)
        if worktree.writable or worktree.role is Role.IMPLEMENTER:
            raise StateError("writable worktree cannot use read-only retargeting")
        if not await self.is_clean(worktree):
            raise StateError("dirty read-only worktree cannot be retargeted")
        await self._validate_commit(commit)
        _set_tree_owner_writable(worktree.path, writable=True)
        try:
            await self._git_command(
                "retarget read-only worktree",
                "-C",
                str(worktree.path),
                "checkout",
                "--detach",
                commit,
                include_repository=False,
            )
        finally:
            _set_tree_owner_writable(worktree.path, writable=False)
        head = await self._head_commit(worktree.path)
        if head != commit:
            raise StateError("retargeted worktree does not have the requested commit")
        return ManagedWorktree(
            run_id=worktree.run_id,
            task_id=worktree.task_id,
            role=worktree.role,
            path=worktree.path,
            head_commit=head,
            branch=None,
            writable=False,
        )

    async def _assert_implementer_head_unchanged(self, worktree: ManagedWorktree) -> None:
        head = await self._head_commit(worktree.path)
        if head != worktree.head_commit:
            raise StateError("implementer changed HEAD before supervisor candidate creation")
        if worktree.branch is None:
            raise StateError("implementer worktree has no owned candidate branch")
        branch_head = _decode_single_line(
            (
                await self._git_command(
                    "inspect pending candidate branch",
                    "rev-parse",
                    "--verify",
                    f"refs/heads/{worktree.branch}",
                )
            ).stdout,
            "pending candidate branch",
        )
        if branch_head != worktree.head_commit:
            raise StateError("implementer changed the candidate branch before supervisor commit")

    async def remove(self, worktree: ManagedWorktree) -> None:
        """Remove only a recognized clean worktree, without force or ref deletion."""

        self._validate_managed_worktree(worktree)
        if not await self.is_clean(worktree):
            raise StateError("dirty worktree is retained for operator inspection")
        registered = await self._registered_worktree_paths()
        if worktree.path not in registered:
            raise StateError("worktree is not registered in the configured repository")

        restored_permissions = not worktree.writable
        if restored_permissions:
            _set_tree_owner_writable(worktree.path, writable=True)
        try:
            await self._git_command(
                "remove role worktree", "worktree", "remove", str(worktree.path)
            )
        except Exception:
            if restored_permissions and worktree.path.exists():
                _set_tree_owner_writable(worktree.path, writable=False)
            raise
        if worktree.path.exists() or worktree.path.is_symlink():
            raise ExecutionError("Git reported success but the worktree path still exists")

    async def _validate_commit(self, commit: str) -> None:
        if not FULL_SHA_RE.fullmatch(commit):
            raise StateError("worktree commit must be a lowercase full Git SHA")
        resolved = _decode_single_line(
            (
                await self._git_command(
                    "validate worktree commit",
                    "rev-parse",
                    "--verify",
                    f"{commit}^{{commit}}",
                )
            ).stdout,
            "worktree commit",
        )
        if resolved != commit:
            raise StateError("worktree commit did not resolve exactly")

    async def _head_commit(self, path: Path) -> str:
        return _decode_single_line(
            (
                await self._git_command(
                    "inspect worktree HEAD",
                    "-C",
                    str(path),
                    "rev-parse",
                    "--verify",
                    "HEAD^{commit}",
                    include_repository=False,
                )
            ).stdout,
            "worktree HEAD",
        )

    async def _registered_worktree_paths(self) -> frozenset[Path]:
        result = await self._git_command(
            "list registered worktrees", "worktree", "list", "--porcelain"
        )
        paths: set[Path] = set()
        for line in result.stdout.splitlines():
            if not line.startswith(b"worktree "):
                continue
            raw_path = os.fsdecode(line.removeprefix(b"worktree "))
            paths.add(_resolve_unchecked(Path(raw_path)))
        return frozenset(paths)

    def _target_path(self, run_id: str, task_id: str, role: Role) -> Path:
        root = self._require_prepared_root()
        parent = root / run_id / task_id
        try:
            parent.mkdir(parents=True, exist_ok=True)
            resolved_parent = parent.resolve(strict=True)
        except OSError as exc:
            raise PreflightError(
                "unable to create run worktree directory", context={"reason": str(exc)}
            ) from exc
        if not _is_within(resolved_parent, root):
            raise PreflightError("run worktree directory escaped its configured root")
        target = resolved_parent / role.value
        if target.exists() or target.is_symlink():
            raise StateError("role worktree path already exists", context={"role": role})
        return target

    def _validate_managed_worktree(
        self, worktree: ManagedWorktree, *, expected_role: Role | None = None
    ) -> None:
        _validate_run_task_role(worktree.run_id, worktree.task_id, worktree.role)
        if expected_role is not None and worktree.role is not expected_role:
            raise StateError("worktree role is not authorized for this operation")
        root = self._require_prepared_root()
        expected = root / worktree.run_id / worktree.task_id / worktree.role.value
        if worktree.path != expected or not _is_within(worktree.path, root):
            raise StateError("managed worktree path does not match its run and role")
        if not worktree.path.exists() or worktree.path.is_symlink():
            raise StateError("managed worktree path is missing or is a symlink")

    def _require_prepared(self) -> RepositorySnapshot:
        if self._snapshot is None:
            raise StateError("worktree manager must be prepared before use")
        return self._snapshot

    def _require_prepared_root(self) -> Path:
        self._require_prepared()
        if self._root is None:
            raise StateError("worktree manager root is unavailable")
        return self._root

    async def _git_command(
        self,
        operation: str,
        *arguments: str,
        accepted_returncodes: frozenset[int] = frozenset({0}),
        include_repository: bool = True,
    ):
        if self._git_environment is None or self._hooks_path is None:
            raise StateError("worktree manager must be prepared before running Git")
        command = (
            "-c",
            f"core.hooksPath={self._hooks_path}",
            "-c",
            "credential.helper=",
        )
        if include_repository:
            command += ("-C", str(self._repository_input))
        command += arguments
        result = await self._runner.run(
            ProcessSpec(
                executable=self._git,
                arguments=command,
                cwd=self._repository_input,
                environment=self._git_environment,
                timeout_seconds=60,
                output_limit_bytes=_GIT_OUTPUT_LIMIT_BYTES,
                termination_grace_seconds=2,
            )
        )
        if (
            result.termination_reason is not TerminationReason.EXITED
            or result.returncode not in accepted_returncodes
        ):
            raise ExecutionError(
                "Git operation failed",
                context={
                    "operation": operation,
                    "returncode": result.returncode,
                    "termination": result.termination_reason,
                },
            )
        return result


def build_git_environment(git: Path, home: Path) -> tuple[tuple[str, str], ...]:
    path_entries = [str(git.parent)]
    values: dict[str, str] = {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "HOME": str(home.resolve(strict=True)),
        "LC_ALL": "C",
        "LANG": "C",
    }
    system_root = os.environ.get("SYSTEMROOT")
    if not system_root:
        raise PreflightError("SystemRoot is required to run Git on Windows")
    values["SystemRoot"] = system_root
    path_entries.append(str(Path(system_root) / "System32"))
    values["PATH"] = os.pathsep.join(dict.fromkeys(path_entries))
    return tuple(sorted(values.items()))


def _parse_raw_diff(raw: bytes) -> tuple[_RawDiffEntry, ...]:
    if not raw:
        return ()
    fields = raw.split(b"\x00")
    if fields[-1] != b"":
        raise StateError("Git raw diff was not NUL terminated")
    entries: list[_RawDiffEntry] = []
    index = 0
    while index < len(fields) - 1:
        header = fields[index]
        if not header.startswith(b":") or index + 1 >= len(fields) - 1:
            raise StateError("Git raw diff has an invalid record shape")
        parts = header.split()
        if len(parts) != 5:
            raise StateError("Git raw diff has an invalid header")
        status_code = parts[4]
        if not status_code or status_code[:1] not in {
            b"A",
            b"C",
            b"D",
            b"M",
            b"R",
            b"T",
            b"U",
            b"X",
            b"B",
        }:
            raise StateError("Git raw diff contains an unknown status")
        try:
            new_mode = parts[1].decode("ascii")
            path = fields[index + 1].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise StateError("Git diff contains a non-UTF-8 path") from exc
        _validate_git_path(path)
        entries.append(_RawDiffEntry(new_mode=new_mode, path=path))
        index += 2
    return tuple(entries)


def _parse_status(raw: bytes) -> tuple[_StatusEntry, ...]:
    if not raw:
        return ()
    records = raw.split(b"\x00")
    if records[-1] != b"":
        raise StateError("Git status output was not NUL terminated")
    entries: list[_StatusEntry] = []
    for record in records[:-1]:
        if len(record) < 4 or record[2:3] != b" ":
            raise StateError("Git status output has an invalid record")
        try:
            index_status = record[0:1].decode("ascii")
            worktree_status = record[1:2].decode("ascii")
            path = record[3:].decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise StateError("Git status contains a non-UTF-8 path or status") from exc
        _validate_git_path(path)
        entries.append(
            _StatusEntry(
                index_status=index_status,
                worktree_status=worktree_status,
                path=path,
            )
        )
    return tuple(entries)


def _validate_git_path(path: str) -> None:
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or "\x00" in path
        or any(part in {"", ".", ".."} for part in path.split("/"))
    ):
        raise StateError("Git produced an unsafe repository-relative path")


def _path_is_allowed(path: str, allowed_paths: tuple[str, ...]) -> bool:
    return any(
        allowed == "." or path == allowed or path.startswith(f"{allowed}/")
        for allowed in allowed_paths
    )


def _validate_pending_file(worktree: Path, relative_path: str) -> None:
    target = worktree.joinpath(*relative_path.split("/"))
    try:
        metadata = target.lstat()
        resolved_root = worktree.resolve(strict=True)
        resolved_target = target.resolve(strict=True)
    except OSError as exc:
        raise StateError(
            "pending changed path cannot be inspected", context={"path": relative_path}
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or not _is_within(resolved_target, resolved_root)
    ):
        raise StateError(
            "pending candidate contains a symlink, directory or special file",
            context={"path": relative_path},
        )


def _candidate_branch(run_id: str, task_id: str) -> str:
    run_component = run_id.removeprefix("run_")
    if not _SAFE_REF_COMPONENT_RE.fullmatch(run_component) or not _SAFE_REF_COMPONENT_RE.fullmatch(
        task_id
    ):
        raise StateError("run or task identifier cannot form a safe Git branch")
    return f"technocore-orchestrator/{run_component}/{task_id}/candidate"


def _validate_run_task_role(run_id: str, task_id: str, role: Role) -> None:
    if not RUN_ID_RE.fullmatch(run_id) or not TASK_ID_RE.fullmatch(task_id):
        raise StateError("worktree run or task identifier is invalid")
    if role not in _WORKTREE_ROLES:
        raise StateError("role does not own a worktree")


def _resolve_directory(path: Path, label: str) -> Path:
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise PreflightError(f"unable to resolve {label}", context={"reason": str(exc)}) from exc
    if not resolved.is_dir():
        raise PreflightError(f"{label} is not a directory")
    return resolved


def _decode_single_line(raw: bytes, label: str) -> str:
    try:
        value = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ExecutionError(f"Git returned a non-UTF-8 {label}") from exc
    if not value or "\n" in value or "\r" in value:
        raise ExecutionError(f"Git returned an invalid {label}")
    return value


def _validate_host_path(path: Path, label: str) -> None:
    value = str(path)
    if "\x00" in value or "\r" in value or "\n" in value:
        raise PreflightError(f"{label} path contains a prohibited control character")


def _paths_overlap(first: Path, second: Path) -> bool:
    return _is_within(first, second) or _is_within(second, first)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _set_tree_owner_writable(root: Path, *, writable: bool) -> None:
    """Change owner write bits without following repository symlinks."""

    try:
        for current_root, directories, files in os.walk(root, topdown=False, followlinks=False):
            current = Path(current_root)
            for name in files:
                _set_owner_writable(current / name, writable=writable)
            for name in directories:
                _set_owner_writable(current / name, writable=writable)
        _set_owner_writable(root, writable=writable)
    except OSError as exc:
        raise ExecutionError(
            "unable to apply worktree permissions", context={"reason": str(exc)}
        ) from exc


def _set_owner_writable(path: Path, *, writable: bool) -> None:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode):
        return
    mode = stat.S_IMODE(metadata.st_mode)
    updated = (
        mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
    )
    path.chmod(updated)


def _resolve_unchecked(path: Path) -> Path:
    return path.resolve(strict=False)
