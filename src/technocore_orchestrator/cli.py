"""Command-line entry point for supervised Technocore workflow operations."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from technocore_orchestrator import __version__
from technocore_orchestrator.artifacts import export_run_output
from technocore_orchestrator.config import (
    LoadedConfig,
    LoadedProfile,
    load_profile,
)
from technocore_orchestrator.domain.models import RUN_ID_RE, HarnessKind, Role
from technocore_orchestrator.errors import (
    ConfigurationError,
    ExitCode,
    PreflightError,
    StateError,
    StorageError,
    WorkflowError,
)
from technocore_orchestrator.evaluation import compare_run_reports
from technocore_orchestrator.execution import TrustedExecutable
from technocore_orchestrator.identity import (
    RoleIdentity,
    create_protected_identity,
    load_protected_identity,
)
from technocore_orchestrator.managed_project import create_generated_project_repository
from technocore_orchestrator.network import require_loopback_technocore_listener
from technocore_orchestrator.operations import cleanup_run
from technocore_orchestrator.profile import (
    load_resolved_config,
    persist_resolved_config,
    resolve_profile,
)
from technocore_orchestrator.real_runtime import (
    RealRuntime,
    build_real_runtime,
    identity_path,
    read_room_capability_for_run,
    room_hash_for_run,
)
from technocore_orchestrator.reporting import build_status_payload, generate_reports
from technocore_orchestrator.runtime import (
    build_fake_orchestrator,
    new_run_id,
    request_active_cancellation,
    run_with_control,
)
from technocore_orchestrator.storage import SQLiteStore
from technocore_orchestrator.technocore import TechnocoreClient
from technocore_orchestrator.viewer import TechnocoreTimeline
from technocore_orchestrator.web_viewer import open_and_serve_conversation_viewer
from technocore_orchestrator.worktrees import WorktreeManager

PROBE_TIMEOUT_SECONDS = 5


@dataclass(frozen=True, slots=True)
class ToolProbe:
    name: str
    required: bool
    found: bool
    executable: str | None
    version: str | None
    error: str | None


_PROBE_ARGS = {
    "git": ("--version",),
    "uv": ("--version",),
    "docker": ("--version",),
    "codex": ("--version",),
    "claude": ("--version",),
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="technocore-orchestrator", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser("validate-config", help="validate a secret-free run config")
    validate.add_argument("config", type=Path)
    validate.add_argument("--json", action="store_true", dest="as_json")

    doctor = subparsers.add_parser("doctor", help="probe prerequisites without invoking models")
    doctor.add_argument("--config", type=Path)
    doctor.add_argument("--json", action="store_true", dest="as_json")

    identity = subparsers.add_parser(
        "identity-create", help="create one DPAPI-protected Technocore role identity"
    )
    identity.add_argument("role", choices=("supervisor", "planner", "implementer", "reviewer"))
    identity.add_argument("--config", type=Path, required=True)

    run = subparsers.add_parser("run", help="start a supervised workflow")
    run.add_argument("--config", type=Path, required=True)
    run.add_argument("--run-id")
    run.add_argument("--allow-model-invocations", action="store_true")
    run.add_argument("--json", action="store_true", dest="as_json")

    resume = subparsers.add_parser("resume", help="reconcile and continue a durable run")
    resume.add_argument("run_id")
    resume.add_argument("--config", type=Path, required=True)
    resume.add_argument("--allow-model-invocations", action="store_true")
    resume.add_argument("--json", action="store_true", dest="as_json")

    status = subparsers.add_parser("status", help="show durable run state and recent events")
    status.add_argument("run_id")
    status.add_argument("--config", type=Path, required=True)
    status.add_argument("--recent", type=int, default=5)
    status.add_argument("--json", action="store_true", dest="as_json")

    view = subparsers.add_parser("view", help="open the verified conversation in a local UI")
    view.add_argument("run_id")
    view.add_argument("--config", type=Path, required=True)
    view.add_argument("--port", type=int, default=0)
    view.add_argument("--no-open", action="store_true")
    view.add_argument("--startup-timeout", type=float, default=0.0)

    report = subparsers.add_parser("report", help="regenerate redacted local run reports")
    report.add_argument("run_id")
    report.add_argument("--config", type=Path, required=True)
    report.add_argument("--json", action="store_true", dest="as_json")

    compare = subparsers.add_parser(
        "compare-reports", help="compare matched baseline and Technocore quality evidence"
    )
    compare.add_argument("mode_a", type=Path)
    compare.add_argument("mode_b", type=Path)
    compare.add_argument("--seeded-criterion", action="append", default=[])
    compare.add_argument("--json", action="store_true", dest="as_json")

    cancel = subparsers.add_parser("cancel", help="cancel an active or idle durable run")
    cancel.add_argument("run_id")
    cancel.add_argument("--config", type=Path, required=True)
    cancel.add_argument("--json", action="store_true", dest="as_json")

    cleanup = subparsers.add_parser(
        "cleanup", help="inspect or remove clean recognized worktrees from a terminal run"
    )
    cleanup.add_argument("run_id")
    cleanup.add_argument("--config", type=Path, required=True)
    mode = cleanup.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True)
    mode.add_argument("--apply", action="store_false", dest="dry_run")
    cleanup.add_argument("--json", action="store_true", dest="as_json")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "validate-config":
            return _validate_config(args.config, as_json=args.as_json)
        if args.command == "doctor":
            return _doctor(args.config, as_json=args.as_json)
        if args.command == "identity-create":
            return _identity_create(args.config, Role(args.role))
        if args.command == "run":
            return _execute_workflow(
                args.config,
                run_id=args.run_id,
                resume=False,
                allow_model_invocations=args.allow_model_invocations,
                as_json=args.as_json,
            )
        if args.command == "resume":
            return _execute_workflow(
                args.config,
                run_id=args.run_id,
                resume=True,
                allow_model_invocations=args.allow_model_invocations,
                as_json=args.as_json,
            )
        if args.command == "status":
            return _status(args.config, args.run_id, recent=args.recent, as_json=args.as_json)
        if args.command == "view":
            return _view(
                args.config,
                args.run_id,
                port=args.port,
                open_browser=not args.no_open,
                startup_timeout_seconds=args.startup_timeout,
            )
        if args.command == "report":
            return _report(args.config, args.run_id, as_json=args.as_json)
        if args.command == "compare-reports":
            return _compare_reports(
                args.mode_a,
                args.mode_b,
                seeded_criteria=tuple(args.seeded_criterion),
                as_json=args.as_json,
            )
        if args.command == "cancel":
            return _cancel(args.config, args.run_id, as_json=args.as_json)
        if args.command == "cleanup":
            return _cleanup(
                args.config,
                args.run_id,
                dry_run=args.dry_run,
                as_json=args.as_json,
            )
        parser.error(f"unknown command: {args.command}")
    except WorkflowError as exc:
        print(f"error[{exc.category}]: {exc.message}", file=sys.stderr)
        if exc.context:
            print(json.dumps(exc.context, ensure_ascii=False, sort_keys=True), file=sys.stderr)
        return int(exc.exit_code)
    except KeyboardInterrupt:
        print("error[state]: workflow cancellation was requested", file=sys.stderr)
        return int(ExitCode.STATE)
    except Exception:
        print("error[internal]: unexpected internal failure", file=sys.stderr)
        return int(ExitCode.INTERNAL)
    return int(ExitCode.INTERNAL)


def _validate_config(path: Path, *, as_json: bool) -> int:
    loaded = load_profile(path)
    payload = {
        "valid": True,
        "source": str(loaded.source_path),
        "sha256": loaded.sha256,
        "schema_version": loaded.profile.schema_version,
        "mode": "reusable-workflow-profile",
    }
    _print_payload(payload, as_json=as_json)
    return int(ExitCode.SUCCESS)


def _doctor(config_path: Path | None, *, as_json: bool) -> int:
    loaded = load_profile(config_path) if config_path else None
    required = _required_tools(loaded)
    probes = tuple(
        _probe_tool(
            name,
            required=name in required,
            executable=_configured_probe_executable(loaded, name),
        )
        for name in _PROBE_ARGS
    )
    platform_ok = sys.version_info[:2] == (3, 12)
    payload: dict[str, Any] = {
        "ok": platform_ok and all(probe.found for probe in probes if probe.required),
        "application_version": __version__,
        "python": platform.python_version(),
        "python_supported": platform_ok,
        "platform": platform.platform(),
        "reference_environment": "Windows 11",
        "tools": [asdict(probe) for probe in probes],
    }
    if loaded:
        payload["config_sha256"] = loaded.sha256
    _print_payload(payload, as_json=as_json)
    return int(ExitCode.SUCCESS if payload["ok"] else ExitCode.PREFLIGHT)


def _identity_create(config_path: Path, role: Role) -> int:
    loaded = load_profile(config_path)
    path = identity_path(loaded.profile.storage.root, role)
    public = create_protected_identity(path)
    _print_payload({"role": role.value, "path": str(path), "did": public.did}, as_json=False)
    return int(ExitCode.SUCCESS)


def _execute_workflow(
    config_path: Path,
    *,
    run_id: str | None,
    resume: bool,
    allow_model_invocations: bool = False,
    as_json: bool = False,
) -> int:
    selected_run_id = run_id or new_run_id()
    if not RUN_ID_RE.fullmatch(selected_run_id):
        raise ConfigurationError("run id does not match the required run_* format")
    profile = load_profile(config_path)
    profile_roles = profile.profile.roles
    fake_profile = all(
        harness is HarnessKind.FAKE
        for harness in (profile_roles.planner, profile_roles.implementer, profile_roles.reviewer)
    )
    if not fake_profile and not allow_model_invocations:
        raise PreflightError("real roles require the explicit --allow-model-invocations flag")
    loaded = (
        load_resolved_config(profile, selected_run_id)
        if resume
        else resolve_profile(
            profile,
            create_generated_project_repository(profile.profile.storage.root, selected_run_id),
        )
    )
    fake_config = _is_fake_config(loaded)
    if fake_config != fake_profile:
        raise ConfigurationError("resolved workflow provider mode differs from the profile")
    if not fake_config:
        _ensure_protected_identities(loaded)
    if not resume:
        persist_resolved_config(loaded, profile, selected_run_id)
    store_context = (
        _open_existing_store(loaded, selected_run_id)
        if resume
        else SQLiteStore.open(_state_database_path(loaded))
    )
    canceled = False
    report_room_hash = room_hash_for_run(loaded.config.storage.root, selected_run_id)
    with store_context as store:
        real: RealRuntime | None = None
        if fake_config:
            orchestrator = build_fake_orchestrator(loaded, store)
        else:
            real = build_real_runtime(
                loaded=loaded,
                store=store,
                run_id=selected_run_id,
                resume=resume,
                load_identity=_load_protected_role_identity,
            )
            orchestrator = real.orchestrator
            report_room_hash = real.room_hash
        try:
            if real is None:
                operation = (
                    orchestrator.resume(selected_run_id)
                    if resume
                    else orchestrator.run(selected_run_id)
                )
                asyncio.run(
                    run_with_control(
                        operation,
                        storage_root=loaded.config.storage.root,
                        run_id=selected_run_id,
                    )
                )
            else:
                asyncio.run(
                    _run_real_operation(
                        real,
                        resume=resume,
                        storage_root=loaded.config.storage.root,
                        run_id=selected_run_id,
                    )
                )
        except asyncio.CancelledError:
            canceled = True
        except KeyboardInterrupt:
            _regenerate_report_if_run_exists(
                store,
                loaded,
                selected_run_id,
                room_hash=report_room_hash,
            )
            raise
        except Exception:
            _regenerate_report_if_run_exists(
                store,
                loaded,
                selected_run_id,
                room_hash=report_room_hash,
            )
            raise
        artifacts = generate_reports(
            store=store,
            loaded_config=loaded,
            run_id=selected_run_id,
            output_root=loaded.config.storage.root / "reports",
            room_hash=report_room_hash,
        )
        state = store.get_run(selected_run_id).state
        output = export_run_output(
            store=store,
            loaded_config=loaded,
            run_id=selected_run_id,
            reports=artifacts,
        )
    payload = {
        "run_id": selected_run_id,
        "state": state.value,
        "cancellation_observed": canceled,
        "run_json": str(artifacts.run_json),
        "report_markdown": str(artifacts.report_markdown),
        "output_directory": str(output.directory),
    }
    _print_payload(payload, as_json=as_json)
    return int(ExitCode.SUCCESS if state.value == "completed" else ExitCode.STATE)


def _cancel(config_path: Path, run_id: str, *, as_json: bool) -> int:
    loaded = _load_existing_config(config_path, run_id)
    with _open_existing_store(loaded, run_id) as store:
        run = store.get_run(run_id)
        if run.state.is_terminal:
            payload = {"run_id": run_id, "state": run.state.value, "requested": False}
            _print_payload(payload, as_json=as_json)
            return int(ExitCode.SUCCESS)
        cancellation_requested = request_active_cancellation(loaded.config.storage.root, run_id)
        if cancellation_requested:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                run = store.get_run(run_id)
                if run.state.is_terminal:
                    break
                time.sleep(0.1)
            if run.state.is_terminal:
                payload = {"run_id": run_id, "state": run.state.value, "requested": True}
                _print_payload(payload, as_json=as_json)
                return int(ExitCode.SUCCESS)
            if request_active_cancellation(loaded.config.storage.root, run_id):
                raise StateError(
                    "the active supervisor did not acknowledge cancellation within five seconds"
                )
        report_room_hash = room_hash_for_run(loaded.config.storage.root, run_id)
        if _is_fake_config(loaded):
            orchestrator = build_fake_orchestrator(loaded, store)
            run = asyncio.run(
                run_with_control(
                    orchestrator.cancel(run_id),
                    storage_root=loaded.config.storage.root,
                    run_id=run_id,
                )
            )
        else:
            real = build_real_runtime(
                loaded=loaded,
                store=store,
                run_id=run_id,
                resume=True,
                load_identity=_load_protected_role_identity,
            )
            report_room_hash = real.room_hash
            run = asyncio.run(
                run_with_control(
                    _cancel_real_operation(real, run_id),
                    storage_root=loaded.config.storage.root,
                    run_id=run_id,
                )
            )
        artifacts = generate_reports(
            store=store,
            loaded_config=loaded,
            run_id=run_id,
            output_root=loaded.config.storage.root / "reports",
            room_hash=report_room_hash,
        )
    _print_payload(
        {
            "run_id": run_id,
            "state": run.state.value,
            "requested": False,
            "run_json": str(artifacts.run_json),
        },
        as_json=as_json,
    )
    return int(ExitCode.SUCCESS)


def _regenerate_report_if_run_exists(
    store: SQLiteStore,
    loaded: LoadedConfig,
    run_id: str,
    *,
    room_hash: str | None = None,
) -> None:
    try:
        run = store.get_run(run_id)
        artifacts = generate_reports(
            store=store,
            loaded_config=loaded,
            run_id=run_id,
            output_root=loaded.config.storage.root / "reports",
            room_hash=room_hash,
        )
        if run.state.is_terminal:
            export_run_output(
                store=store,
                loaded_config=loaded,
                run_id=run_id,
                reports=artifacts,
            )
    except WorkflowError:
        return


async def _run_real_operation(
    runtime: RealRuntime,
    *,
    resume: bool,
    storage_root: Path,
    run_id: str,
) -> Any:
    try:
        participants = await runtime.preflight()
        operation = (
            runtime.orchestrator.resume(run_id, participants=participants)
            if resume
            else runtime.orchestrator.run(run_id, participants=participants)
        )
        return await run_with_control(operation, storage_root=storage_root, run_id=run_id)
    finally:
        await runtime.close()


async def _cancel_real_operation(runtime: RealRuntime, run_id: str):
    try:
        return await runtime.orchestrator.cancel(run_id)
    finally:
        await runtime.close()


def _load_protected_role_identity(_role: Role, path: Path) -> RoleIdentity:
    return load_protected_identity(path)


def _ensure_protected_identities(loaded: LoadedConfig) -> None:
    identities: list[RoleIdentity] = []
    for role in (Role.SUPERVISOR, Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER):
        path = identity_path(loaded.config.storage.root, role)
        if not path.exists() and not path.is_symlink():
            create_protected_identity(path)
        identities.append(load_protected_identity(path))
    if len({identity.public.did for identity in identities}) != len(identities):
        raise ConfigurationError("workflow roles must use four distinct Technocore identities")


def _load_existing_config(path: Path, run_id: str) -> LoadedConfig:
    return load_resolved_config(load_profile(path), run_id)


def _open_existing_store(loaded: LoadedConfig, run_id: str) -> SQLiteStore:
    store = SQLiteStore.open(_existing_database_path(loaded))
    try:
        relocation = loaded.path_relocation
        if relocation is not None:
            store.relocate_run_paths(
                run_id=run_id,
                previous_storage_root=relocation.previous_storage_root,
                current_storage_root=loaded.config.storage.root,
                previous_repository_path=relocation.previous_repository_path,
                current_repository_path=loaded.config.repository.path,
            )
        return store
    except Exception:
        store.close()
        raise


def _is_fake_config(loaded: LoadedConfig) -> bool:
    roles = loaded.config.roles
    return all(
        harness is HarnessKind.FAKE
        for harness in (roles.planner, roles.implementer, roles.reviewer)
    )


def _status(config_path: Path, run_id: str, *, recent: int, as_json: bool) -> int:
    loaded = _load_existing_config(config_path, run_id)
    with _open_existing_store(loaded, run_id) as store:
        try:
            payload = build_status_payload(store, run_id, recent=recent)
        except ValueError as exc:
            raise ConfigurationError(str(exc)) from exc
    _print_payload(payload, as_json=as_json)
    return int(ExitCode.SUCCESS)


def _view(
    config_path: Path,
    run_id: str,
    *,
    port: int,
    open_browser: bool,
    startup_timeout_seconds: float,
) -> int:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ConfigurationError("run id does not match the required run_* format")
    profile = load_profile(config_path)

    def snapshot_reader(after_sequence: int) -> dict[str, Any]:
        return _conversation_snapshot(profile, run_id, after_sequence)

    open_and_serve_conversation_viewer(
        run_id,
        snapshot_reader,
        port=port,
        open_browser=open_browser,
        startup_timeout_seconds=startup_timeout_seconds,
    )
    return int(ExitCode.SUCCESS)


def _conversation_snapshot(
    profile: LoadedProfile,
    run_id: str,
    after_sequence: int,
) -> dict[str, Any]:
    storage_root = profile.profile.storage.root.resolve()
    resolved_path = storage_root / "resolved-configs" / f"{run_id}.json"
    room_path = storage_root / "rooms" / f"{run_id}.room"
    database_path = storage_root / "state.sqlite3"
    if not resolved_path.is_file() or not room_path.is_file() or not database_path.is_file():
        return _waiting_conversation_snapshot(after_sequence)
    loaded = load_resolved_config(profile, run_id)
    if _is_fake_config(loaded):
        raise ConfigurationError("conversation UI requires a real Technocore workflow")
    room = read_room_capability_for_run(loaded.config.storage.root, run_id)
    with _open_existing_store(loaded, run_id) as store:
        try:
            store.get_run(run_id)
        except StorageError as exc:
            if exc.message == "run does not exist":
                return _waiting_conversation_snapshot(after_sequence)
            raise
        return asyncio.run(
            _read_conversation_snapshot(
                loaded,
                store,
                run_id,
                room,
                after_sequence=after_sequence,
            )
        )


def _waiting_conversation_snapshot(after_sequence: int) -> dict[str, Any]:
    return {
        "state": "waiting_for_run",
        "terminal": False,
        "cursor": after_sequence,
        "at_limit": False,
        "entries": [],
    }


async def _read_conversation_snapshot(
    loaded: LoadedConfig,
    store: SQLiteStore,
    run_id: str,
    room: str,
    *,
    after_sequence: int,
) -> dict[str, Any]:
    client = TechnocoreClient(loaded.config.technocore)
    try:
        await client.health()
        version = await client.manifest_version()
        if version != loaded.config.technocore.expected_version:
            raise PreflightError(
                "Technocore service version is unsupported",
                context={
                    "expected": loaded.config.technocore.expected_version,
                    "installed": version,
                },
            )
        require_loopback_technocore_listener(loaded.config.technocore.base_url)
        timeline = TechnocoreTimeline(client=client, store=store, room=room, run_id=run_id)
        window = await timeline.read(after_sequence, wait_seconds=0)
        state = store.get_run(run_id).state
        harness_by_role = {
            Role.PLANNER: loaded.config.roles.planner.value.title(),
            Role.IMPLEMENTER: loaded.config.roles.implementer.value.title(),
            Role.REVIEWER: loaded.config.roles.reviewer.value.title(),
        }
        return {
            "state": state.value,
            "terminal": state.is_terminal,
            "cursor": window.cursor_after,
            "at_limit": window.at_limit,
            "entries": [
                {
                    "sequence": entry.sequence,
                    "created_at": entry.created_at.isoformat(),
                    "sender": entry.sender.value,
                    "agent": harness_by_role.get(entry.sender, "Workflow supervisor"),
                    "kind": entry.kind,
                    "reply_to": str(entry.reply_to) if entry.reply_to else None,
                    "text": entry.text,
                }
                for entry in window.entries
            ],
        }
    finally:
        await client.aclose()


def _report(config_path: Path, run_id: str, *, as_json: bool) -> int:
    loaded = _load_existing_config(config_path, run_id)
    with _open_existing_store(loaded, run_id) as store:
        artifacts = generate_reports(
            store=store,
            loaded_config=loaded,
            run_id=run_id,
            output_root=loaded.config.storage.root / "reports",
            room_hash=room_hash_for_run(loaded.config.storage.root, run_id),
        )
        output = export_run_output(
            store=store,
            loaded_config=loaded,
            run_id=run_id,
            reports=artifacts,
        )
    payload = {
        "run_id": run_id,
        "directory": str(artifacts.directory),
        "output_directory": str(output.directory),
        "run_json": {
            "path": str(artifacts.run_json),
            "sha256": artifacts.run_json_sha256,
        },
        "events_jsonl": {
            "path": str(artifacts.events_jsonl),
            "sha256": artifacts.events_jsonl_sha256,
        },
        "conversation_jsonl": {
            "path": str(artifacts.conversation_jsonl),
            "sha256": artifacts.conversation_jsonl_sha256,
        },
        "report_markdown": {
            "path": str(artifacts.report_markdown),
            "sha256": artifacts.report_markdown_sha256,
        },
    }
    _print_payload(payload, as_json=as_json)
    return int(ExitCode.SUCCESS)


def _compare_reports(
    mode_a: Path,
    mode_b: Path,
    *,
    seeded_criteria: tuple[str, ...],
    as_json: bool,
) -> int:
    payload = compare_run_reports(
        mode_a,
        mode_b,
        seeded_criteria=seeded_criteria,
    )
    _print_payload(payload, as_json=as_json)
    return int(ExitCode.SUCCESS)


def _cleanup(config_path: Path, run_id: str, *, dry_run: bool, as_json: bool) -> int:
    loaded = _load_existing_config(config_path, run_id)
    git = _capture_tool("git")
    manager = WorktreeManager(
        repository=loaded.config.repository.path,
        root=(loaded.config.storage.root / "worktrees").resolve(),
        base_commit=loaded.config.repository.base_commit,
        git=git,
    )
    with _open_existing_store(loaded, run_id) as store:
        results = asyncio.run(
            cleanup_run(store=store, manager=manager, run_id=run_id, dry_run=dry_run)
        )
    payload = {
        "run_id": run_id,
        "dry_run": dry_run,
        "results": [
            {
                "role": result.role.value,
                "path": str(result.path),
                "action": result.action,
                "clean": result.clean,
                "reason": result.reason,
            }
            for result in results
        ],
    }
    _print_payload(payload, as_json=as_json)
    return int(
        ExitCode.SUCCESS
        if all(result.action not in {"retain"} for result in results)
        else ExitCode.PREFLIGHT
    )


def _state_database_path(loaded: LoadedConfig) -> Path:
    return (loaded.config.storage.root / "state.sqlite3").resolve()


def _existing_database_path(loaded: LoadedConfig) -> Path:
    path = _state_database_path(loaded)
    try:
        info = path.lstat()
    except OSError as exc:
        raise StorageError("run database does not exist", context={"file": path.name}) from exc
    if path.is_symlink() or not path.is_file() or info.st_size < 1:
        raise StorageError("run database path is not a non-empty regular file")
    return path


def _capture_tool(name: str) -> TrustedExecutable:
    discovered = shutil.which(name)
    if discovered is None:
        raise PreflightError("required executable is not available", context={"tool": name})
    return TrustedExecutable.capture(Path(discovered))


def _required_tools(loaded: LoadedProfile | None) -> frozenset[str]:
    required = {"git", "uv", "docker"}
    if loaded is None:
        return frozenset(required)
    mapping = {
        HarnessKind.CODEX: "codex",
        HarnessKind.CLAUDE: "claude",
    }
    profile = loaded.profile
    for harness in (profile.roles.planner, profile.roles.implementer, profile.roles.reviewer):
        executable = mapping.get(harness)
        if executable:
            required.add(executable)
    return frozenset(required)


def _configured_probe_executable(loaded: LoadedProfile | None, name: str) -> str | None:
    if loaded is None:
        return None
    profile = {
        "codex": loaded.profile.providers.codex,
        "claude": loaded.profile.providers.claude,
    }.get(name)
    return profile.executable if profile is not None else None


def _probe_tool(name: str, *, required: bool, executable: str | None = None) -> ToolProbe:
    requested = executable or name
    candidate = Path(requested)
    discovered = str(candidate) if candidate.is_absolute() else shutil.which(requested)
    if discovered is None:
        return ToolProbe(name, required, False, None, None, "not found on PATH")
    resolved_path = Path(discovered).resolve()
    resolved = str(resolved_path)
    if (
        executable is not None
        and name in {"codex", "claude"}
        and resolved_path.suffix.casefold() != ".exe"
    ):
        return ToolProbe(
            name,
            required,
            False,
            resolved,
            None,
            "configured provider must be a native Windows .exe",
        )
    try:
        completed = subprocess.run(  # noqa: S603 - executable was resolved by trusted probe code
            [resolved, *_PROBE_ARGS[name]],
            check=False,
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            env=_probe_environment(),
            shell=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return ToolProbe(name, required, False, resolved, None, str(exc))
    output = (completed.stdout or completed.stderr).strip().splitlines()
    version = output[0][:500] if output else None
    error = None if completed.returncode == 0 else f"version probe exited {completed.returncode}"
    return ToolProbe(name, required, completed.returncode == 0, resolved, version, error)


def _probe_environment() -> dict[str, str]:
    allowed = {
        "PATH",
        "PATHEXT",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "TEMP",
        "TMP",
        "LANG",
        "LC_ALL",
        "TERM",
    }
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def _print_payload(payload: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2))
        return
    for key, value in payload.items():
        if key == "tools":
            print("tools:")
            for tool in value:
                status = "ok" if tool["found"] else "missing"
                requirement = "required" if tool["required"] else "optional"
                detail = tool["version"] or tool["error"] or "unknown"
                print(f"  {tool['name']}: {status} ({requirement}) - {detail}")
        else:
            print(f"{key}: {value}")
