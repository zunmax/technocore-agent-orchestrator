from __future__ import annotations

from pathlib import Path

from technocore_orchestrator.config import (
    RepositoryConfig,
    RoleAssignments,
    TaskConfig,
    VerificationCommand,
    VerificationConfig,
    WorkflowConfig,
)
from technocore_orchestrator.domain.collaboration import CollaborationPhase
from technocore_orchestrator.domain.models import (
    FindingSeverity,
    HarnessKind,
    ReviewerResult,
    ReviewFinding,
    Role,
)
from technocore_orchestrator.prompts import build_role_prompt


def _config(repository: Path) -> WorkflowConfig:
    return WorkflowConfig(
        schema_version=3,
        repository=RepositoryConfig(
            path=repository,
            base_commit="a" * 40,
            allowed_paths=(".",),
        ),
        task=TaskConfig(
            id="age_calculator",
            title="Build an age calculator",
            brief="Build the requested static product.",
            acceptance_criteria=("The calculator works.", "Only allowed paths change."),
        ),
        roles=RoleAssignments(
            planner=HarnessKind.FAKE,
            implementer=HarnessKind.FAKE,
            reviewer=HarnessKind.FAKE,
        ),
        verification=VerificationConfig(
            commands=(
                VerificationCommand(
                    id="git_diff_check",
                    argv=("git", "diff", "--check"),
                ),
            )
        ),
    )


def test_revision_prompt_defines_current_diff_and_retry_semantics(tmp_path: Path) -> None:
    review = ReviewerResult(
        decision="revision_required",
        summary="Malformed dates need validation.",
        findings=(
            ReviewFinding(
                id="finding_1",
                severity=FindingSeverity.IMPORTANT,
                criterion_id="criterion_1",
                path="script.js",
                problem="Malformed dates can produce NaN.",
                required_fix="Validate all parsed calendar components.",
            ),
        ),
        acceptance_coverage=("criterion_1", "criterion_2"),
        residual_risks=(),
    )

    prompt = build_role_prompt(
        _config(tmp_path),
        run_id="run_12345678",
        role=Role.IMPLEMENTER,
        commit="b" * 40,
        phase=CollaborationPhase.REVISE,
        revision_request=review,
        collaboration_phase=CollaborationPhase.REVISE,
    )

    assert "git diff --name-only HEAD" in prompt
    assert "not the whole earlier candidate" in prompt
    assert "list may be empty" in prompt
    assert "until exactly one call succeeds" in prompt
