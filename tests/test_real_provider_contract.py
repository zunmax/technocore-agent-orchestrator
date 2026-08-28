from __future__ import annotations

import asyncio
import hashlib
import os
import secrets
import shutil
import subprocess
import sys
from importlib import resources
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from technocore_orchestrator.adapters.base import HarnessInvocation
from technocore_orchestrator.adapters.claude import ClaudeAdapter
from technocore_orchestrator.adapters.codex import CodexAdapter
from technocore_orchestrator.collaboration_backend import TechnocoreCollaborationBackend
from technocore_orchestrator.config import (
    ClaudeHarnessProfile,
    ExecutionLimits,
    HarnessProfile,
    LoadedConfig,
    ProviderProfiles,
    RepositoryConfig,
    RoleAssignments,
    StorageConfig,
    TaskConfig,
    TechnocoreConfig,
    VerificationCommand,
    VerificationConfig,
    WorkflowConfig,
)
from technocore_orchestrator.domain.collaboration import CollaborationKind, CollaborationPhase
from technocore_orchestrator.domain.models import HarnessKind, Role, RunState
from technocore_orchestrator.execution import TrustedExecutable
from technocore_orchestrator.gateway import RoleGateway
from technocore_orchestrator.identity import RoleIdentity
from technocore_orchestrator.network import require_loopback_technocore_listener
from technocore_orchestrator.real_runtime import build_real_runtime
from technocore_orchestrator.room_security import OwnedRoomProvisioner
from technocore_orchestrator.runtime import _safe_child_environment
from technocore_orchestrator.storage import SQLiteStore
from technocore_orchestrator.technocore import TechnocoreClient

pytestmark = pytest.mark.skipif(
    os.environ.get("TCORE_RUN_REAL_PROVIDER_TESTS") != "1",
    reason="real account-backed provider contract test requires explicit opt-in",
)


def _required_path(name: str) -> Path:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required for the opted-in provider contract test")
    return Path(value).resolve(strict=True)


