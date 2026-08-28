"""Closed contracts for the shared Technocore engineering conversation."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Literal, Protocol
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

from technocore_orchestrator.domain.models import (
    FULL_SHA_RE,
    RUN_ID_RE,
    SHA256_RE,
    STEP_ID_RE,
    TASK_ID_RE,
    CriterionEvidence,
    FindingSeverity,
    ImplementerResult,
    PlannerResult,
    ReviewerResult,
    Role,
    canonical_repository_path,
)


class ClosedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CollaborationPhase(StrEnum):
    PROPOSE_PLAN = "propose_plan"
    CHALLENGE_PLAN = "challenge_plan"
    FINALIZE_PLAN = "finalize_plan"
    IMPLEMENT = "implement"
    REVISE = "revise"
    REVIEW = "review"


class CollaborationKind(StrEnum):
    PLAN_PROPOSED = "plan_proposed"
    PLAN_CHALLENGED = "plan_challenged"
    PLAN_FINALIZED = "plan_finalized"
    HANDOFF_ACKNOWLEDGED = "handoff_acknowledged"
    IMPLEMENTATION_SUBMITTED = "implementation_submitted"
    CANDIDATE_READY = "candidate_ready"
    FINDINGS_RESOLVED = "findings_resolved"
    REVIEW_SUBMITTED = "review_submitted"


class CollaborationEnvelope(ClosedModel):
    v: Literal[1]
    channel: Literal["collaboration"] = "collaboration"
    event_id: UUID
    run_id: StrictStr
    task_id: StrictStr
    kind: CollaborationKind
    sender: Role
    created_at: datetime
    reply_to: UUID | None
    text: Annotated[StrictStr, Field(min_length=1, max_length=600)]
    payload_sha256: StrictStr

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

    @field_validator("payload_sha256")
    @classmethod
    def validate_payload_sha256(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("collaboration payload digest must be a lowercase SHA-256")
        return value


class PlanChallengeIssue(ClosedModel):
    id: StrictStr
    severity: FindingSeverity
    criterion_id: StrictStr
    rationale: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]
    recommendation: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError(f"challenge issue id must match {STEP_ID_RE.pattern!r}")
        return value

    @field_validator("criterion_id")
    @classmethod
    def validate_criterion_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError("challenge issue must use a stable criterion id")
        return value


class PlanChallengeResult(ClosedModel):
    decision: Literal["approved", "changes_requested", "blocked"]
    summary: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]
    issues: tuple[PlanChallengeIssue, ...]

    @field_validator("issues")
    @classmethod
    def validate_issues(
        cls, values: tuple[PlanChallengeIssue, ...]
    ) -> tuple[PlanChallengeIssue, ...]:
        ids = tuple(issue.id for issue in values)
        if len(ids) != len(set(ids)):
            raise ValueError("plan challenge issue ids must be unique")
        return values

    @model_validator(mode="after")
    def validate_decision(self) -> PlanChallengeResult:
        if self.decision == "approved" and self.issues:
            raise ValueError("an approved plan challenge cannot contain issues")
        if self.decision == "changes_requested" and not self.issues:
            raise ValueError("changes_requested requires at least one challenge issue")
        return self


class FindingResolution(ClosedModel):
    finding_id: StrictStr
    status: Literal["fixed", "rejected"]
    evidence: tuple[Annotated[StrictStr, Field(min_length=1, max_length=1_000)], ...]

    @field_validator("finding_id")
    @classmethod
    def validate_finding_id(cls, value: str) -> str:
        if not STEP_ID_RE.fullmatch(value):
            raise ValueError(f"finding id must match {STEP_ID_RE.pattern!r}")
        return value


class FindingsResolutionResult(ClosedModel):
    summary: Annotated[StrictStr, Field(min_length=1, max_length=2_000)]
    resolutions: tuple[FindingResolution, ...]

    @field_validator("resolutions")
    @classmethod
    def validate_resolutions(
        cls, values: tuple[FindingResolution, ...]
    ) -> tuple[FindingResolution, ...]:
        if not values:
            raise ValueError("a revision must resolve at least one finding")
        ids = tuple(resolution.finding_id for resolution in values)
        if len(ids) != len(set(ids)):
            raise ValueError("finding resolutions must be unique")
        return values


class HandoffAcknowledgement(ClosedModel):
    event_id: UUID
    sequence: Annotated[StrictInt, Field(ge=1)]
    payload_sha256: StrictStr

    @field_validator("payload_sha256")
    @classmethod
    def validate_payload_sha256(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("acknowledged payload digest must be a lowercase SHA-256")
        return value


class CandidateHandoff(ClosedModel):
    candidate_commit: StrictStr
    diff_sha256: StrictStr
    plan_sha256: StrictStr
    changed_paths: tuple[Annotated[StrictStr, Field(min_length=1, max_length=512)], ...]
    criterion_evidence: tuple[CriterionEvidence, ...]

    @field_validator("candidate_commit")
    @classmethod
    def validate_candidate_commit(cls, value: str) -> str:
        if not FULL_SHA_RE.fullmatch(value):
            raise ValueError("candidate handoff requires a lowercase full Git SHA")
        return value

    @field_validator("diff_sha256", "plan_sha256")
    @classmethod
    def validate_sha256(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("candidate handoff digests must be lowercase SHA-256 values")
        return value

    @field_validator("changed_paths")
    @classmethod
    def validate_changed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(canonical_repository_path(value) for value in values)
        if not normalized or len(normalized) != len(set(normalized)):
            raise ValueError("candidate handoff paths must be non-empty and unique")
        return values


class CandidateSubmission(ImplementerResult):
    """Live gateway contract that rejects inconsistent evidence before publication."""

    @model_validator(mode="after")
    def validate_current_change_evidence(self) -> CandidateSubmission:
        if self.outcome == "blocked":
            if self.criterion_evidence:
                raise ValueError("a blocked candidate submission cannot claim criterion evidence")
            return self
        evidence_ids = tuple(item.criterion_id for item in self.criterion_evidence)
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("candidate submission criterion evidence must be unique")
        evidence_paths = {
            canonical_repository_path(path)
            for item in self.criterion_evidence
            for path in item.changed_paths
        }
        declared_paths = {canonical_repository_path(path) for path in self.declared_changed_paths}
        if evidence_paths != declared_paths:
            raise ValueError(
                "criterion evidence changed_paths must exactly match "
                "declared_changed_paths for the current invocation"
            )
        return self


type CollaborationPayload = (
    PlannerResult
    | PlanChallengeResult
    | ImplementerResult
    | CandidateHandoff
    | FindingsResolutionResult
    | ReviewerResult
    | HandoffAcknowledgement
)

type ModelResult = PlannerResult | PlanChallengeResult | ImplementerResult | ReviewerResult


class ConversationMessage(ClosedModel):
    sequence: Annotated[StrictInt, Field(ge=1)]
    event_id: UUID
    sender: Role
    kind: CollaborationKind
    reply_to: UUID | None
    text: Annotated[StrictStr, Field(min_length=1, max_length=600)]
    payload_sha256: StrictStr
    payload: dict[str, object]

    @field_validator("payload_sha256")
    @classmethod
    def validate_payload_sha256(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("conversation payload digest must be a lowercase SHA-256")
        return value


class ConversationWindow(ClosedModel):
    cursor_before: Annotated[StrictInt, Field(ge=0)]
    cursor_after: Annotated[StrictInt, Field(ge=0)]
    gap_detected: bool
    messages: tuple[ConversationMessage, ...]


class PublicationResult(ClosedModel):
    sequence: Annotated[StrictInt, Field(ge=1)]
    event_id: UUID
    payload_sha256: StrictStr

    @field_validator("payload_sha256")
    @classmethod
    def validate_payload_sha256(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("publication digest must be a lowercase SHA-256")
        return value


class CollaborationBackend(Protocol):
    async def read_new_messages(self, role: Role, after_sequence: int) -> ConversationWindow: ...

    async def publish(
        self,
        *,
        role: Role,
        kind: CollaborationKind,
        payload: CollaborationPayload,
        reply_to: UUID | None,
    ) -> PublicationResult: ...

    async def acknowledge(
        self,
        *,
        role: Role,
        event_id: UUID,
        sequence: int,
        payload_sha256: str,
    ) -> PublicationResult: ...


def canonical_collaboration_payload(payload: CollaborationPayload) -> str:
    return json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def collaboration_payload_sha256(payload: CollaborationPayload) -> str:
    return hashlib.sha256(canonical_collaboration_payload(payload).encode("utf-8")).hexdigest()


def canonical_collaboration_envelope(envelope: CollaborationEnvelope) -> str:
    return json.dumps(
        envelope.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def parse_collaboration_payload(kind: CollaborationKind, serialized: str) -> CollaborationPayload:
    model = {
        CollaborationKind.PLAN_PROPOSED: PlannerResult,
        CollaborationKind.PLAN_FINALIZED: PlannerResult,
        CollaborationKind.PLAN_CHALLENGED: PlanChallengeResult,
        CollaborationKind.HANDOFF_ACKNOWLEDGED: HandoffAcknowledgement,
        CollaborationKind.IMPLEMENTATION_SUBMITTED: ImplementerResult,
        CollaborationKind.CANDIDATE_READY: CandidateHandoff,
        CollaborationKind.FINDINGS_RESOLVED: FindingsResolutionResult,
        CollaborationKind.REVIEW_SUBMITTED: ReviewerResult,
    }[kind]
    return model.model_validate_json(serialized)
