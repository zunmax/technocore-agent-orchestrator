from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest

from technocore_orchestrator.domain.models import CriterionEvidence, ImplementerResult, Role
from technocore_orchestrator.errors import StorageError
from technocore_orchestrator.reporting import (
    Redactor,
    _acquire_generation_lock,
    _candidate_changed_paths,
)
from technocore_orchestrator.storage import SQLiteStore, WorktreeObservation


def test_redactor_removes_owned_and_unowned_private_room_capabilities() -> None:
    redactor = Redactor()
    value = "rooms d-p-orchestrator-abcdefghijklmnopqrstuvwxyz and p-private-room-12345678"

    redacted = redactor.text(value)

    assert "d-p-orchestrator" not in redacted
    assert "p-private" not in redacted
    assert redacted.count("[REDACTED ROOM]") == 2


def test_redactor_removes_seed_assignments_and_exact_seed_values() -> None:
    seed = "ab" * 32
    redactor = Redactor(secret_values=(seed,))

    redacted = redactor.text(f"SIGN_SEED={seed} seed: {'cd' * 32}")

    assert seed not in redacted
    assert "cd" * 32 not in redacted
    assert redacted.count("[REDACTED]") >= 2


def test_report_uses_full_durable_candidate_paths_after_a_partial_revision() -> None:
    candidate = "a" * 40
    observation = WorktreeObservation(
        observation_id=1,
        worktree_id=1,
        head_commit=candidate,
        clean=True,
        changed_paths=("index.html", "styles.css", "script.js"),
        observed_at=datetime.now(UTC),
    )

    class ObservationStore:
        def get_latest_worktree_observation(self, _run_id: str, _role: Role) -> WorktreeObservation:
            return observation

    revision = ImplementerResult(
        outcome="changes_ready",
        summary="Corrected the reviewed script.",
        candidate_commit=None,
        declared_changed_paths=("script.js",),
        focused_checks=(),
        criterion_evidence=(
            CriterionEvidence(
                criterion_id="criterion_1",
                changed_paths=("script.js",),
                verification_command_ids=("git_diff_check",),
                evidence=("Corrected the review finding.",),
            ),
        ),
        remaining_concerns=(),
        blocked_reason=None,
    )
    role_results = (SimpleNamespace(role=Role.IMPLEMENTER, result=revision),)

    assert _candidate_changed_paths(
        cast(SQLiteStore, ObservationStore()),
        "run_12345678",
        candidate,
        (),
        role_results,
    ) == ("index.html", "styles.css", "script.js")


def test_report_generation_lock_rejects_a_concurrent_writer(tmp_path: Path) -> None:
    path = tmp_path / "generation.lock"
    lease = _acquire_generation_lock(path)
    try:
        with pytest.raises(StorageError, match="another process"):
            _acquire_generation_lock(path)
    finally:
        lease.close()

    replacement = _acquire_generation_lock(path)
    replacement.close()
