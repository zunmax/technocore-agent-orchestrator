"""Resolve one reusable workflow profile against its managed Git repository."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

from technocore_orchestrator.config import (
    ConfigPathRelocation,
    LoadedConfig,
    LoadedProfile,
    RepositoryConfig,
    TaskConfig,
    VerificationCommand,
    VerificationConfig,
    WorkflowConfig,
    loaded_config_from_model,
)
from technocore_orchestrator.domain.models import RUN_ID_RE
from technocore_orchestrator.errors import ConfigurationError, PreflightError, StorageError

_MAX_GIT_OUTPUT_BYTES = 64 * 1024
_MAX_RESOLVED_CONFIG_BYTES = 1024 * 1024


def resolve_profile(profile: LoadedProfile, repository: Path) -> LoadedConfig:
    """Bind a profile prompt to its clean managed repository and empty baseline."""

    root = _repository_root(repository)
    base_commit = _git_text(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", base_commit):
        raise PreflightError("current Git HEAD is not a full lowercase commit SHA")
    prompt = profile.profile.task.prompt.strip()
    prompt_digest = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
    title = next((line.strip() for line in prompt.splitlines() if line.strip()), "Workflow task")
    criteria = profile.profile.task.acceptance_criteria or (
        "The requested prompt is implemented completely within the allowed repository paths.",
        "The final candidate passes every configured deterministic verification command.",
        "The reviewer finds no unresolved correctness, security, or maintainability issue.",
    )
    commands = list(profile.profile.verification.commands)
    if profile.profile.verification.include_git_diff_check:
        git = shutil.which("git")
        if git is None:
            raise PreflightError("Git is required to resolve a workflow profile")
        commands.insert(
            0,
            VerificationCommand(
                id="git_diff_check",
                argv=(
                    str(Path(git).resolve(strict=True)),
                    "-c",
                    "core.whitespace=blank-at-eol,blank-at-eof,space-before-tab,cr-at-eol",
                    "diff",
                    "--check",
                    f"{base_commit}..HEAD",
                ),
                timeout_seconds=60,
                required=True,
            ),
        )
    resolved = WorkflowConfig(
        schema_version=3,
        repository=RepositoryConfig(
            path=root,
            base_commit=base_commit,
            allowed_paths=profile.profile.task.allowed_paths,
        ),
        task=TaskConfig(
            id=f"task_{prompt_digest[:12]}",
            title=title[:160],
            brief=prompt,
            acceptance_criteria=criteria,
        ),
        roles=profile.profile.roles,
        providers=profile.profile.providers,
        limits=profile.profile.limits,
        technocore=profile.profile.technocore,
        verification=VerificationConfig(commands=tuple(commands)),
        storage=profile.profile.storage,
        output=profile.profile.output,
    )
    return loaded_config_from_model(resolved, profile.source_path)


def persist_resolved_config(loaded: LoadedConfig, profile: LoadedProfile, run_id: str) -> Path:
    """Persist the immutable run config so later commands survive profile edits."""

    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("resolved configuration run id is invalid")
    directory = _resolved_directory(profile.profile.storage.root)
    target = directory / f"{run_id}.json"
    payload = {
        "run_id": run_id,
        "profile_sha256": profile.sha256,
        "resolved_sha256": loaded.sha256,
        "config": loaded.config.model_dump(mode="json"),
    }
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    if len(encoded) > _MAX_RESOLVED_CONFIG_BYTES:
        raise StorageError("resolved configuration exceeds the 1 MiB limit")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    try:
        descriptor = os.open(target, flags, 0o600)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError as exc:
        raise StorageError("resolved configuration already exists for this run id") from exc
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        raise StorageError("unable to persist resolved workflow configuration") from exc
    return target


def load_resolved_config(profile: LoadedProfile, run_id: str) -> LoadedConfig:
    """Load the exact immutable configuration selected when a profile run began."""

    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("resolved configuration run id is invalid")
    path = _resolved_directory(profile.profile.storage.root) / f"{run_id}.json"
    try:
        info = path.lstat()
        if path.is_symlink() or not path.is_file() or info.st_size > _MAX_RESOLVED_CONFIG_BYTES:
            raise StorageError("resolved configuration is not a bounded regular file")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or payload.get("run_id") != run_id:
            raise StorageError("resolved configuration run id does not match")
        raw_config = payload.get("config")
        resolved_sha256 = payload.get("resolved_sha256")
        if not isinstance(raw_config, dict) or not isinstance(resolved_sha256, str):
            raise StorageError("resolved configuration payload is incomplete")
        raw_digest = hashlib.sha256(
            json.dumps(
                raw_config,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if resolved_sha256 != raw_digest:
            raise StorageError("resolved configuration digest does not match its content")
        normalized = _migrate_legacy_resolved_config(raw_config)
        config = WorkflowConfig.model_validate(normalized)
    except StorageError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise StorageError("unable to load resolved workflow configuration") from exc
    config, path_relocation = _relocate_resolved_config(config, profile, run_id)
    if config.storage.root.resolve() != profile.profile.storage.root.resolve():
        raise StorageError("resolved configuration storage root differs from the profile")
    return LoadedConfig(
        config=config,
        source_path=profile.source_path.resolve(),
        sha256=resolved_sha256,
        path_relocation=path_relocation,
    )


def _migrate_legacy_resolved_config(raw_config: dict[str, object]) -> dict[str, object]:
    """Hydrate resolved schema 2 without changing its persisted digest."""

    normalized = deepcopy(raw_config)
    if normalized.get("schema_version") != 2:
        return normalized
    limits = normalized.get("limits")
    if not isinstance(limits, dict) or "max_turns" not in limits:
        raise StorageError("resolved schema 2 configuration has invalid execution limits")
    if "max_model_invocations" in limits or "claude_max_turns" in limits:
        raise StorageError("resolved configuration mixes legacy and current execution limits")
    legacy_max_turns = limits.pop("max_turns")
    limits["max_model_invocations"] = legacy_max_turns
    limits["claude_max_turns"] = legacy_max_turns
    normalized["schema_version"] = 3
    return normalized


def _relocate_resolved_config(
    config: WorkflowConfig, profile: LoadedProfile, run_id: str
) -> tuple[WorkflowConfig, ConfigPathRelocation | None]:
    """Rebase structurally verified generated-run paths after the profile directory moves."""

    current_storage = profile.profile.storage.root.resolve()
    stored_storage = config.storage.root.resolve()
    if stored_storage == current_storage:
        return config, None

    current_profile_root = profile.source_path.resolve().parent
    current_output = profile.profile.output.root.resolve()
    try:
        storage_suffix = current_storage.relative_to(current_profile_root)
        output_suffix = current_output.relative_to(current_profile_root)
    except ValueError as exc:
        raise StorageError("resolved configuration storage root differs from the profile") from exc
    if not storage_suffix.parts or not output_suffix.parts:
        raise StorageError("resolved configuration paths cannot be safely relocated")

    stored_suffix = Path(*stored_storage.parts[-len(storage_suffix.parts) :])
    if stored_suffix != storage_suffix:
        raise StorageError("resolved configuration storage root differs from the profile")
    previous_profile_root = stored_storage
    for _part in storage_suffix.parts:
        previous_profile_root = previous_profile_root.parent

    expected_output = (previous_profile_root / output_suffix).resolve()
    generated_suffix = Path("generated-projects") / run_id / "source"
    expected_repository = (stored_storage / generated_suffix).resolve()
    if config.output.root.resolve() != expected_output:
        raise StorageError("resolved configuration output root cannot be safely relocated")
    if config.repository.path.resolve() != expected_repository:
        raise StorageError("resolved configuration repository cannot be safely relocated")

    relocated = config.model_copy(
        update={
            "repository": config.repository.model_copy(
                update={"path": (current_storage / generated_suffix).resolve()}
            ),
            "storage": config.storage.model_copy(update={"root": current_storage}),
            "output": config.output.model_copy(update={"root": current_output}),
        }
    )
    return relocated, ConfigPathRelocation(
        previous_storage_root=stored_storage,
        previous_repository_path=config.repository.path.resolve(),
    )


def _repository_root(repository: Path) -> Path:
    try:
        selected = repository.resolve(strict=True)
    except OSError as exc:
        raise ConfigurationError("managed repository does not exist") from exc
    if not selected.is_dir():
        raise ConfigurationError("managed repository is not a directory")
    reported = Path(_git_text(selected, "rev-parse", "--show-toplevel")).resolve(strict=True)
    if reported != selected:
        raise PreflightError(
            "run the command from the Git top-level directory",
            context={"selected": str(selected), "top_level": str(reported)},
        )
    return selected


def _git_text(repository: Path, *arguments: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise PreflightError("Git is unavailable")
    try:
        completed = subprocess.run(  # noqa: S603 - resolved Git with fixed internal arguments
            [git, "-C", str(repository), *arguments],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PreflightError("unable to inspect the managed Git repository") from exc
    if len(completed.stdout) + len(completed.stderr) > _MAX_GIT_OUTPUT_BYTES:
        raise PreflightError("managed Git repository inspection exceeded its output limit")
    if completed.returncode != 0:
        raise PreflightError(
            "managed Git repository inspection failed",
            context={"reason": completed.stderr.decode("utf-8", errors="replace")[:500]},
        )
    try:
        return completed.stdout.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise PreflightError("managed Git repository inspection output is not UTF-8") from exc


def _resolved_directory(storage_root: Path) -> Path:
    root = storage_root.resolve()
    directory = root / "resolved-configs"
    try:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if directory.is_symlink():
            raise StorageError("resolved-configs directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        resolved = directory.resolve(strict=True)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("unable to prepare resolved-configs directory") from exc
    if resolved.parent != root:
        raise StorageError("resolved-configs directory escaped the storage root")
    return resolved
