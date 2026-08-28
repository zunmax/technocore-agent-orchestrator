"""Explicit real-provider assembly behind identity, containment, and usage consent."""

from __future__ import annotations

import base64
import hashlib
import os
import secrets
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from technocore_orchestrator.adapters import (
    ClaudeAdapter,
    CodexAdapter,
    HarnessAdapter,
    HarnessCapabilities,
)
from technocore_orchestrator.collaboration_backend import TechnocoreCollaborationBackend
from technocore_orchestrator.config import (
    ClaudeHarnessProfile,
    HarnessProfile,
    LoadedConfig,
    WorkflowConfig,
)
from technocore_orchestrator.domain.models import RUN_ID_RE, HarnessKind, Role
from technocore_orchestrator.errors import IdentityError, PreflightError, StorageError
from technocore_orchestrator.execution import TrustedExecutable
from technocore_orchestrator.identity import RoleIdentity, validate_room_name
from technocore_orchestrator.network import require_loopback_technocore_listener
from technocore_orchestrator.orchestrator import WorkflowOrchestrator
from technocore_orchestrator.publication import TechnocoreEventPublisher
from technocore_orchestrator.room_security import OwnedRoomProvisioner
from technocore_orchestrator.runtime import (
    _capture_executable,
    _safe_child_environment,
    _verification_jobs,
)
from technocore_orchestrator.storage import ParticipantEvidence, SQLiteStore
from technocore_orchestrator.technocore import TechnocoreClient
from technocore_orchestrator.verification import Verifier
from technocore_orchestrator.worktrees import WorktreeManager

IdentityLoader = Callable[[Role, Path], RoleIdentity]
_IDENTITY_ROLES = (Role.SUPERVISOR, Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER)
_REAL_HARNESS_ORDER = (HarnessKind.CODEX, HarnessKind.CLAUDE)


@dataclass(frozen=True, slots=True)
class RealRuntime:
    orchestrator: WorkflowOrchestrator
    client: TechnocoreClient
    adapters: Mapping[HarnessKind, HarnessAdapter]
    config: WorkflowConfig
    executables: Mapping[HarnessKind, TrustedExecutable]
    identities: Mapping[Role, RoleIdentity]
    room_hash: str
    room_provisioner: OwnedRoomProvisioner
    collaboration: TechnocoreCollaborationBackend

    async def preflight(self) -> tuple[ParticipantEvidence, ...]:
        """Check transport and exact CLI versions without invoking a model."""

        await self.client.health()
        version = await self.client.manifest_version()
        if version != self.config.technocore.expected_version:
            raise PreflightError(
                "Technocore service version is unsupported",
                context={
                    "expected": self.config.technocore.expected_version,
                    "installed": version,
                },
            )
        require_loopback_technocore_listener(self.config.technocore.base_url)
        await self.room_provisioner.provision()
        capabilities: dict[HarnessKind, HarnessCapabilities] = {}
        for kind, adapter in self.adapters.items():
            capabilities[kind] = await adapter.probe()
        return _participant_snapshot(
            config=self.config,
            capabilities=capabilities,
            executables=self.executables,
            identities=self.identities,
        )

    async def close(self) -> None:
        await self.client.aclose()


