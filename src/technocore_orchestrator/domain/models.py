"""Versioned, closed models at workflow trust boundaries."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

RUN_ID_RE = re.compile(r"run_[a-z0-9][a-z0-9_-]{7,63}")
TASK_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
STEP_ID_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
FULL_SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


def canonical_repository_path(value: str, *, allow_root: bool = False) -> str:
    """Normalize one safe model-supplied path to Git's repository-relative form."""

    if (
        not value
        or "\\" in value
        or ":" in value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("repository path must use visible forward-slash-separated text")
    if value == ".":
        if allow_root:
            return value
        raise ValueError("repository file path must not name the repository root")
    raw_parts = value.split("/")
    if raw_parts[0] == ".":
        raw_parts = raw_parts[1:]
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise ValueError("repository path contains an unsafe component")
    path = PurePosixPath(*raw_parts)
    if path.is_absolute():
        raise ValueError("repository path must be relative")
    return path.as_posix()


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class HarnessKind(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    FAKE = "fake"


class TerminationReason(StrEnum):
    EXITED = "exited"
    TIMED_OUT = "timed_out"
    OUTPUT_LIMIT_EXCEEDED = "output_limit_exceeded"


class Role(StrEnum):
    SUPERVISOR = "supervisor"
    PLANNER = "planner"
    IMPLEMENTER = "implementer"
    REVIEWER = "reviewer"
    VERIFIER = "verifier"


class RunState(StrEnum):
    CREATED = "created"
    PLANNING = "planning"
    CHALLENGING = "challenging"
    FINALIZING = "finalizing"
    READY = "ready"
    IMPLEMENTING = "implementing"
    REVIEWING = "reviewing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"

    @property
    def is_terminal(self) -> bool:
        return self in {self.COMPLETED, self.FAILED, self.CANCELED}


class EventKind(StrEnum):
    RUN_STARTED = "run_started"
    PLAN_PROPOSED = "plan_proposed"
    PLAN_CHALLENGED = "plan_challenged"
    PLAN_FINALIZED = "plan_finalized"
    IMPLEMENTATION_STARTED = "implementation_started"
    IMPLEMENTATION_READY = "implementation_ready"
    REVISION_REQUIRED = "revision_required"
    REVIEW_APPROVED = "review_approved"
    VERIFICATION_PASSED = "verification_passed"
    VERIFICATION_FAILED = "verification_failed"
    RUN_FAILED = "run_failed"
    RUN_CANCELED = "run_canceled"
    TRANSPORT_WARNING = "transport_warning"
    SECURITY_WARNING = "security_warning"


class ArtifactType(StrEnum):
    GIT_COMMIT = "git_commit"
    REPO_FILE = "repo_file"
    LOCAL_REPORT = "local_report"


class ArtifactReference(ClosedModel):
    type: ArtifactType
    value: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    sha256: StrictStr | None = None

    @model_validator(mode="after")
    def validate_by_type(self) -> ArtifactReference:
        if self.type is ArtifactType.GIT_COMMIT:
            if not FULL_SHA_RE.fullmatch(self.value) or self.sha256 is not None:
                raise ValueError("git_commit requires a lowercase full SHA and no sha256 field")
        elif self.sha256 is None or not SHA256_RE.fullmatch(self.sha256):
            raise ValueError("file/report artifacts require a lowercase SHA-256 digest")
        return self


class EventEnvelope(ClosedModel):
    v: Literal[1]
    event_id: UUID
    run_id: StrictStr
    task_id: StrictStr
    kind: EventKind
    sender: Role
    attempt: Annotated[StrictInt, Field(ge=1, le=100)]
    created_at: datetime
    reply_to: UUID | None = None
    summary: Annotated[StrictStr, Field(min_length=1, max_length=600)]
    artifact: ArtifactReference | None = None

    @field_validator("run_id")
    @classmethod
    def validate_run_id(cls, value: str) -> str:
        if not RUN_ID_RE.fullmatch(value):
            raise ValueError(f"run_id must match {RUN_ID_RE.pattern!r}")
        return value

    @field_validator("task_id")
    @classmethod
    def validate_task_id(cls, value: str) -> str:
        if not TASK_ID_RE.fullmatch(value):
            raise ValueError(f"task_id must match {TASK_ID_RE.pattern!r}")
        return value

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include an explicit timezone")
        return value.astimezone(UTC)


class PlanStep(ClosedModel):
    id: StrictStr
    description: Annotated[StrictStr, Field(min_length=1, max_length=1_000)]
    expected_paths: tuple[Annotated[StrictStr, Field(min_length=1, max_length=512)], ...]
    criterion_ids: tuple[StrictStr, ...]

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError(f"step id must match {STEP_ID_RE.pattern!r}")
        return value

    @field_validator("expected_paths")
    @classmethod
    def validate_expected_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(canonical_repository_path(value, allow_root=True) for value in values)
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("plan step expected paths must be non-empty and unique")
        return values

    @field_validator("criterion_ids")
    @classmethod
    def validate_criterion_ids(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("plan step criterion ids must be non-empty and unique")
        if any(not STEP_ID_RE.fullmatch(value) for value in values):
            raise ValueError("plan step criterion ids must use stable identifier syntax")
        return values


class ChallengeDisposition(ClosedModel):
    issue_id: StrictStr
    disposition: Literal["accepted", "rejected"]
    rationale: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]

    @field_validator("issue_id")
    @classmethod
    def validate_issue_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError(f"challenge issue id must match {STEP_ID_RE.pattern!r}")
        return value


class PlannerResult(ClosedModel):
    summary: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]
    steps: tuple[PlanStep, ...]
    risks: tuple[Annotated[StrictStr, Field(min_length=1, max_length=1_000)], ...]
    verification_suggestions: tuple[
        Annotated[StrictStr, Field(min_length=1, max_length=1_000)], ...
    ]
    challenge_dispositions: tuple[ChallengeDisposition, ...]
    blocked_reason: Annotated[StrictStr, Field(min_length=1, max_length=2_000)] | None

    @field_validator("steps")
    @classmethod
    def validate_steps(cls, values: tuple[PlanStep, ...]) -> tuple[PlanStep, ...]:
        if not values:
            raise ValueError("planner result must contain at least one step")
        ids = [step.id for step in values]
        if len(ids) != len(set(ids)):
            raise ValueError("planner step ids must be unique")
        return values


class CriterionEvidence(ClosedModel):
    criterion_id: StrictStr
    changed_paths: Annotated[
        tuple[Annotated[StrictStr, Field(min_length=1, max_length=512)], ...],
        Field(
            description=(
                "Paths changed in the current implementation invocation that support this "
                "criterion; use an empty list when the current revision did not change a path "
                "for this criterion."
            )
        ),
    ]
    verification_command_ids: tuple[StrictStr, ...]
    evidence: tuple[Annotated[StrictStr, Field(min_length=1, max_length=1_000)], ...]

    @field_validator("criterion_id")
    @classmethod
    def validate_criterion_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError("criterion evidence must use a stable criterion id")
        return value

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(canonical_repository_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("criterion changed paths must be unique")
        return values

    @field_validator("verification_command_ids", "evidence")
    @classmethod
    def validate_unique_non_empty(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values or len(values) != len(set(values)):
            raise ValueError("criterion evidence fields must be non-empty and unique")
        return values


class CheckClaim(ClosedModel):
    id: StrictStr
    stated_outcome: Literal["passed", "failed", "not_run"]


class ImplementerResult(ClosedModel):
    outcome: Literal["changes_ready", "candidate_committed", "blocked"]
    summary: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]
    candidate_commit: StrictStr | None
    declared_changed_paths: Annotated[
        tuple[Annotated[StrictStr, Field(min_length=1, max_length=512)], ...],
        Field(
            description=(
                "Exact pending paths changed since this invocation's starting commit; during a "
                "revision, exclude unchanged files inherited from the reviewed candidate."
            )
        ),
    ]
    focused_checks: tuple[CheckClaim, ...]
    criterion_evidence: tuple[CriterionEvidence, ...]
    remaining_concerns: tuple[Annotated[StrictStr, Field(min_length=1, max_length=1_000)], ...]
    blocked_reason: Annotated[StrictStr, Field(min_length=1, max_length=2_000)] | None

    @field_validator("candidate_commit")
    @classmethod
    def validate_candidate_commit(cls, value: str | None) -> str | None:
        if value is not None and not FULL_SHA_RE.fullmatch(value):
            raise ValueError("candidate_commit must be a lowercase full Git SHA")
        return value

    @field_validator("declared_changed_paths")
    @classmethod
    def validate_declared_changed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(canonical_repository_path(value) for value in values)
        if len(normalized) != len(set(normalized)):
            raise ValueError("declared changed paths must be unique")
        return values

    @model_validator(mode="after")
    def validate_outcome(self) -> ImplementerResult:
        if self.outcome == "candidate_committed":
            if self.candidate_commit is None or self.blocked_reason is not None:
                raise ValueError("candidate_committed requires only candidate_commit")
        elif self.outcome == "changes_ready":
            if (
                self.candidate_commit is not None
                or self.blocked_reason is not None
                or not self.declared_changed_paths
            ):
                raise ValueError("changes_ready requires declared paths and no commit or block")
        elif (
            self.candidate_commit is not None
            or self.blocked_reason is None
            or self.declared_changed_paths
        ):
            raise ValueError("blocked requires only blocked_reason")
        return self


class FindingSeverity(StrEnum):
    BLOCKING = "blocking"
    IMPORTANT = "important"
    MINOR = "minor"


class ReviewFinding(ClosedModel):
    id: StrictStr
    severity: FindingSeverity
    criterion_id: StrictStr
    path: Annotated[StrictStr, Field(min_length=1, max_length=512)]
    problem: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]
    required_fix: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        canonical_repository_path(value)
        return value

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError(f"finding id must match {STEP_ID_RE.pattern!r}")
        return value

    @field_validator("criterion_id")
    @classmethod
    def validate_criterion_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError("review finding must use a stable criterion id")
        return value


class ReviewerResult(ClosedModel):
    decision: Literal["approved", "revision_required", "blocked"]
    summary: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]
    findings: tuple[ReviewFinding, ...]
    acceptance_coverage: tuple[StrictStr, ...]
    residual_risks: tuple[Annotated[StrictStr, Field(min_length=1, max_length=1_000)], ...]

    @model_validator(mode="after")
    def validate_decision(self) -> ReviewerResult:
        blocking = any(finding.severity is FindingSeverity.BLOCKING for finding in self.findings)
        if self.decision == "approved" and blocking:
            raise ValueError("an approved review cannot contain a blocking finding")
        if self.decision == "revision_required" and not self.findings:
            raise ValueError("revision_required must contain at least one finding")
        if not self.acceptance_coverage or len(self.acceptance_coverage) != len(
            set(self.acceptance_coverage)
        ):
            raise ValueError("review acceptance coverage must be non-empty and unique")
        return self


def canonical_event_json(event: EventEnvelope) -> str:
    """Stable local representation used for event identity and persistence."""

    return json.dumps(
        event.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def event_sha256(event: EventEnvelope) -> str:
    return hashlib.sha256(canonical_event_json(event).encode("utf-8")).hexdigest()