def _required_value(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.fail(f"{name} is required for the opted-in provider contract test")
    return value


def _schema_root() -> Path:
    return Path(str(resources.files("technocore_orchestrator.schemas.v1"))).resolve(strict=True)


def _git(git: Path, repository: Path, *arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - test uses one resolved Git executable
        [str(git), "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.stdout.strip()


def _real_loaded_config(tmp_path: Path, repository: Path, base_commit: str) -> LoadedConfig:
    config = WorkflowConfig(
        schema_version=3,
        repository=RepositoryConfig(
            path=repository,
            base_commit=base_commit,
            allowed_paths=("product.txt",),
        ),
        task=TaskConfig(
            id="real_contract",
            title="Apply the exact contract fixture",
            brief=(
                "Change only product.txt so its complete UTF-8 content is exactly "
                "feature from real providers followed by one newline."
            ),
            acceptance_criteria=(
                "product.txt contains exactly the requested line and trailing newline.",
            ),
        ),
        roles=RoleAssignments(
            planner=HarnessKind.CODEX,
            implementer=HarnessKind.CLAUDE,
            reviewer=HarnessKind.CODEX,
        ),
        providers=ProviderProfiles(
            codex=HarnessProfile(
                executable=str(_required_path("TCORE_CODEX_EXE")),
                model=_required_value("TCORE_CODEX_MODEL"),
                expected_version=_required_value("TCORE_CODEX_VERSION"),
            ),
            claude=ClaudeHarnessProfile(
                executable=str(_required_path("TCORE_CLAUDE_EXE")),
                model=_required_value("TCORE_CLAUDE_MODEL"),
                expected_version=_required_value("TCORE_CLAUDE_VERSION"),
            ),
        ),
        limits=ExecutionLimits(
            run_wall_seconds=1_800,
            invocation_wall_seconds=300,
            max_revision_cycles=1,
            max_model_invocations=20,
            claude_max_turns=20,
        ),
        verification=VerificationConfig(
            commands=(
                VerificationCommand(
                    id="fixture_check",
                    argv=(
                        sys.executable,
                        "-c",
                        (
                            "from pathlib import Path; "
                            "assert Path('product.txt').read_text(encoding='utf-8') "
                            "== 'feature from real providers\\n'"
                        ),
                    ),
                    timeout_seconds=30,
                ),
            )
        ),
        storage=StorageConfig(root=(tmp_path / "local").resolve()),
    )
    return LoadedConfig(
        config=config,
        source_path=(tmp_path / "workflow.toml").resolve(),
        sha256=hashlib.sha256(config.model_dump_json().encode()).hexdigest(),
    )


async def _exercise_real_providers(tmp_path: Path) -> None:
    config = TechnocoreConfig()
    client = TechnocoreClient(config)
    identities = {
        Role.SUPERVISOR: RoleIdentity(Ed25519PrivateKey.generate()),
        Role.PLANNER: RoleIdentity(Ed25519PrivateKey.generate()),
        Role.IMPLEMENTER: RoleIdentity(Ed25519PrivateKey.generate()),
        Role.REVIEWER: RoleIdentity(Ed25519PrivateKey.generate()),
    }
    room = f"d-p-orchestrator-contract-{secrets.token_hex(8)}"
    store = SQLiteStore.open(tmp_path / "state.sqlite3")
    try:
        await client.health()
        assert await client.manifest_version() == config.expected_version
        require_loopback_technocore_listener(config.base_url)
        store.create_run(
            run_id="run_12345678",
            task_id="task",
            config_digest="a" * 64,
            repository_path=tmp_path,
            base_commit="b" * 40,
        )
        await OwnedRoomProvisioner(
            client=client,
            store=store,
            room=room,
            identities=identities,
        ).provision()
        backend = TechnocoreCollaborationBackend(
            client=client,
            store=store,
            room=room,
            run_id="run_12345678",
            task_id="task",
            identities=identities,
        )
        schema_root = _schema_root()
        environment = _safe_child_environment()
        codex = CodexAdapter(
            executable=TrustedExecutable.capture(_required_path("TCORE_CODEX_EXE")),
            schema_root=schema_root,
            model=_required_value("TCORE_CODEX_MODEL"),
            expected_version=_required_value("TCORE_CODEX_VERSION"),
            environment=environment,
        )
        claude = ClaudeAdapter(
            executable=TrustedExecutable.capture(_required_path("TCORE_CLAUDE_EXE")),
            schema_root=schema_root,
            model=_required_value("TCORE_CLAUDE_MODEL"),
            expected_version=_required_value("TCORE_CLAUDE_VERSION"),
            environment=environment,
            max_turns=8,
        )
        async with RoleGateway(
            role=Role.PLANNER,
            phase=CollaborationPhase.PROPOSE_PLAN,
            backend=backend,
        ) as gateway:
            planner = await codex.invoke(
                HarnessInvocation(
                    run_id="run_12345678",
                    task_id="task",
                    role=Role.PLANNER,
                    phase=CollaborationPhase.PROPOSE_PLAN,
                    attempt=1,
                    worktree=Path.cwd().resolve(),
                    prompt=(
                        "Use read_new_messages exactly once, then publish_plan exactly once. "
                        "Propose one read-only plan step named inspect_contract for product.txt, "
                        "mapped to criterion_1. challenge_dispositions must be empty. Return the "
                        "exact same plan as final JSON. Do not modify files."
                    ),
                    timeout_seconds=300,
                    output_limit_bytes=1_048_576,
                    gateway=gateway.config,
                )
            )
            assert gateway.called_tools == ("read_new_messages", "publish_plan")
        proposal = store.list_collaboration_messages("run_12345678")[-1]
        assert proposal.envelope.kind is CollaborationKind.PLAN_PROPOSED

        async with RoleGateway(
            role=Role.IMPLEMENTER,
            phase=CollaborationPhase.CHALLENGE_PLAN,
            backend=backend,
        ) as gateway:
            challenge = await claude.invoke(
                HarnessInvocation(
                    run_id="run_12345678",
                    task_id="task",
                    role=Role.IMPLEMENTER,
                    phase=CollaborationPhase.CHALLENGE_PLAN,
                    attempt=1,
                    worktree=Path.cwd().resolve(),
                    prompt=(
                        "Use read_new_messages exactly once. Acknowledge the exact proposed plan "
                        "using its event_id, sequence and payload_sha256, then call "
                        "publish_plan_challenge exactly once replying to that proposal. Return the "
                        "same challenge JSON. Approve it with no issues if it is bounded."
                    ),
                    timeout_seconds=300,
                    output_limit_bytes=1_048_576,
                    gateway=gateway.config,
                )
            )
            assert gateway.called_tools == (
                "read_new_messages",
                "acknowledge_handoff",
                "publish_plan_challenge",
            )
        assert planner.role_result == proposal.payload
        assert (
            challenge.role_result == store.list_collaboration_messages("run_12345678")[-1].payload
        )
    finally:
        store.close()
        await client.aclose()


def test_codex_and_claude_exchange_signed_local_technocore_messages(tmp_path: Path) -> None:
    asyncio.run(_exercise_real_providers(tmp_path))


def test_complete_real_cross_harness_workflow(tmp_path: Path) -> None:
    discovered = shutil.which("git")
    assert discovered is not None
    git = Path(discovered).resolve(strict=True)
    repository = tmp_path / "repository"
    repository.mkdir()
    (repository / "product.txt").write_text("original\n", encoding="utf-8")
    _git(git, repository, "init")
    _git(git, repository, "config", "core.autocrlf", "false")
    _git(git, repository, "add", "product.txt")
    _git(
        git,
        repository,
        "-c",
        "user.name=Workflow Test",
        "-c",
        "user.email=workflow-test@example.invalid",
        "commit",
        "-m",
        "fixture base",
    )
    base_commit = _git(git, repository, "rev-parse", "HEAD")
    loaded = _real_loaded_config(tmp_path, repository.resolve(), base_commit)
    identities = {
        Role.SUPERVISOR: RoleIdentity(Ed25519PrivateKey.generate()),
        Role.PLANNER: RoleIdentity(Ed25519PrivateKey.generate()),
        Role.IMPLEMENTER: RoleIdentity(Ed25519PrivateKey.generate()),
        Role.REVIEWER: RoleIdentity(Ed25519PrivateKey.generate()),
    }

    async def exercise(store: SQLiteStore) -> None:
        runtime = build_real_runtime(
            loaded=loaded,
            store=store,
            run_id="run_87654321",
            resume=False,
            load_identity=lambda role, _path: identities[role],
        )
        try:
            participants = await runtime.preflight()
            outcome = await runtime.orchestrator.run("run_87654321", participants=participants)
            assert outcome.verification.passed
        finally:
            await runtime.close()

    with SQLiteStore.open(loaded.config.storage.root / "state.sqlite3") as store:
        asyncio.run(exercise(store))
        assert store.get_run("run_87654321").state is RunState.COMPLETED
        assert tuple(
            message.envelope.kind for message in store.list_collaboration_messages("run_87654321")
        ) == (
            CollaborationKind.PLAN_PROPOSED,
            CollaborationKind.HANDOFF_ACKNOWLEDGED,
            CollaborationKind.PLAN_CHALLENGED,
            CollaborationKind.HANDOFF_ACKNOWLEDGED,
            CollaborationKind.PLAN_FINALIZED,
            CollaborationKind.HANDOFF_ACKNOWLEDGED,
            CollaborationKind.IMPLEMENTATION_SUBMITTED,
            CollaborationKind.CANDIDATE_READY,
            CollaborationKind.HANDOFF_ACKNOWLEDGED,
            CollaborationKind.REVIEW_SUBMITTED,
        )