def build_real_runtime(
    *,
    loaded: LoadedConfig,
    store: SQLiteStore,
    run_id: str,
    resume: bool,
    load_identity: IdentityLoader,
) -> RealRuntime:
    """Assemble only assigned real harnesses without reading raw provider API keys."""

    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("real runtime run id is invalid", context={"run_id": run_id})
    config = loaded.config
    active_harnesses = _active_real_harnesses(config)

    git = _capture_executable("git")
    environment = _safe_child_environment()
    schema_root = Path(str(resources.files("technocore_orchestrator.schemas.v1"))).resolve(
        strict=True
    )
    executables = {
        kind: _capture_provider_executable(_require_profile(config, kind).executable)
        for kind in active_harnesses
    }
    adapters = _build_adapters(
        config,
        schema_root=schema_root,
        environment=environment,
        executables=executables,
    )
    identities = _load_identities(config.storage.root, load_identity)
    room = _room_for_run(config.storage.root, run_id, resume=resume)
    client = TechnocoreClient(config.technocore)
    publisher_identities = {
        Role.SUPERVISOR: identities[Role.SUPERVISOR],
        Role.PLANNER: identities[Role.PLANNER],
        Role.IMPLEMENTER: identities[Role.IMPLEMENTER],
        Role.REVIEWER: identities[Role.REVIEWER],
        Role.VERIFIER: identities[Role.SUPERVISOR],
    }
    room_provisioner = OwnedRoomProvisioner(
        client=client,
        store=store,
        room=room,
        identities=identities,
    )
    collaboration = TechnocoreCollaborationBackend(
        client=client,
        store=store,
        room=room,
        run_id=run_id,
        task_id=config.task.id,
        identities=identities,
    )
    orchestrator = WorkflowOrchestrator(
        loaded_config=loaded,
        store=store,
        worktrees=WorktreeManager(
            repository=config.repository.path,
            root=(config.storage.root / "worktrees").resolve(),
            base_commit=config.repository.base_commit,
            git=git,
        ),
        adapters=adapters,
        publisher=TechnocoreEventPublisher(
            client=client,
            store=store,
            room=room,
            identities=publisher_identities,
        ),
        verifier=Verifier(),
        verification_jobs=_verification_jobs(loaded),
        collaboration=collaboration,
    )
    return RealRuntime(
        orchestrator=orchestrator,
        client=client,
        adapters=adapters,
        config=config,
        executables=executables,
        identities=identities,
        room_hash=hashlib.sha256(room.encode("utf-8")).hexdigest(),
        room_provisioner=room_provisioner,
        collaboration=collaboration,
    )


def room_hash_for_run(storage_root: Path, run_id: str) -> str | None:
    """Return only the hash of a retained room capability for report regeneration."""

    path = _room_path(storage_root, run_id)
    if not path.exists() and not path.is_symlink():
        return None
    room = _read_room(path)
    return hashlib.sha256(room.encode("utf-8")).hexdigest()


def read_room_capability_for_run(storage_root: Path, run_id: str) -> str:
    """Load the private room capability for an in-scope local runtime operation."""

    return _read_room(_room_path(storage_root, run_id))


def identity_path(storage_root: Path, role: Role) -> Path:
    if role not in _IDENTITY_ROLES:
        raise IdentityError("role does not own a standalone identity file")
    root = _secure_subdirectory(storage_root, "identities")
    return root / f"{role.value}.identity.dpapi"


def _build_adapters(
    config: WorkflowConfig,
    *,
    schema_root: Path,
    environment: tuple[tuple[str, str], ...],
    executables: Mapping[HarnessKind, TrustedExecutable],
) -> dict[HarnessKind, HarnessAdapter]:
    adapters: dict[HarnessKind, HarnessAdapter] = {}
    for kind in _active_real_harnesses(config):
        profile = _require_profile(config, kind)
        executable = executables[kind]
        if kind is HarnessKind.CODEX:
            adapters[kind] = CodexAdapter(
                executable=executable,
                schema_root=schema_root,
                model=profile.model,
                expected_version=profile.expected_version,
                environment=environment,
            )
        elif kind is HarnessKind.CLAUDE:
            if not isinstance(profile, ClaudeHarnessProfile):
                raise PreflightError("Claude profile does not provide its required limits")
            adapters[kind] = ClaudeAdapter(
                executable=executable,
                schema_root=schema_root,
                model=profile.model,
                expected_version=profile.expected_version,
                environment=environment,
                max_turns=config.limits.claude_max_turns,
            )
    return adapters


def _active_real_harnesses(config: WorkflowConfig) -> tuple[HarnessKind, ...]:
    assignments = {config.roles.planner, config.roles.implementer, config.roles.reviewer}
    if HarnessKind.FAKE in assignments:
        raise PreflightError("fake roles cannot be mixed with real provider roles")
    if len(assignments) < 2:
        raise PreflightError("real workflows require at least two distinct providers")
    return tuple(kind for kind in _REAL_HARNESS_ORDER if kind in assignments)


