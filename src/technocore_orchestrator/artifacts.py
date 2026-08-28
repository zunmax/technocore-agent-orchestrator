"""Export one human-readable run folder without copying workflow secrets."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath
from typing import Any

from technocore_orchestrator.config import LoadedConfig
from technocore_orchestrator.domain.models import RUN_ID_RE, SHA256_RE, Role
from technocore_orchestrator.errors import StorageError
from technocore_orchestrator.reporting import Redactor, ReportArtifacts
from technocore_orchestrator.storage import SQLiteStore

ARTIFACT_VERSION = 1
MAX_EXPORTED_FILES = 10_000
MAX_EXPORTED_BYTES = 100 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class OutputArtifacts:
    directory: Path
    code_directory: Path
    agent_output_directory: Path
    report_directory: Path
    manifest: Path


def export_run_output(
    *,
    store: SQLiteStore,
    loaded_config: LoadedConfig,
    run_id: str,
    reports: ReportArtifacts,
    secret_values: tuple[str, ...] = (),
    generated_at: datetime | None = None,
) -> OutputArtifacts:
    """Create one timestamped output folder with changed code and structured agent results."""

    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("output run id is invalid")
    run = store.get_run(run_id)
    timestamp = (generated_at or run.created_at).astimezone().strftime("%Y-%m-%d_%H-%M-%S_%z")
    root = _prepare_output_root(
        loaded_config.config.output.root,
        repository=loaded_config.config.repository.path,
    )
    name = f"{run.task_id}__{timestamp}__{run_id}"
    target = root / name
    if target.exists() or target.is_symlink():
        return _existing_output(target, run_id)

    temporary = Path(tempfile.mkdtemp(prefix=f".{name}.", dir=root)).resolve(strict=True)
    try:
        code_directory = temporary / "code"
        agent_directory = temporary / "agent-outputs"
        report_directory = temporary / "reports"
        code_directory.mkdir()
        agent_directory.mkdir()
        report_directory.mkdir()
        redactor = Redactor(secret_values=secret_values)
        report_payload = _read_report_payload(reports.run_json)
        code_files = _export_changed_code(
            store,
            loaded_config,
            run_id,
            report_payload,
            code_directory,
        )
        agent_files = _export_agent_results(
            store,
            loaded_config,
            run_id,
            agent_directory,
            redactor,
        )
        report_files = _copy_reports(reports, report_directory)
        manifest_payload = {
            "artifact_version": ARTIFACT_VERSION,
            "run_id": run_id,
            "task_id": run.task_id,
            "created_at": run.created_at.isoformat(),
            "exported_at": (generated_at or datetime.now().astimezone()).isoformat(),
            "state": run.state.value,
            "repository": {
                "base_commit": run.base_commit,
                "candidate_commit": report_payload["repository"]["candidate_commit"],
            },
            "code": code_files,
            "agent_outputs": agent_files,
            "reports": report_files,
        }
        _write_json(temporary / "artifact-manifest.json", manifest_payload)
        temporary.replace(target)
    except Exception:
        _remove_temporary_directory(temporary, root)
        raise
    return OutputArtifacts(
        directory=target,
        code_directory=target / "code",
        agent_output_directory=target / "agent-outputs",
        report_directory=target / "reports",
        manifest=target / "artifact-manifest.json",
    )


def _export_changed_code(
    store: SQLiteStore,
    loaded: LoadedConfig,
    run_id: str,
    report: dict[str, Any],
    destination: Path,
) -> list[dict[str, Any]]:
    repository = report.get("repository")
    if not isinstance(repository, dict):
        raise StorageError("run report does not contain repository evidence")
    candidate = repository.get("candidate_commit")
    changed_paths = repository.get("changed_paths")
    if candidate is None:
        return []
    if not isinstance(candidate, str) or not isinstance(changed_paths, list):
        raise StorageError("run report repository evidence is malformed")
    if len(changed_paths) > MAX_EXPORTED_FILES:
        raise StorageError("changed code exceeds the output file-count limit")
    worktree = store.get_worktree(run_id, Role.IMPLEMENTER)
    observation = store.get_latest_worktree_observation(run_id, Role.IMPLEMENTER)
    if observation.head_commit != candidate:
        raise StorageError("implementer worktree no longer matches the reported candidate")
    canonical_report_paths = tuple(_safe_relative_path(path).as_posix() for path in changed_paths)
    if observation.changed_paths and observation.changed_paths != canonical_report_paths:
        raise StorageError("reported changed paths differ from durable candidate evidence")
    root = worktree.path.resolve(strict=True)
    records: list[dict[str, Any]] = []
    total_bytes = 0
    for raw_path in changed_paths:
        relative = _safe_relative_path(raw_path)
        source = root.joinpath(*relative.parts)
        output = destination.joinpath(*relative.parts)
        if not source.exists() and not source.is_symlink():
            records.append({"path": relative.as_posix(), "status": "deleted"})
            continue
        try:
            resolved = source.resolve(strict=True)
            info = source.lstat()
        except OSError as exc:
            raise StorageError("unable to inspect changed output code") from exc
        if source.is_symlink() or not source.is_file() or not resolved.is_relative_to(root):
            raise StorageError("changed output code must be a contained regular file")
        total_bytes += info.st_size
        if total_bytes > MAX_EXPORTED_BYTES:
            raise StorageError("changed code exceeds the 100 MiB output limit")
        output.parent.mkdir(parents=True, exist_ok=True)
        digest = _copy_and_hash(source, output)
        records.append(
            {
                "path": relative.as_posix(),
                "status": "present",
                "bytes": info.st_size,
                "sha256": digest,
            }
        )
    return records


def _export_agent_results(
    store: SQLiteStore,
    loaded: LoadedConfig,
    run_id: str,
    destination: Path,
    redactor: Redactor,
) -> list[dict[str, Any]]:
    role_harness = {
        Role.PLANNER: loaded.config.roles.planner.value,
        Role.IMPLEMENTER: loaded.config.roles.implementer.value,
        Role.REVIEWER: loaded.config.roles.reviewer.value,
    }
    records: list[dict[str, Any]] = []
    for index, stored in enumerate(store.list_role_results(run_id), start=1):
        payload = redactor.value(
            {
                "role": stored.role.value,
                "harness": role_harness[stored.role],
                "attempt": stored.attempt,
                "created_at": stored.created_at.isoformat(),
                "content_sha256": stored.content_sha256,
                "result": stored.result.model_dump(mode="json"),
            }
        )
        name = f"{index:02d}-{stored.role.value}-attempt-{stored.attempt}.json"
        path = destination / name
        encoded = _write_json(path, payload)
        records.append({"path": name, "bytes": len(encoded), "sha256": _sha256(encoded)})
    return records


def _copy_reports(reports: ReportArtifacts, destination: Path) -> list[dict[str, Any]]:
    sources = (
        (reports.run_json, reports.run_json_sha256),
        (reports.events_jsonl, reports.events_jsonl_sha256),
        (reports.conversation_jsonl, reports.conversation_jsonl_sha256),
        (reports.report_markdown, reports.report_markdown_sha256),
    )
    records: list[dict[str, Any]] = []
    for source, expected_digest in sources:
        output = destination / source.name
        digest = _copy_and_hash(source, output)
        if digest != expected_digest:
            raise StorageError("report changed while the output folder was being exported")
        records.append({"path": source.name, "bytes": output.stat().st_size, "sha256": digest})
    return records


def _prepare_output_root(root_input: Path, *, repository: Path) -> Path:
    try:
        root_path = root_input.absolute()
        if root_path.is_symlink():
            raise StorageError("output root must not be a symlink")
        root = root_path.resolve()
        repository_root = repository.resolve(strict=True)
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("unable to prepare output root") from exc
    if root == repository_root or root.is_relative_to(repository_root):
        raise StorageError("output root must be outside the source repository")
    return root


def _existing_output(target: Path, run_id: str) -> OutputArtifacts:
    if _is_link_like(target) or not target.is_dir() or target.resolve(strict=True) != target:
        raise StorageError("output directory must be a regular directory, not a symlink")
    manifest = target / "artifact-manifest.json"
    try:
        if manifest.stat().st_size > MAX_MANIFEST_BYTES:
            raise StorageError("output manifest exceeds its size limit")
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError("output directory already exists without a valid manifest") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("artifact_version") != ARTIFACT_VERSION
        or payload.get("run_id") != run_id
    ):
        raise StorageError("output directory collision belongs to another run")
    _verify_existing_output(target, payload)
    return OutputArtifacts(
        directory=target,
        code_directory=target / "code",
        agent_output_directory=target / "agent-outputs",
        report_directory=target / "reports",
        manifest=manifest,
    )


def _verify_existing_output(target: Path, manifest: dict[str, Any]) -> None:
    expected_files = {"artifact-manifest.json"}
    expected_directories = {"code", "agent-outputs", "reports"}
    sections = (
        ("code", "code", True),
        ("agent_outputs", "agent-outputs", False),
        ("reports", "reports", False),
    )
    for section_name, directory_name, allows_deleted in sections:
        records = manifest.get(section_name)
        if not isinstance(records, list) or len(records) > MAX_EXPORTED_FILES:
            raise StorageError("output manifest contains an invalid file section")
        seen: set[str] = set()
        for record in records:
            relative, present = _verify_manifest_record(
                target / directory_name,
                record,
                allows_deleted=allows_deleted,
            )
            value = relative.as_posix()
            if value in seen:
                raise StorageError("output manifest contains a duplicate file path")
            seen.add(value)
            if not present:
                continue
            exported = f"{directory_name}/{value}"
            expected_files.add(exported)
            parent = PurePosixPath(exported).parent
            while parent.as_posix() != ".":
                expected_directories.add(parent.as_posix())
                parent = parent.parent
    actual_files, actual_directories = _output_tree_entries(target)
    if actual_files != expected_files or actual_directories != expected_directories:
        raise StorageError("output directory contents differ from its manifest")


def _verify_manifest_record(
    root: Path,
    record: object,
    *,
    allows_deleted: bool,
) -> tuple[PurePosixPath, bool]:
    if not isinstance(record, dict):
        raise StorageError("output manifest file record is invalid")
    relative = _safe_relative_path(record.get("path"))
    target = root.joinpath(*relative.parts)
    deleted = allows_deleted and record.get("status") == "deleted"
    if deleted:
        if set(record) != {"path", "status"}:
            raise StorageError("deleted output manifest record is invalid")
        if target.exists() or target.is_symlink():
            raise StorageError("deleted output path unexpectedly exists")
        return relative, False
    expected_keys = {"path", "bytes", "sha256"}
    if allows_deleted:
        expected_keys.add("status")
        if record.get("status") != "present":
            raise StorageError("output manifest code status is invalid")
    size = record.get("bytes")
    digest = record.get("sha256")
    if (
        set(record) != expected_keys
        or isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 <= size <= MAX_EXPORTED_BYTES
        or not isinstance(digest, str)
        or not SHA256_RE.fullmatch(digest)
    ):
        raise StorageError("output manifest file identity is invalid")
    try:
        info = target.lstat()
        resolved = target.resolve(strict=True)
    except OSError as exc:
        raise StorageError("output manifest file is missing") from exc
    if (
        _is_link_like(target)
        or not stat.S_ISREG(info.st_mode)
        or not resolved.is_relative_to(root)
        or info.st_size != size
        or _sha256_file(target) != digest
    ):
        raise StorageError("output file differs from its manifest identity")
    return relative, True


def _output_tree_entries(root: Path) -> tuple[set[str], set[str]]:
    files: set[str] = set()
    directories: set[str] = set()
    try:
        for current, names, file_names in os.walk(root, followlinks=False):
            current_path = Path(current)
            for name in names:
                path = current_path / name
                if _is_link_like(path):
                    raise StorageError("output directory contains a link or reparse point")
                directories.add(path.relative_to(root).as_posix())
            for name in file_names:
                path = current_path / name
                if _is_link_like(path) or not path.is_file():
                    raise StorageError("output directory contains a non-regular file")
                files.add(path.relative_to(root).as_posix())
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError("unable to verify the existing output directory") from exc
    return files, directories


def _is_link_like(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError:
        return False
    reparse = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(info, "st_file_attributes", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse)


def _read_report_payload(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise StorageError("unable to read generated run report") from exc
    if not isinstance(payload, dict):
        raise StorageError("generated run report must contain a JSON object")
    return payload


def _safe_relative_path(raw: object) -> PurePosixPath:
    if not isinstance(raw, str) or not raw or "\\" in raw or "\x00" in raw:
        raise StorageError("changed output path is invalid")
    path = PurePosixPath(raw)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise StorageError("changed output path escapes its output folder")
    return path


def _copy_and_hash(source: Path, destination: Path) -> str:
    digest = hashlib.sha256()
    try:
        with source.open("rb") as reader, destination.open("xb") as writer:
            while chunk := reader.read(64 * 1024):
                writer.write(chunk)
                digest.update(chunk)
            writer.flush()
            os.fsync(writer.fileno())
    except OSError as exc:
        raise StorageError("unable to copy output artifact") from exc
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> bytes:
    encoded = (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode(
        "utf-8"
    )
    try:
        with path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StorageError("unable to write output artifact") from exc
    return encoded


def _remove_temporary_directory(path: Path, root: Path) -> None:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return
    if resolved.parent == root and resolved.name.startswith("."):
        shutil.rmtree(resolved)


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
