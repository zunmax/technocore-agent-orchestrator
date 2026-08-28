from __future__ import annotations

import asyncio
import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from technocore_orchestrator.adapters import FakeScenario
from technocore_orchestrator.artifacts import export_run_output
from technocore_orchestrator.config import (
    ExecutionLimits,
    LoadedConfig,
    OutputConfig,
    RepositoryConfig,
    RoleAssignments,
    StorageConfig,
    TaskConfig,
    VerificationCommand,
    VerificationConfig,
    WorkflowConfig,
)
from technocore_orchestrator.domain.collaboration import PlanChallengeResult
from technocore_orchestrator.domain.models import (
    EventKind,
    HarnessKind,
    PlannerResult,
    Role,
    RunState,
)
from technocore_orchestrator.errors import StorageError
from technocore_orchestrator.reporting import generate_reports
from technocore_orchestrator.runtime import build_fake_orchestrator
from technocore_orchestrator.storage import SQLiteStore


def _git(git: Path, repository: Path, *arguments: str) -> str:
    completed = subprocess.run(  # noqa: S603 - the resolved Git executable is fixed by the test
        [str(git), "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )
    return completed.stdout.strip()


def _loaded_config(
    tmp_path: Path,
    repository: Path,
    base_commit: str,
    *,
    expected_value: str = "feature from fake harness\n",
) -> LoadedConfig:
    config = WorkflowConfig(
        schema_version=3,
        repository=RepositoryConfig(
            path=repository,
            base_commit=base_commit,
            allowed_paths=("product.txt",),
        ),
        task=TaskConfig(
            id="fixture_change",
            title="Change the fixture",
            brief="Replace the product fixture with the supervised value.",
            acceptance_criteria=("The product fixture contains the supervised value.",),
        ),
        roles=RoleAssignments(
            planner=HarnessKind.FAKE,
            implementer=HarnessKind.FAKE,
            reviewer=HarnessKind.FAKE,
        ),
        limits=ExecutionLimits(max_model_invocations=10),
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
                            f"== {expected_value!r}"
                        ),
                    ),
                    timeout_seconds=30,
                ),
            )
        ),
        storage=StorageConfig(root=(tmp_path / "local").resolve()),
        output=OutputConfig(root=(tmp_path / "output").resolve()),
    )
    digest = hashlib.sha256(config.model_dump_json().encode()).hexdigest()
    return LoadedConfig(
        config=config,
        source_path=(tmp_path / "workflow.toml").resolve(),
        sha256=digest,
    )


def test_fake_workflow_requires_challenge_and_finalized_plan(tmp_path: Path) -> None:
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
    loaded = _loaded_config(tmp_path, repository.resolve(), base_commit)

    with SQLiteStore.open(loaded.config.storage.root / "state.sqlite3") as store:
        outcome = asyncio.run(build_fake_orchestrator(loaded, store).run("run_12345678"))
        assert store.get_run(outcome.run_id).state is RunState.COMPLETED
        assert tuple(event.kind for event in store.list_events(outcome.run_id)) == (
            EventKind.RUN_STARTED,
            EventKind.PLAN_PROPOSED,
            EventKind.PLAN_CHALLENGED,
            EventKind.PLAN_FINALIZED,
            EventKind.IMPLEMENTATION_STARTED,
            EventKind.IMPLEMENTATION_READY,
            EventKind.REVIEW_APPROVED,
            EventKind.VERIFICATION_PASSED,
        )
        planner_results = store.list_role_results(outcome.run_id, Role.PLANNER)
        implementer_results = store.list_role_results(outcome.run_id, Role.IMPLEMENTER)
        assert len(planner_results) == 2
        assert all(isinstance(result.result, PlannerResult) for result in planner_results)
        assert len(implementer_results) == 2
        assert isinstance(implementer_results[0].result, PlanChallengeResult)
        assert len(store.list_invocations(outcome.run_id)) == 5
        assert outcome.verification.passed
        artifacts = generate_reports(
            store=store,
            loaded_config=loaded,
            run_id=outcome.run_id,
            output_root=loaded.config.storage.root / "reports",
        )
        assert artifacts.run_json.is_file()
        assert artifacts.events_jsonl.is_file()
        assert artifacts.conversation_jsonl.read_text(encoding="utf-8") == ""
        output = export_run_output(
            store=store,
            loaded_config=loaded,
            run_id=outcome.run_id,
            reports=artifacts,
            secret_values=("aa" * 32,),
        )
        assert output.directory.name.startswith("fixture_change__")
        assert output.directory.name.endswith("__run_12345678")
        assert (output.code_directory / "product.txt").read_text(encoding="utf-8") == (
            "feature from fake harness\n"
        )
        agent_outputs = sorted(output.agent_output_directory.glob("*.json"))
        assert len(agent_outputs) == 5
        assert all("fake" in path.read_text(encoding="utf-8") for path in agent_outputs)
        assert (output.report_directory / "run.json").is_file()
        assert output.manifest.is_file()
        assert (
            export_run_output(
                store=store,
                loaded_config=loaded,
                run_id=outcome.run_id,
                reports=artifacts,
            ).directory
            == output.directory
        )
        (output.code_directory / "product.txt").write_text("tampered\n", encoding="utf-8")
        with pytest.raises(StorageError, match="differs from its manifest"):
            export_run_output(
                store=store,
                loaded_config=loaded,
                run_id=outcome.run_id,
                reports=artifacts,
            )


def test_fake_workflow_completes_one_revision_cycle(tmp_path: Path) -> None:
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
    loaded = _loaded_config(
        tmp_path,
        repository.resolve(),
        base_commit,
        expected_value="feature from revised fake harness\n",
    )

    with SQLiteStore.open(loaded.config.storage.root / "state.sqlite3") as store:
        outcome = asyncio.run(
            build_fake_orchestrator(
                loaded,
                store,
                scenario=FakeScenario.REVISION_CYCLE,
            ).run("run_revision01")
        )
        events = store.list_events(outcome.run_id)
        implementations = tuple(
            event for event in events if event.kind is EventKind.IMPLEMENTATION_READY
        )

        assert store.get_run(outcome.run_id).state is RunState.COMPLETED
        assert len(implementations) == 2
        assert sum(event.kind is EventKind.REVISION_REQUIRED for event in events) == 1
        assert sum(event.kind is EventKind.REVIEW_APPROVED for event in events) == 1
        assert len(store.list_invocations(outcome.run_id)) == 7
        assert outcome.verification.passed