def _require_profile(config: WorkflowConfig, kind: HarnessKind) -> HarnessProfile:
    profile = {
        HarnessKind.CODEX: config.providers.codex,
        HarnessKind.CLAUDE: config.providers.claude,
    }[kind]
    if profile is None:
        raise PreflightError("real harness profile is missing", context={"harness": kind.value})
    return profile


def _capture_provider_executable(value: str) -> TrustedExecutable:
    executable = _capture_executable(value)
    if executable.path.suffix.casefold() != ".exe":
        raise PreflightError(
            "Windows provider executable must be a native .exe",
            context={"path": str(executable.path)},
        )
    return executable


def _participant_snapshot(
    *,
    config: WorkflowConfig,
    capabilities: Mapping[HarnessKind, HarnessCapabilities],
    executables: Mapping[HarnessKind, TrustedExecutable],
    identities: Mapping[Role, RoleIdentity],
) -> tuple[ParticipantEvidence, ...]:
    role_harnesses = {
        Role.PLANNER: config.roles.planner,
        Role.IMPLEMENTER: config.roles.implementer,
        Role.REVIEWER: config.roles.reviewer,
    }
    participants: list[ParticipantEvidence] = [
        ParticipantEvidence(role=Role.SUPERVISOR, did=identities[Role.SUPERVISOR].public.did)
    ]
    for role in (Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER):
        kind = role_harnesses[role]
        profile = _require_profile(config, kind)
        capability = capabilities[kind]
        executable = executables[kind]
        participants.append(
            ParticipantEvidence(
                role=role,
                did=identities[role].public.did,
                harness=kind,
                model=profile.model,
                cli_name=capability.name,
                cli_version=capability.version,
                executable_path=executable.resolved_path,
                executable_sha256=executable.sha256,
                executable_size_bytes=executable.size_bytes,
                structured_output=capability.structured_output,
                resumable=capability.resumable,
            )
        )
    participants.append(
        ParticipantEvidence(role=Role.VERIFIER, did=identities[Role.SUPERVISOR].public.did)
    )
    return tuple(participants)


def _load_identities(storage_root: Path, load_identity: IdentityLoader) -> dict[Role, RoleIdentity]:
    identities = {
        role: load_identity(role, identity_path(storage_root, role)) for role in _IDENTITY_ROLES
    }
    dids = {identity.public.did for identity in identities.values()}
    if len(dids) != len(identities):
        raise IdentityError("real workflow roles must use distinct Technocore identities")
    return identities


def _room_for_run(storage_root: Path, run_id: str, *, resume: bool) -> str:
    path = _room_path(storage_root, run_id)
    if resume:
        return _read_room(path)
    if path.exists() or path.is_symlink():
        raise StorageError("private room file already exists for a new run")
    room = validate_room_name(
        "d-p-orchestrator-"
        + base64.b32encode(secrets.token_bytes(16)).decode("ascii").rstrip("=").lower()
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
            handle.write(room + "\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise StorageError("unable to create private room file") from exc
    return room


def _room_path(storage_root: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("room run id is invalid", context={"run_id": run_id})
    return _secure_subdirectory(storage_root, "rooms") / f"{run_id}.room"


def _read_room(path: Path) -> str:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise StorageError("private room path must be a regular file")
        if info.st_size > 64:
            raise StorageError("private room file exceeds its size limit")
        room = path.read_text(encoding="ascii").strip()
    except StorageError:
        raise
    except (OSError, UnicodeError) as exc:
        raise StorageError("unable to read private room file") from exc
    return validate_room_name(room)


def _secure_subdirectory(storage_root: Path, name: str) -> Path:
    try:
        root = storage_root.resolve()
        candidate = root / name
        if candidate.is_symlink():
            raise StorageError(f"{name} directory must not be a symlink")
        candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
        resolved = candidate.resolve(strict=True)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(f"unable to prepare {name} directory") from exc
    if resolved.parent != root:
        raise StorageError(f"{name} directory escaped its storage root")
    return resolved
