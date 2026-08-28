from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from technocore_orchestrator.config import load_profile
from technocore_orchestrator.errors import PreflightError, StorageError
from technocore_orchestrator.profile import (
    load_resolved_config,
    persist_resolved_config,
    resolve_profile,
)


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


def _repository(tmp_path: Path) -> tuple[Path, str]:
    repository = tmp_path / "product"
    return _initialize_repository(repository)


def _initialize_repository(repository: Path) -> tuple[Path, str]:
    repository.mkdir(parents=True)
    (repository / "product.txt").write_text("baseline\n", encoding="utf-8")
    _git(repository, "init")
    _git(repository, "config", "core.autocrlf", "false")
    _git(repository, "config", "user.name", "Profile Test")
    _git(repository, "config", "user.email", "profile-test@localhost")
    _git(repository, "add", "product.txt")
    _git(repository, "commit", "-m", "baseline")
    return repository, _git(repository, "rev-parse", "HEAD")


def _profile(tmp_path: Path, *, prompt: str = "Build a useful product.") -> Path:
    path = tmp_path / "workflow.toml"
    path.write_text(
        f'''\
schema_version = 4

[task]
prompt = "{prompt}"

[roles]
planner = "codex"
implementer = "claude"
reviewer = "codex"

[providers.codex]
executable = 'C:\\tools\\codex.exe'
model = "gpt-test"
expected_version = "1.2.3"

[providers.claude]
executable = 'C:\\tools\\claude.exe'
model = "claude-test"
expected_version = "4.5.6"

[storage]
root = "state"

[output]
root = "output"
''',
        encoding="utf-8",
    )
    return path


def test_profile_resolves_current_repository_head_and_generic_acceptance(tmp_path: Path) -> None:
    repository, head = _repository(tmp_path)
    profile = load_profile(_profile(tmp_path))

    loaded = resolve_profile(profile, repository)

    assert loaded.config.repository.path == repository.resolve()
    assert loaded.config.repository.base_commit == head
    assert loaded.config.repository.allowed_paths == (".",)
    assert loaded.config.task.brief == "Build a useful product."
    assert loaded.config.task.id.startswith("task_")
    assert len(loaded.config.task.acceptance_criteria) == 3
    check = loaded.config.verification.commands[0]
    assert check.id == "git_diff_check"
    assert check.argv[-1] == f"{head}..HEAD"
    assert "cr-at-eol" in check.argv[2]


def test_resolved_config_survives_later_prompt_edits(tmp_path: Path) -> None:
    repository, _head = _repository(tmp_path)
    path = _profile(tmp_path)
    profile = load_profile(path)
    loaded = resolve_profile(profile, repository)
    persist_resolved_config(loaded, profile, "run_profile01")

    path.write_text(
        path.read_text(encoding="utf-8").replace("useful", "different"), encoding="utf-8"
    )
    restored = load_resolved_config(load_profile(path), "run_profile01")

    assert restored.sha256 == loaded.sha256
    assert restored.path_relocation is None
    assert restored.config.task.brief == "Build a useful product."


def test_pre_v4_resolved_execution_limit_is_migrated_without_digest_drift(
    tmp_path: Path,
) -> None:
    repository, _head = _repository(tmp_path)
    profile = load_profile(_profile(tmp_path))
    loaded = resolve_profile(profile, repository)
    path = persist_resolved_config(loaded, profile, "run_legacy01")
    payload = json.loads(path.read_text(encoding="utf-8"))
    limits = payload["config"]["limits"]
    assert limits.pop("max_model_invocations") == limits.pop("claude_max_turns") == 20
    limits["max_turns"] = 20
    payload["config"]["schema_version"] = 2
    canonical = json.dumps(
        payload["config"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    payload["resolved_sha256"] = hashlib.sha256(canonical).hexdigest()
    path.write_text(json.dumps(payload), encoding="utf-8")

    restored = load_resolved_config(profile, "run_legacy01")

    assert restored.sha256 == payload["resolved_sha256"]
    assert restored.config.schema_version == 3
    assert restored.config.limits.max_model_invocations == 20
    assert restored.config.limits.claude_max_turns == 20


def test_resolved_config_rebases_generated_project_after_profile_directory_moves(
    tmp_path: Path,
) -> None:
    run_id = "run_relocate1"
    previous_root = tmp_path / "previous"
    previous_root.mkdir()
    repository, _head = _initialize_repository(
        previous_root / "state" / "generated-projects" / run_id / "source"
    )
    profile = load_profile(_profile(previous_root))
    loaded = resolve_profile(profile, repository)
    persist_resolved_config(loaded, profile, run_id)

    current_root = tmp_path / "current"
    previous_root.rename(current_root)
    restored = load_resolved_config(load_profile(current_root / "workflow.toml"), run_id)

    assert restored.sha256 == loaded.sha256
    assert restored.path_relocation is not None
    assert restored.path_relocation.previous_storage_root == (previous_root / "state").resolve()
    assert (
        restored.config.repository.path
        == (current_root / "state" / "generated-projects" / run_id / "source").resolve()
    )
    assert restored.config.storage.root == (current_root / "state").resolve()
    assert restored.config.output.root == (current_root / "output").resolve()


def test_resolved_config_relocation_rejects_an_external_repository(tmp_path: Path) -> None:
    run_id = "run_relocate2"
    previous_root = tmp_path / "previous"
    previous_root.mkdir()
    repository, _head = _initialize_repository(tmp_path / "external-product")
    profile = load_profile(_profile(previous_root))
    loaded = resolve_profile(profile, repository)
    persist_resolved_config(loaded, profile, run_id)

    current_root = tmp_path / "current"
    previous_root.rename(current_root)

    with pytest.raises(StorageError, match="repository cannot be safely relocated"):
        load_resolved_config(load_profile(current_root / "workflow.toml"), run_id)


def test_profile_requires_launch_from_git_top_level(tmp_path: Path) -> None:
    repository, _head = _repository(tmp_path)
    nested = repository / "nested"
    nested.mkdir()

    with pytest.raises(PreflightError, match="Git top-level"):
        resolve_profile(load_profile(_profile(tmp_path)), nested)
