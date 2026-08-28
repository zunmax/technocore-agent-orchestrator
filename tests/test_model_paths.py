from __future__ import annotations

import pytest
from pydantic import ValidationError

from technocore_orchestrator.domain.collaboration import CandidateSubmission
from technocore_orchestrator.domain.models import (
    CriterionEvidence,
    ImplementerResult,
    PlanStep,
    canonical_repository_path,
)


def test_model_paths_preserve_signed_text_but_have_a_canonical_git_form() -> None:
    evidence = CriterionEvidence(
        criterion_id="criterion_1",
        changed_paths=("./index.html", "./styles.css"),
        verification_command_ids=("git_diff_check",),
        evidence=("Created the complete product.",),
    )
    result = ImplementerResult(
        outcome="changes_ready",
        summary="Created the product.",
        candidate_commit=None,
        declared_changed_paths=("./index.html", "./styles.css"),
        focused_checks=(),
        criterion_evidence=(evidence,),
        remaining_concerns=(),
        blocked_reason=None,
    )

    assert result.declared_changed_paths == ("./index.html", "./styles.css")
    assert result.criterion_evidence[0].changed_paths == ("./index.html", "./styles.css")
    assert tuple(canonical_repository_path(path) for path in result.declared_changed_paths) == (
        "index.html",
        "styles.css",
    )


def test_plan_paths_keep_their_signed_spelling() -> None:
    step = PlanStep(
        id="build",
        description="Build the product.",
        expected_paths=(".", "./index.html"),
        criterion_ids=("criterion_1",),
    )

    assert step.expected_paths == (".", "./index.html")


def test_model_paths_reject_duplicate_canonical_forms() -> None:
    with pytest.raises(ValidationError, match="unique"):
        PlanStep(
            id="build",
            description="Build the product.",
            expected_paths=("index.html", "./index.html"),
            criterion_ids=("criterion_1",),
        )


def test_model_paths_reject_parent_traversal() -> None:
    with pytest.raises(ValidationError, match="unsafe component"):
        PlanStep(
            id="build",
            description="Build the product.",
            expected_paths=("../outside.txt",),
            criterion_ids=("criterion_1",),
        )


def test_live_candidate_submission_rejects_inherited_revision_paths() -> None:
    inherited_evidence = CriterionEvidence(
        criterion_id="criterion_1",
        changed_paths=("index.html", "styles.css", "script.js"),
        verification_command_ids=("git_diff_check",),
        evidence=("The complete candidate remains in scope.",),
    )
    current_evidence = CriterionEvidence(
        criterion_id="criterion_2",
        changed_paths=("script.js",),
        verification_command_ids=("git_diff_check",),
        evidence=("The current revision fixes the finding.",),
    )
    retained_payload = ImplementerResult(
        outcome="changes_ready",
        summary="Fixed the review finding.",
        candidate_commit=None,
        declared_changed_paths=("script.js",),
        focused_checks=(),
        criterion_evidence=(inherited_evidence, current_evidence),
        remaining_concerns=(),
        blocked_reason=None,
    )

    assert retained_payload.declared_changed_paths == ("script.js",)
    with pytest.raises(ValidationError, match="must exactly match"):
        CandidateSubmission.model_validate(retained_payload.model_dump())


def test_live_candidate_submission_accepts_empty_paths_for_unchanged_criterion() -> None:
    submission = CandidateSubmission(
        outcome="changes_ready",
        summary="Fixed the review finding.",
        candidate_commit=None,
        declared_changed_paths=("script.js",),
        focused_checks=(),
        criterion_evidence=(
            CriterionEvidence(
                criterion_id="criterion_1",
                changed_paths=(),
                verification_command_ids=("git_diff_check",),
                evidence=("The current revision does not alter this criterion.",),
            ),
            CriterionEvidence(
                criterion_id="criterion_2",
                changed_paths=("script.js",),
                verification_command_ids=("git_diff_check",),
                evidence=("The current revision fixes this criterion.",),
            ),
        ),
        remaining_concerns=(),
        blocked_reason=None,
    )

    assert submission.declared_changed_paths == ("script.js",)
