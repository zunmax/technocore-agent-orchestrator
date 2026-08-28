"""Deterministic single-host workflow orchestration."""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import uuid4

from technocore_orchestrator.adapters import HarnessAdapter, HarnessInvocation
from technocore_orchestrator.config import LoadedConfig
from technocore_orchestrator.domain.collaboration import (
    CandidateHandoff,
    CollaborationBackend,
    CollaborationKind,
    CollaborationPhase,
    FindingsResolutionResult,
    HandoffAcknowledgement,
    ModelResult,
    PlanChallengeResult,
    collaboration_payload_sha256,
)
from technocore_orchestrator.domain.models import (
    ArtifactReference,
    ArtifactType,
    EventEnvelope,
    EventKind,
    HarnessKind,
    ImplementerResult,
    PlannerResult,
    ReviewerResult,
    Role,
    RunState,
    TerminationReason,
    canonical_repository_path,
)
from technocore_orchestrator.errors import (
    ErrorCategory,
    ExecutionError,
    ProtocolError,
    RoleResultValidationError,
    StateError,
    WorkflowError,
)
from technocore_orchestrator.gateway import RoleGateway
from technocore_orchestrator.prompts import build_role_prompt
from technocore_orchestrator.publication import EventPublisher, PublicationReceipt
from technocore_orchestrator.storage import (
    CheckEvidence,
    CheckRecord,
    InvocationStatus,
    ParticipantEvidence,
    RunRecord,
    SQLiteStore,
    StoredCollaborationMessage,
    StoredRoleResult,
    TransportStatus,
    WorktreeStatus,
)
from technocore_orchestrator.technocore import WriteOutcomeUnknownError
from technocore_orchestrator.verification import (
    CheckResult,
    VerificationJob,
    VerificationSuiteResult,
    Verifier,
)
from technocore_orchestrator.worktrees import CandidateValidation, ManagedWorktree, WorktreeManager


@dataclass(frozen=True, slots=True)
class WorkflowOutcome:
    run_id: str
    candidate_commit: str
    verification: VerificationSuiteResult
    worktrees: tuple[ManagedWorktree, ...]


class WorkflowOrchestrator:
    """The only component authorized to turn role results into workflow transitions."""

    def __init__(
        self,
        *,
        loaded_config: LoadedConfig,
        store: SQLiteStore,
        worktrees: WorktreeManager,
        adapters: Mapping[HarnessKind, HarnessAdapter],
        publisher: EventPublisher,
        verifier: Verifier,
        verification_jobs: tuple[VerificationJob, ...],
        collaboration: CollaborationBackend | None = None,
    ) -> None:
        self._loaded = loaded_config
        self._config = loaded_config.config
        self._store = store
        self._worktrees = worktrees
        self._adapters = dict(adapters)
        self._publisher = publisher
        self._verifier = verifier
        self._verification_jobs = verification_jobs
        self._collaboration = collaboration

    async def run(
        self, run_id: str, *, participants: tuple[ParticipantEvidence, ...] = ()
    ) -> WorkflowOutcome:
        """Execute the first complete planner/implementer/reviewer/verifier path."""

        run_created = False
        try:
            async with asyncio.timeout(self._config.limits.run_wall_seconds):
                snapshot = await self._worktrees.prepare()
                if (
                    snapshot.path != self._config.repository.path
                    or snapshot.base_commit != self._config.repository.base_commit
                ):
                    raise StateError(
                        "prepared repository does not match the validated run configuration"
                    )
                self._store.create_run(
                    run_id=run_id,
                    task_id=self._config.task.id,
                    config_digest=self._loaded.sha256,
                    repository_path=snapshot.path,
                    base_commit=snapshot.base_commit,
                    participants=participants,
                )
                run_created = True
                return await self._run_created(run_id, snapshot.base_commit)
        except TimeoutError as exc:
            if run_created:
                await self._terminalize_run(
                    run_id,
                    kind=EventKind.RUN_FAILED,
                    summary="The absolute run wall-clock deadline was exceeded.",
                )
            raise ExecutionError("run exceeded its absolute wall-clock deadline") from exc
        except asyncio.CancelledError:
            if run_created:
                await asyncio.shield(
                    self._terminalize_run(
                        run_id,
                        kind=EventKind.RUN_CANCELED,
                        summary="The run was canceled while work was in progress.",
                    )
                )
            raise
        except WorkflowError as exc:
            if run_created:
                await self._terminalize_run(
                    run_id,
                    kind=EventKind.RUN_FAILED,
                    summary=f"The run stopped after a {exc.category.value} failure.",
                )
            raise
        except Exception:
            if run_created:
                await self._terminalize_run(
                    run_id,
                    kind=EventKind.RUN_FAILED,
                    summary="The run stopped after an internal failure.",
                )
            raise

    async def resume(
        self, run_id: str, *, participants: tuple[ParticipantEvidence, ...] = ()
    ) -> WorkflowOutcome | None:
        """Reconcile durable side effects without repeating provider invocations."""

        validated_run = False
        try:
            run = self._store.get_run(run_id)
            self._validate_resume_record(run)
            self._store.record_participant_set(run_id, participants)
            validated_run = True
            if run.state.is_terminal:
                snapshot = await self._worktrees.prepare()
                self._validate_resume_snapshot(snapshot.path, snapshot.base_commit)
                await self.reconcile_transports(run_id)
                return await self._terminal_outcome(run_id)
            deadline = run.created_at.timestamp() + self._config.limits.run_wall_seconds
            remaining = deadline - datetime.now(UTC).timestamp()
            if remaining <= 0:
                await self._terminalize_run(
                    run_id,
                    kind=EventKind.RUN_FAILED,
                    summary="The absolute run wall-clock deadline was exceeded before resume.",
                )
                raise ExecutionError("run exceeded its absolute wall-clock deadline")
            async with asyncio.timeout(remaining):
                snapshot = await self._worktrees.prepare()
                self._validate_resume_snapshot(snapshot.path, snapshot.base_commit)
                await self.reconcile_transports(run_id)
                return await self._drive_run(run_id, snapshot.base_commit)
        except TimeoutError as exc:
            if validated_run:
                await self._terminalize_run(
                    run_id,
                    kind=EventKind.RUN_FAILED,
                    summary="The absolute run wall-clock deadline was exceeded during resume.",
                )
            raise ExecutionError("run exceeded its absolute wall-clock deadline") from exc
        except asyncio.CancelledError:
            if validated_run:
                await asyncio.shield(
                    self._terminalize_run(
                        run_id,
                        kind=EventKind.RUN_CANCELED,
                        summary="The resumed run was canceled while work was in progress.",
                    )
                )
            raise
        except WorkflowError as exc:
            if validated_run:
                await self._terminalize_run(
                    run_id,
                    kind=EventKind.RUN_FAILED,
                    summary=f"The resumed run stopped after a {exc.category.value} failure.",
                )
            raise
        except Exception:
            if validated_run:
                await self._terminalize_run(
                    run_id,
                    kind=EventKind.RUN_FAILED,
                    summary="The resumed run stopped after an internal failure.",
                )
            raise

    async def cancel(self, run_id: str) -> RunRecord:
        """Cancel an idle run after proving no harness invocation remains active."""

        run = self._store.get_run(run_id)
        self._validate_resume_record(run)
        if run.state.is_terminal:
            await self.reconcile_transports(run_id)
            return run
        active = tuple(
            invocation
            for invocation in self._store.list_invocations(run_id)
            if invocation.status is InvocationStatus.STARTED
        )
        if active:
            raise StateError(
                "cannot cancel an idle run with an unresolved started invocation",
                context={"active_invocations": len(active)},
            )
        snapshot = await self._worktrees.prepare()
        self._validate_resume_snapshot(snapshot.path, snapshot.base_commit)
        await self.reconcile_transports(run_id)
        await self._terminalize_run(
            run_id,
            kind=EventKind.RUN_CANCELED,
            summary="The operator canceled the idle run.",
        )
        return self._store.get_run(run_id)

    def _validate_resume_record(self, run: RunRecord) -> None:
        if (
            run.task_id != self._config.task.id
            or run.config_digest != self._loaded.sha256
            or run.repository_path != self._config.repository.path
            or run.base_commit != self._config.repository.base_commit
        ):
            raise StateError("resume configuration does not match the durable run record")

    def _validate_resume_snapshot(self, path: Path, base_commit: str) -> None:
        if (
            path != self._config.repository.path
            or base_commit != self._config.repository.base_commit
        ):
            raise StateError("prepared repository does not match the durable run record")

    async def _terminal_outcome(self, run_id: str) -> WorkflowOutcome | None:
        events = self._store.list_events(run_id)
        candidates = tuple(
            _candidate_from_event(event)
            for event in events
            if event.kind is EventKind.IMPLEMENTATION_READY
        )
        if not candidates:
            return None
        candidate = candidates[-1]
        records = tuple(
            check
            for check in self._store.list_checks(run_id)
            if check.candidate_commit == candidate
        )
        if not records:
            if self._store.get_run(run_id).state is RunState.COMPLETED:
                raise StateError("completed run is missing verifier evidence")
            return None
        worktrees = await self._restore_worktrees(run_id)
        return self._workflow_outcome(
            run_id,
            candidate,
            self._verification_from_records(records),
            worktrees,
        )

    async def _run_created(self, run_id: str, base_commit: str) -> WorkflowOutcome:
        await self._emit(
            run_id=run_id,
            kind=EventKind.RUN_STARTED,
            sender=Role.SUPERVISOR,
            attempt=1,
            summary="The supervisor accepted the run configuration and immutable base commit.",
        )
        return await self._drive_run(run_id, base_commit)

    async def _drive_run(self, run_id: str, base_commit: str) -> WorkflowOutcome:
        worktrees = await self._restore_worktrees(run_id)
        while True:
            state = self._store.get_run(run_id).state
            events = self._store.list_events(run_id)
            if state is RunState.CREATED:
                await self._emit(
                    run_id=run_id,
                    kind=EventKind.RUN_STARTED,
                    sender=Role.SUPERVISOR,
                    attempt=1,
                    summary=(
                        "The supervisor resumed the validated run at its immutable base commit."
                    ),
                )
                continue
            if state is RunState.PLANNING:
                planner_tree = await self._ensure_worktree(
                    worktrees, run_id, Role.PLANNER, base_commit
                )
                plans = self._store.list_role_results(run_id, Role.PLANNER)
                if len(plans) > 1:
                    raise StateError("run contains multiple successful planner results")
                if plans:
                    planner_result = _require_planner_result(plans[0])
                    planner_attempt = plans[0].attempt
                else:
                    planner_result, planner_attempt = await self._invoke_planner(
                        run_id, planner_tree
                    )
                await self._emit(
                    run_id=run_id,
                    kind=EventKind.PLAN_PROPOSED,
                    sender=Role.PLANNER,
                    attempt=planner_attempt,
                    summary=_event_summary(planner_result.summary),
                )
                continue

            if state is RunState.CHALLENGING:
                proposed_plan = self._proposed_plan(run_id)
                challenger_tree = await self._ensure_worktree(
                    worktrees, run_id, Role.IMPLEMENTER, base_commit
                )
                challenges = tuple(
                    result
                    for result in self._store.list_role_results(run_id, Role.IMPLEMENTER)
                    if isinstance(result.result, PlanChallengeResult)
                )
                if len(challenges) > 1:
                    raise StateError("run contains multiple successful plan challenges")
                if challenges:
                    challenge = _require_plan_challenge(challenges[0])
                    challenge_attempt = challenges[0].attempt
                else:
                    challenge, challenge_attempt = await self._invoke_plan_challenger(
                        run_id, challenger_tree, proposed_plan
                    )
                await self._emit(
                    run_id=run_id,
                    kind=EventKind.PLAN_CHALLENGED,
                    sender=Role.IMPLEMENTER,
                    attempt=challenge_attempt,
                    summary=_event_summary(challenge.summary),
                )
                continue

            if state is RunState.FINALIZING:
                proposed_plan = self._proposed_plan(run_id)
                challenge = self._stored_plan_challenge(run_id)
                planner_tree = await self._ensure_worktree(
                    worktrees, run_id, Role.PLANNER, base_commit
                )
                plans = self._store.list_role_results(run_id, Role.PLANNER)
                if len(plans) not in {1, 2}:
                    raise StateError(
                        "planner results do not match proposal/finalization checkpoints"
                    )
                if len(plans) == 1:
                    finalized_plan, planner_attempt = await self._invoke_plan_finalizer(
                        run_id,
                        planner_tree,
                        proposed_plan,
                        challenge,
                    )
                else:
                    finalized_plan = _require_planner_result(plans[-1])
                    planner_attempt = plans[-1].attempt
                await self._emit(
                    run_id=run_id,
                    kind=EventKind.PLAN_FINALIZED,
                    sender=Role.PLANNER,
                    attempt=planner_attempt,
                    summary=_event_summary(finalized_plan.summary),
                )
                continue

            planner_result = self._stored_plan(run_id)
            if state is RunState.READY:
                await self._emit(
                    run_id=run_id,
                    kind=EventKind.IMPLEMENTATION_STARTED,
                    sender=Role.SUPERVISOR,
                    attempt=self._next_invocation_attempt(run_id, Role.IMPLEMENTER),
                    summary="The supervisor authorized one scoped implementation attempt.",
                )
                continue

            if state is RunState.IMPLEMENTING:
                implementation_events = tuple(
                    event for event in events if event.kind is EventKind.IMPLEMENTATION_READY
                )
                prior_candidate = (
                    _candidate_from_event(implementation_events[-1])
                    if implementation_events
                    else base_commit
                )
                implementer_tree = await self._ensure_worktree(
                    worktrees, run_id, Role.IMPLEMENTER, base_commit
                )
                implementer_results = tuple(
                    result
                    for result in self._store.list_role_results(run_id, Role.IMPLEMENTER)
                    if isinstance(result.result, ImplementerResult)
                )
                completed_cycles = len(implementation_events)
                if len(implementer_results) not in {
                    completed_cycles,
                    completed_cycles + 1,
                }:
                    raise StateError(
                        "implementer results do not match durable candidate checkpoints"
                    )
                revision_request = (
                    self._latest_revision_request(run_id, events) if completed_cycles else None
                )
                baseline_tree = replace(implementer_tree, head_commit=prior_candidate)
                if len(implementer_results) == completed_cycles:
                    implementer_result, implementer_attempt = await self._invoke_implementer(
                        run_id,
                        baseline_tree,
                        planner_result,
                        revision_request=revision_request,
                    )
                else:
                    stored = implementer_results[-1]
                    implementer_result = _require_implementer_result(stored)
                    implementer_attempt = stored.attempt
                validated_candidate, implementer_tree = await self._materialize_candidate(
                    run_id,
                    baseline_tree,
                    implementer_result,
                )
                candidate = validated_candidate.commit
                worktrees[Role.IMPLEMENTER] = implementer_tree
                await self._publish_candidate_handoff(
                    run_id=run_id,
                    candidate=validated_candidate,
                    plan=planner_result,
                    implementation=implementer_result,
                )
                await self._emit(
                    run_id=run_id,
                    kind=EventKind.IMPLEMENTATION_READY,
                    sender=Role.IMPLEMENTER,
                    attempt=implementer_attempt,
                    summary=_event_summary(implementer_result.summary),
                    artifact=ArtifactReference(type=ArtifactType.GIT_COMMIT, value=candidate),
                )
                continue

            candidate = self._latest_candidate(events)
            if state is RunState.REVIEWING:
                reviewer_tree = await self._ensure_worktree(
                    worktrees, run_id, Role.REVIEWER, candidate
                )
                decision_events = tuple(
                    event
                    for event in events
                    if event.kind in {EventKind.REVISION_REQUIRED, EventKind.REVIEW_APPROVED}
                )
                reviewer_results = self._store.list_role_results(run_id, Role.REVIEWER)
                if len(reviewer_results) not in {
                    len(decision_events),
                    len(decision_events) + 1,
                }:
                    raise StateError("reviewer results do not match durable review checkpoints")
                if len(reviewer_results) == len(decision_events):
                    reviewer_result, reviewer_attempt = await self._invoke_reviewer(
                        run_id, reviewer_tree, candidate, planner_result
                    )
                else:
                    stored = reviewer_results[-1]
                    reviewer_result = _require_reviewer_result(stored)
                    reviewer_attempt = stored.attempt
                if reviewer_result.decision == "approved":
                    await self._emit(
                        run_id=run_id,
                        kind=EventKind.REVIEW_APPROVED,
                        sender=Role.REVIEWER,
                        attempt=reviewer_attempt,
                        summary=_event_summary(reviewer_result.summary),
                    )
                    continue
                if reviewer_result.decision != "revision_required":
                    raise StateError("reviewer blocked the candidate")
                if (
                    self._store.get_run_counters(run_id).revision_cycles
                    >= self._config.limits.max_revision_cycles
                ):
                    raise StateError("review revision limit is exhausted")
                await self._emit(
                    run_id=run_id,
                    kind=EventKind.REVISION_REQUIRED,
                    sender=Role.REVIEWER,
                    attempt=reviewer_attempt,
                    summary=_event_summary(reviewer_result.summary),
                    artifact=ArtifactReference(type=ArtifactType.GIT_COMMIT, value=candidate),
                )
                continue

            if state is RunState.VERIFYING:
                verifier_tree = await self._ensure_worktree(
                    worktrees, run_id, Role.VERIFIER, candidate
                )
                check_records = tuple(
                    check
                    for check in self._store.list_checks(run_id)
                    if check.candidate_commit == candidate
                )
                if check_records:
                    verification = self._verification_from_records(check_records)
                else:
                    verification = await self._verifier.run(
                        worktree=verifier_tree.path,
                        jobs=self._verification_jobs,
                    )
                    self._record_checks(run_id, candidate, verification)
                await self._emit_verification(run_id, candidate, verification)
                return self._workflow_outcome(run_id, candidate, verification, worktrees)

            raise StateError("run state has no recovery driver", context={"state": state})

    async def _emit_verification(
        self,
        run_id: str,
        candidate: str,
        verification: VerificationSuiteResult,
    ) -> None:
        await self._emit(
            run_id=run_id,
            kind=(
                EventKind.VERIFICATION_PASSED
                if verification.passed
                else EventKind.VERIFICATION_FAILED
            ),
            sender=Role.VERIFIER,
            attempt=1,
            summary=(
                "All required deterministic verification commands passed."
                if verification.passed
                else "At least one required deterministic verification command failed."
            ),
            artifact=ArtifactReference(type=ArtifactType.GIT_COMMIT, value=candidate),
        )

    async def _invoke_planner(
        self, run_id: str, worktree: ManagedWorktree
    ) -> tuple[PlannerResult, int]:
        result, attempt = await self._invoke_with_schema_repair(
            run_id,
            Role.PLANNER,
            worktree,
            self._config.repository.base_commit,
            phase=CollaborationPhase.PROPOSE_PLAN,
        )
        result = cast(PlannerResult, result)
        if result.blocked_reason is not None:
            raise StateError("planner reported that the task is blocked")
        return result, attempt

    async def _invoke_plan_challenger(
        self,
        run_id: str,
        worktree: ManagedWorktree,
        proposed_plan: PlannerResult,
    ) -> tuple[PlanChallengeResult, int]:
        result, attempt = await self._invoke_with_schema_repair(
            run_id,
            Role.IMPLEMENTER,
            worktree,
            self._config.repository.base_commit,
            accepted_plan=proposed_plan,
            phase=CollaborationPhase.CHALLENGE_PLAN,
        )
        result = cast(PlanChallengeResult, result)
        if result.decision == "blocked":
            raise StateError("plan challenger reported that the task is blocked")
        return result, attempt

    async def _invoke_plan_finalizer(
        self,
        run_id: str,
        worktree: ManagedWorktree,
        proposed_plan: PlannerResult,
        challenge: PlanChallengeResult,
    ) -> tuple[PlannerResult, int]:
        result, attempt = await self._invoke_with_schema_repair(
            run_id,
            Role.PLANNER,
            worktree,
            self._config.repository.base_commit,
            accepted_plan=proposed_plan,
            plan_challenge=challenge,
            phase=CollaborationPhase.FINALIZE_PLAN,
        )
        result = cast(PlannerResult, result)
        if result.blocked_reason is not None:
            raise StateError("planner reported that finalization is blocked")
        return result, attempt

    async def _invoke_implementer(
        self,
        run_id: str,
        worktree: ManagedWorktree,
        plan: PlannerResult,
        *,
        revision_request: ReviewerResult | None = None,
    ) -> tuple[ImplementerResult, int]:
        result, attempt = await self._invoke_with_schema_repair(
            run_id,
            Role.IMPLEMENTER,
            worktree,
            worktree.head_commit,
            accepted_plan=plan,
            revision_request=revision_request,
        )
        result = cast(ImplementerResult, result)
        if result.blocked_reason is not None:
            raise StateError("implementer reported that the task is blocked")
        return result, attempt

    async def _invoke_reviewer(
        self,
        run_id: str,
        worktree: ManagedWorktree,
        candidate: str,
        plan: PlannerResult,
    ) -> tuple[ReviewerResult, int]:
        result, attempt = await self._invoke_with_schema_repair(
            run_id, Role.REVIEWER, worktree, candidate, accepted_plan=plan
        )
        result = cast(ReviewerResult, result)
        return result, attempt

    async def _invoke_with_schema_repair(
        self,
        run_id: str,
        role: Role,
        worktree: ManagedWorktree,
        commit: str,
        *,
        accepted_plan: PlannerResult | None = None,
        plan_challenge: PlanChallengeResult | None = None,
        revision_request: ReviewerResult | None = None,
        phase: CollaborationPhase | None = None,
    ) -> tuple[ModelResult, int]:
        attempt = self._next_invocation_attempt(run_id, role)
        repair_index = 0
        while True:
            try:
                result = await self._invoke(
                    run_id,
                    role,
                    worktree,
                    commit,
                    attempt=attempt,
                    accepted_plan=accepted_plan,
                    plan_challenge=plan_challenge,
                    revision_request=revision_request,
                    schema_repair_attempt=repair_index or None,
                    phase=phase,
                )
                return result, attempt
            except RoleResultValidationError:
                if repair_index >= self._config.limits.max_schema_repairs:
                    raise
                repair_index += 1
                attempt += 1

    def _next_invocation_attempt(self, run_id: str, role: Role) -> int:
        all_invocations = self._store.list_invocations(run_id)
        if len(all_invocations) >= self._config.limits.max_model_invocations:
            raise StateError(
                "workflow exhausted its configured model-invocation limit",
                context={"max_model_invocations": self._config.limits.max_model_invocations},
            )
        role_invocations = tuple(record for record in all_invocations if record.role is role)
        if role_invocations and role_invocations[-1].status is InvocationStatus.STARTED:
            raise StateError("cannot repeat a harness attempt with an unknown terminal outcome")
        attempts = tuple(record.attempt for record in role_invocations)
        attempt = max(attempts, default=0) + 1
        if attempt > 100:
            raise StateError("role invocation attempt space is exhausted")
        return attempt

    async def _restore_worktrees(self, run_id: str) -> dict[Role, ManagedWorktree]:
        restored: dict[Role, ManagedWorktree] = {}
        for record in self._store.list_worktrees(run_id):
            if record.status is WorktreeStatus.REMOVED:
                raise StateError("removed worktree cannot be used to resume a run")
            observation = self._store.get_latest_worktree_observation(run_id, record.role)
            worktree = ManagedWorktree(
                run_id=run_id,
                task_id=self._config.task.id,
                role=record.role,
                path=record.path,
                head_commit=observation.head_commit,
                branch=record.branch,
                writable=record.writable,
            )
            worktree = await self._worktrees.refresh(worktree)
            clean = await self._worktrees.is_clean(worktree)
            if worktree.role is not Role.IMPLEMENTER and not clean:
                raise StateError("read-only role worktree became dirty before resume")
            self._store.observe_worktree(
                run_id=run_id,
                role=worktree.role,
                head_commit=worktree.head_commit,
                clean=clean,
                changed_paths=observation.changed_paths,
            )
            restored[record.role] = worktree
        return restored

    async def _ensure_worktree(
        self,
        worktrees: dict[Role, ManagedWorktree],
        run_id: str,
        role: Role,
        commit: str,
    ) -> ManagedWorktree:
        worktree = worktrees.get(role)
        if worktree is None:
            created: list[ManagedWorktree] = []
            worktree = await self._create_worktree(created, run_id, role, commit)
            worktrees[role] = worktree
            return worktree
        if role is not Role.IMPLEMENTER and worktree.head_commit != commit:
            worktree = await self._worktrees.retarget_readonly(worktree, commit)
            self._store.observe_worktree(
                run_id=run_id,
                role=role,
                head_commit=commit,
                clean=True,
            )
            worktrees[role] = worktree
        return worktree

    def _stored_plan(self, run_id: str) -> PlannerResult:
        event = self._single_event(run_id, EventKind.PLAN_FINALIZED)
        return _require_planner_result(
            self._store.get_role_result(run_id, Role.PLANNER, event.attempt)
        )

    def _proposed_plan(self, run_id: str) -> PlannerResult:
        event = self._single_event(run_id, EventKind.PLAN_PROPOSED)
        return _require_planner_result(
            self._store.get_role_result(run_id, Role.PLANNER, event.attempt)
        )

    def _stored_plan_challenge(self, run_id: str) -> PlanChallengeResult:
        event = self._single_event(run_id, EventKind.PLAN_CHALLENGED)
        return _require_plan_challenge(
            self._store.get_role_result(run_id, Role.IMPLEMENTER, event.attempt)
        )

    def _single_event(self, run_id: str, kind: EventKind) -> EventEnvelope:
        events = tuple(event for event in self._store.list_events(run_id) if event.kind is kind)
        if len(events) != 1:
            raise StateError(
                "run does not contain exactly one required workflow checkpoint",
                context={"kind": kind, "count": len(events)},
            )
        return events[0]

    def _latest_candidate(self, events: tuple[EventEnvelope, ...]) -> str:
        candidates = tuple(
            _candidate_from_event(event)
            for event in events
            if event.kind is EventKind.IMPLEMENTATION_READY
        )
        if not candidates:
            raise StateError("run state requires a durable candidate event")
        return candidates[-1]

    def _latest_revision_request(
        self, run_id: str, events: tuple[EventEnvelope, ...]
    ) -> ReviewerResult:
        revisions = tuple(event for event in events if event.kind is EventKind.REVISION_REQUIRED)
        if not revisions:
            raise StateError("revision implementation has no durable review request")
        stored = self._store.get_role_result(run_id, Role.REVIEWER, revisions[-1].attempt)
        result = _require_reviewer_result(stored)
        if result.decision != "revision_required":
            raise StateError("revision event does not reference a revision review result")
        return result

    def _verification_from_records(
        self, records: tuple[CheckRecord, ...]
    ) -> VerificationSuiteResult:
        ordered = tuple(sorted(records, key=lambda record: record.ordinal))
        expected = tuple((job.id, job.required) for job in self._verification_jobs)
        actual = tuple((record.command_id, record.required) for record in ordered)
        if not ordered or actual != expected:
            raise StateError("stored verifier checks are missing or do not match configuration")
        return VerificationSuiteResult(
            checks=tuple(
                CheckResult(
                    id=record.command_id,
                    required=record.required,
                    passed=record.passed,
                    termination_reason=record.termination_reason,
                    returncode=record.returncode,
                    started_at=record.started_at,
                    ended_at=record.ended_at,
                    duration_seconds=record.duration_seconds,
                    stdout_sha256=record.stdout_sha256,
                    stderr_sha256=record.stderr_sha256,
                )
                for record in ordered
            )
        )

    @staticmethod
    def _workflow_outcome(
        run_id: str,
        candidate: str,
        verification: VerificationSuiteResult,
        worktrees: Mapping[Role, ManagedWorktree],
    ) -> WorkflowOutcome:
        role_order = (Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER, Role.VERIFIER)
        return WorkflowOutcome(
            run_id=run_id,
            candidate_commit=candidate,
            verification=verification,
            worktrees=tuple(worktrees[role] for role in role_order if role in worktrees),
        )

    async def _invoke(
        self,
        run_id: str,
        role: Role,
        worktree: ManagedWorktree,
        commit: str,
        *,
        attempt: int = 1,
        accepted_plan: PlannerResult | None = None,
        plan_challenge: PlanChallengeResult | None = None,
        revision_request: ReviewerResult | None = None,
        schema_repair_attempt: int | None = None,
        phase: CollaborationPhase | None = None,
    ) -> ModelResult:
        harness_kind = {
            Role.PLANNER: self._config.roles.planner,
            Role.IMPLEMENTER: self._config.roles.implementer,
            Role.REVIEWER: self._config.roles.reviewer,
        }[role]
        adapter = self._adapters.get(harness_kind)
        if adapter is None:
            raise StateError(
                "configured harness adapter is unavailable", context={"harness": harness_kind}
            )
        invocation_phase = phase or _collaboration_phase(role, revision_request)
        collaboration_phase = invocation_phase if self._collaboration is not None else None
        invocation = HarnessInvocation(
            run_id=run_id,
            task_id=self._config.task.id,
            role=role,
            phase=invocation_phase,
            attempt=attempt,
            worktree=worktree.path,
            prompt=build_role_prompt(
                self._config,
                run_id=run_id,
                role=role,
                commit=commit,
                phase=invocation_phase,
                accepted_plan=accepted_plan,
                plan_challenge=plan_challenge,
                revision_request=revision_request,
                schema_repair_attempt=schema_repair_attempt,
                collaboration_phase=collaboration_phase,
            ),
            timeout_seconds=self._config.limits.invocation_wall_seconds,
            output_limit_bytes=self._config.limits.max_output_bytes,
        )
        self._store.start_invocation(
            run_id=run_id,
            role=role,
            attempt=invocation.attempt,
            harness=harness_kind,
            timeout_seconds=invocation.timeout_seconds,
            output_limit_bytes=invocation.output_limit_bytes,
        )
        loop = asyncio.get_running_loop()
        started_monotonic = loop.time()
        collaboration_before = len(self._store.list_collaboration_messages(run_id))
        try:
            gateway: RoleGateway | None = None
            if self._collaboration is None or collaboration_phase is None:
                invoked = await adapter.invoke(invocation)
            else:
                gateway = RoleGateway(
                    role=role,
                    phase=collaboration_phase,
                    backend=self._collaboration,
                )
                try:
                    async with gateway:
                        invoked = await adapter.invoke(replace(invocation, gateway=gateway.config))
                except RoleResultValidationError as exc:
                    if len(self._store.list_collaboration_messages(run_id)) > collaboration_before:
                        raise ProtocolError(
                            "provider published collaboration but returned an invalid final result"
                        ) from exc
                    raise
            authoritative_result = invoked.role_result
            if gateway is not None and collaboration_phase is not None:
                authoritative_result = self._validate_collaboration_invocation(
                    run_id=run_id,
                    role=role,
                    phase=collaboration_phase,
                    revision_request=revision_request,
                    candidate_commit=commit,
                    prior_count=collaboration_before,
                    called_tools=gateway.called_tools,
                )
            if not _model_result_matches(role, invocation_phase, authoritative_result):
                raise ProtocolError(f"{role.value} adapter returned another role's result")
            self._validate_quality_contract(
                phase=invocation_phase,
                result=authoritative_result,
                challenge=plan_challenge,
            )
            self._store.complete_invocation(
                run_id=run_id,
                role=role,
                attempt=invocation.attempt,
                termination_reason=invoked.process.termination_reason,
                returncode=invoked.process.returncode,
                stdout_sha256=hashlib.sha256(invoked.process.stdout).hexdigest(),
                stderr_sha256=hashlib.sha256(invoked.process.stderr).hexdigest(),
                result=authoritative_result,
                duration_seconds=loop.time() - started_monotonic,
                usage=invoked.usage,
            )
            return authoritative_result
        except asyncio.CancelledError:
            with suppress(WorkflowError):
                self._store.cancel_invocation(
                    run_id=run_id,
                    role=role,
                    attempt=invocation.attempt,
                    duration_seconds=loop.time() - started_monotonic,
                )
            raise
        except WorkflowError as exc:
            termination, returncode = _process_failure_facts(exc)
            with suppress(WorkflowError):
                self._store.fail_invocation(
                    run_id=run_id,
                    role=role,
                    attempt=invocation.attempt,
                    error_category=exc.category,
                    termination_reason=termination,
                    returncode=returncode,
                    duration_seconds=loop.time() - started_monotonic,
                )
            raise
        except Exception:
            with suppress(WorkflowError):
                self._store.fail_invocation(
                    run_id=run_id,
                    role=role,
                    attempt=invocation.attempt,
                    error_category=ErrorCategory.INTERNAL,
                    duration_seconds=loop.time() - started_monotonic,
                )
            raise

    def _validate_quality_contract(
        self,
        *,
        phase: CollaborationPhase,
        result: ModelResult,
        challenge: PlanChallengeResult | None,
    ) -> None:
        criterion_ids = {
            f"criterion_{index}"
            for index, _description in enumerate(self._config.task.acceptance_criteria, start=1)
        }
        if isinstance(result, PlannerResult):
            covered = {criterion_id for step in result.steps for criterion_id in step.criterion_ids}
            if covered != criterion_ids:
                raise ProtocolError(
                    "planner result does not map every configured acceptance criterion"
                )
            if any(
                not _is_allowed_model_path(path, self._config.repository.allowed_paths)
                for step in result.steps
                for path in step.expected_paths
            ):
                raise ProtocolError("planner result names a path outside the configured scope")
            if phase is CollaborationPhase.PROPOSE_PLAN:
                if result.challenge_dispositions:
                    raise ProtocolError("proposed plan cannot resolve a future challenge")
                return
            if phase is not CollaborationPhase.FINALIZE_PLAN or challenge is None:
                raise ProtocolError("planner result appeared outside a planning phase")
            expected_issues = {issue.id for issue in challenge.issues}
            dispositions = {item.issue_id for item in result.challenge_dispositions}
            if (
                len(dispositions) != len(result.challenge_dispositions)
                or dispositions != expected_issues
            ):
                raise ProtocolError(
                    "final plan did not accept or reject every challenge issue exactly once"
                )
            return
        if isinstance(result, PlanChallengeResult):
            if phase is not CollaborationPhase.CHALLENGE_PLAN:
                raise ProtocolError("plan challenge appeared outside its bounded phase")
            if any(issue.criterion_id not in criterion_ids for issue in result.issues):
                raise ProtocolError("plan challenge names an unknown acceptance criterion")
            return
        if isinstance(result, ImplementerResult):
            if phase not in {CollaborationPhase.IMPLEMENT, CollaborationPhase.REVISE}:
                raise ProtocolError("implementation result appeared outside an edit phase")
            evidence_ids = {item.criterion_id for item in result.criterion_evidence}
            if result.outcome == "blocked":
                if result.criterion_evidence:
                    raise ProtocolError("blocked implementation cannot claim criterion evidence")
                return
            if len(evidence_ids) != len(result.criterion_evidence) or evidence_ids != criterion_ids:
                raise ProtocolError(
                    "implementation does not provide evidence for every acceptance criterion"
                )
            evidence_paths = {
                canonical_repository_path(path)
                for item in result.criterion_evidence
                for path in item.changed_paths
            }
            declared_paths = {
                canonical_repository_path(path) for path in result.declared_changed_paths
            }
            if evidence_paths != declared_paths:
                raise ProtocolError(
                    "implementation criterion evidence paths do not match declared current changes",
                    context={
                        "declared_paths": tuple(sorted(declared_paths)),
                        "evidence_paths": tuple(sorted(evidence_paths)),
                    },
                )
            command_ids = {command.id for command in self._config.verification.commands}
            claimed_commands = {
                command_id
                for item in result.criterion_evidence
                for command_id in item.verification_command_ids
            }
            if not claimed_commands.issubset(command_ids):
                raise ProtocolError("implementation evidence names an unknown verification command")
            return
        if isinstance(result, ReviewerResult):
            if phase is not CollaborationPhase.REVIEW:
                raise ProtocolError("review result appeared outside the review phase")
            if set(result.acceptance_coverage) != criterion_ids:
                raise ProtocolError("review does not cover every acceptance criterion")
            if any(finding.criterion_id not in criterion_ids for finding in result.findings):
                raise ProtocolError("review finding names an unknown acceptance criterion")
            if any(
                not _is_allowed_model_path(finding.path, self._config.repository.allowed_paths)
                for finding in result.findings
            ):
                raise ProtocolError("review finding names a path outside the configured scope")
            return
        raise ProtocolError("unknown model result type")

    def _validate_collaboration_invocation(
        self,
        *,
        run_id: str,
        role: Role,
        phase: CollaborationPhase,
        revision_request: ReviewerResult | None,
        candidate_commit: str,
        prior_count: int,
        called_tools: tuple[str, ...],
    ) -> ModelResult:
        required_tools = {
            CollaborationPhase.PROPOSE_PLAN: ("read_new_messages", "publish_plan"),
            CollaborationPhase.CHALLENGE_PLAN: (
                "read_new_messages",
                "acknowledge_handoff",
                "publish_plan_challenge",
            ),
            CollaborationPhase.FINALIZE_PLAN: (
                "read_new_messages",
                "acknowledge_handoff",
                "publish_plan",
            ),
            CollaborationPhase.IMPLEMENT: (
                "read_new_messages",
                "acknowledge_handoff",
                "publish_candidate",
            ),
            CollaborationPhase.REVISE: (
                "read_new_messages",
                "acknowledge_handoff",
                "resolve_review_findings",
                "publish_candidate",
            ),
            CollaborationPhase.REVIEW: (
                "read_new_messages",
                "acknowledge_handoff",
                "publish_review",
            ),
        }.get(phase)
        if required_tools is None:
            raise ProtocolError("workflow phase is not implemented by the current orchestrator")
        if any(called_tools.count(tool) != 1 for tool in required_tools):
            raise ProtocolError(
                "provider did not call each required collaboration tool exactly once",
                context={"phase": phase, "called_tools": called_tools},
            )
        messages = self._store.list_collaboration_messages(run_id)[prior_count:]
        expected_kinds = {
            CollaborationPhase.PROPOSE_PLAN: (CollaborationKind.PLAN_PROPOSED,),
            CollaborationPhase.CHALLENGE_PLAN: (
                CollaborationKind.HANDOFF_ACKNOWLEDGED,
                CollaborationKind.PLAN_CHALLENGED,
            ),
            CollaborationPhase.FINALIZE_PLAN: (
                CollaborationKind.HANDOFF_ACKNOWLEDGED,
                CollaborationKind.PLAN_FINALIZED,
            ),
            CollaborationPhase.IMPLEMENT: (
                CollaborationKind.HANDOFF_ACKNOWLEDGED,
                CollaborationKind.IMPLEMENTATION_SUBMITTED,
            ),
            CollaborationPhase.REVISE: (
                CollaborationKind.HANDOFF_ACKNOWLEDGED,
                CollaborationKind.FINDINGS_RESOLVED,
                CollaborationKind.IMPLEMENTATION_SUBMITTED,
            ),
            CollaborationPhase.REVIEW: (
                CollaborationKind.HANDOFF_ACKNOWLEDGED,
                CollaborationKind.REVIEW_SUBMITTED,
            ),
        }[phase]
        if tuple(message.envelope.kind for message in messages) != expected_kinds:
            raise ProtocolError("provider collaboration messages are missing or out of order")
        if any(
            message.envelope.sender is not role
            or message.transport_status is not TransportStatus.PUBLISHED
            for message in messages
        ):
            raise ProtocolError(
                "provider collaboration messages have invalid authority or transport"
            )
        submission = messages[-1]
        if phase is CollaborationPhase.PROPOSE_PLAN:
            return cast(ModelResult, submission.payload)
        acknowledgement = messages[0]
        if not isinstance(acknowledgement.payload, HandoffAcknowledgement):
            raise ProtocolError("collaboration handoff acknowledgement is missing")
        target = self._store.get_collaboration_message(str(acknowledgement.payload.event_id))
        if (
            target.envelope.event_id != submission.envelope.reply_to
            and phase is not CollaborationPhase.REVISE
        ):
            raise ProtocolError("submission does not reply to the acknowledged handoff")
        expected_target_kind = {
            CollaborationPhase.CHALLENGE_PLAN: {CollaborationKind.PLAN_PROPOSED},
            CollaborationPhase.FINALIZE_PLAN: {CollaborationKind.PLAN_CHALLENGED},
            CollaborationPhase.IMPLEMENT: {
                CollaborationKind.PLAN_PROPOSED,
                CollaborationKind.PLAN_FINALIZED,
            },
            CollaborationPhase.REVISE: {CollaborationKind.REVIEW_SUBMITTED},
            CollaborationPhase.REVIEW: {CollaborationKind.CANDIDATE_READY},
        }[phase]
        if target.envelope.kind not in expected_target_kind:
            raise ProtocolError("provider acknowledged the wrong kind of handoff")
        if phase is CollaborationPhase.REVIEW and (
            not isinstance(target.payload, CandidateHandoff)
            or target.payload.candidate_commit != candidate_commit
        ):
            raise ProtocolError("reviewer did not acknowledge the exact candidate commit")
        if phase is CollaborationPhase.REVISE:
            self._validate_revision_messages(messages, target, revision_request)
        return cast(ModelResult, submission.payload)

    @staticmethod
    def _validate_revision_messages(
        messages: tuple[StoredCollaborationMessage, ...],
        target: StoredCollaborationMessage,
        revision_request: ReviewerResult | None,
    ) -> None:
        if revision_request is None:
            raise ProtocolError("revision collaboration is missing its review request")
        resolution = messages[1]
        submission = messages[2]
        if (
            not isinstance(resolution.payload, FindingsResolutionResult)
            or resolution.envelope.reply_to != target.envelope.event_id
            or submission.envelope.reply_to != resolution.envelope.event_id
        ):
            raise ProtocolError("revision collaboration reply chain is invalid")
        expected = {finding.id for finding in revision_request.findings}
        resolved = {item.finding_id for item in resolution.payload.resolutions}
        if resolved != expected:
            raise ProtocolError("revision did not resolve every review finding exactly once")

    async def _materialize_candidate(
        self,
        run_id: str,
        worktree: ManagedWorktree,
        result: ImplementerResult,
    ) -> tuple[CandidateValidation, ManagedWorktree]:
        refreshed = await self._worktrees.refresh(worktree)
        if result.outcome == "changes_ready":
            if refreshed.head_commit == worktree.head_commit:
                validated = await self._worktrees.commit_pending_changes(
                    worktree,
                    declared_paths=result.declared_changed_paths,
                    allowed_paths=self._config.repository.allowed_paths,
                )
                refreshed = await self._worktrees.refresh(worktree)
            else:
                validated = await self._worktrees.validate_candidate(
                    refreshed,
                    refreshed.head_commit,
                    allowed_paths=self._config.repository.allowed_paths,
                )
        elif result.outcome == "candidate_committed":
            candidate = cast(str, result.candidate_commit)
            validated = await self._worktrees.validate_candidate(
                refreshed,
                candidate,
                allowed_paths=self._config.repository.allowed_paths,
            )
            declared_paths = tuple(
                canonical_repository_path(path) for path in result.declared_changed_paths
            )
            if len(declared_paths) != len(set(declared_paths)) or set(declared_paths) != set(
                validated.changed_paths
            ):
                raise ProtocolError("implementer declared paths do not match the candidate diff")
        else:
            raise StateError("blocked implementer outcome cannot produce a candidate")
        refreshed = await self._worktrees.refresh(worktree)
        if refreshed.head_commit != validated.commit:
            raise StateError("validated candidate differs from the implementer worktree HEAD")
        clean = await self._worktrees.is_clean(refreshed)
        if not clean:
            raise StateError("candidate worktree is dirty after supervisor materialization")
        self._store.observe_worktree(
            run_id=run_id,
            role=Role.IMPLEMENTER,
            head_commit=validated.commit,
            clean=True,
            changed_paths=validated.changed_paths,
        )
        return validated, refreshed

    async def _publish_candidate_handoff(
        self,
        *,
        run_id: str,
        candidate: CandidateValidation,
        plan: PlannerResult,
        implementation: ImplementerResult,
    ) -> None:
        if self._collaboration is None:
            return
        existing = tuple(
            message
            for message in self._store.list_collaboration_messages(run_id)
            if message.envelope.kind is CollaborationKind.CANDIDATE_READY
            and isinstance(message.payload, CandidateHandoff)
            and message.payload.candidate_commit == candidate.commit
        )
        if len(existing) > 1:
            raise StateError("candidate has multiple durable collaboration handoffs")
        if existing:
            if existing[0].transport_status is not TransportStatus.PUBLISHED:
                raise StateError("candidate collaboration handoff has unresolved transport")
            return
        submissions = tuple(
            message
            for message in self._store.list_collaboration_messages(run_id)
            if message.envelope.kind is CollaborationKind.IMPLEMENTATION_SUBMITTED
            and message.payload == implementation
            and message.transport_status is TransportStatus.PUBLISHED
        )
        if not submissions:
            raise ProtocolError("verified candidate does not have an implementation submission")
        payload = CandidateHandoff(
            candidate_commit=candidate.commit,
            diff_sha256=candidate.diff_sha256,
            plan_sha256=collaboration_payload_sha256(plan),
            changed_paths=candidate.changed_paths,
            criterion_evidence=implementation.criterion_evidence,
        )
        await self._collaboration.publish(
            role=Role.IMPLEMENTER,
            kind=CollaborationKind.CANDIDATE_READY,
            payload=payload,
            reply_to=submissions[-1].envelope.event_id,
        )

    def _record_checks(
        self,
        run_id: str,
        candidate: str,
        verification: VerificationSuiteResult,
    ) -> None:
        expected = tuple((job.id, job.required) for job in self._verification_jobs)
        actual = tuple((check.id, check.required) for check in verification.checks)
        if actual != expected:
            raise ProtocolError("verifier results do not match the configured command set")
        self._store.record_check_set(
            run_id=run_id,
            candidate_commit=candidate,
            checks=tuple(
                CheckEvidence(
                    command_id=check.id,
                    ordinal=ordinal,
                    required=check.required,
                    passed=check.passed,
                    termination_reason=check.termination_reason,
                    returncode=check.returncode,
                    started_at=check.started_at,
                    ended_at=check.ended_at,
                    duration_seconds=check.duration_seconds,
                    stdout_sha256=check.stdout_sha256,
                    stderr_sha256=check.stderr_sha256,
                )
                for ordinal, check in enumerate(verification.checks, start=1)
            ),
        )

    async def _create_worktree(
        self,
        collected: list[ManagedWorktree],
        run_id: str,
        role: Role,
        commit: str,
    ) -> ManagedWorktree:
        worktree = await self._worktrees.create(
            run_id=run_id,
            task_id=self._config.task.id,
            role=role,
            commit=commit,
        )
        self._store.record_worktree(
            run_id=run_id,
            role=role,
            path=worktree.path,
            branch=worktree.branch,
            writable=worktree.writable,
            initial_commit=worktree.head_commit,
        )
        self._store.observe_worktree(
            run_id=run_id,
            role=role,
            head_commit=worktree.head_commit,
            clean=True,
        )
        collected.append(worktree)
        return worktree

    async def _emit(
        self,
        *,
        run_id: str,
        kind: EventKind,
        sender: Role,
        attempt: int,
        summary: str,
        artifact: ArtifactReference | None = None,
    ) -> PublicationReceipt:
        event = self._new_event(
            run_id=run_id,
            kind=kind,
            sender=sender,
            attempt=attempt,
            summary=summary,
            artifact=artifact,
        )
        self._store.accept_event(event)
        try:
            receipt = await self._publisher.publish(event)
        except WriteOutcomeUnknownError:
            self._store.mark_event_transport(str(event.event_id), status="uncertain")
            raise
        except WorkflowError:
            self._store.mark_event_transport(str(event.event_id), status="failed")
            raise
        self._store.mark_event_transport(
            str(event.event_id), status="published", technocore_seq=receipt.sequence
        )
        return receipt

    async def _terminalize_run(
        self,
        run_id: str,
        *,
        kind: EventKind,
        summary: str,
    ) -> None:
        """Best-effort terminalization that never replaces the originating failure."""

        try:
            run = self._store.get_run(run_id)
            if run.state.is_terminal:
                return
            event = self._new_event(
                run_id=run_id,
                kind=kind,
                sender=Role.SUPERVISOR,
                attempt=1,
                summary=summary,
            )
            self._store.accept_event(event)
        except WorkflowError:
            return
        prior_unpublished = any(
            record.event.event_id != event.event_id
            and record.transport_status is not TransportStatus.PUBLISHED
            for record in self._store.list_event_records(run_id)
        )
        if prior_unpublished:
            return
        try:
            receipt = await self._publisher.publish(event)
        except WriteOutcomeUnknownError:
            with suppress(WorkflowError):
                self._store.mark_event_transport(str(event.event_id), status="uncertain")
        except WorkflowError:
            with suppress(WorkflowError):
                self._store.mark_event_transport(str(event.event_id), status="failed")
        else:
            with suppress(WorkflowError):
                self._store.mark_event_transport(
                    str(event.event_id),
                    status="published",
                    technocore_seq=receipt.sequence,
                )

    async def reconcile_transports(self, run_id: str) -> None:
        """Publish locally accepted events in order after resolving uncertain writes."""

        for record in self._store.list_event_records(run_id):
            if record.transport_status is TransportStatus.PUBLISHED:
                continue
            receipt: PublicationReceipt | None = None
            if record.transport_status is TransportStatus.UNCERTAIN:
                try:
                    receipt = await self._publisher.reconcile(record.event)
                except WriteOutcomeUnknownError:
                    self._store.mark_event_transport(str(record.event.event_id), status="uncertain")
                    raise
            if receipt is None:
                try:
                    receipt = await self._publisher.publish(record.event)
                except WriteOutcomeUnknownError:
                    self._store.mark_event_transport(str(record.event.event_id), status="uncertain")
                    raise
                except WorkflowError:
                    self._store.mark_event_transport(str(record.event.event_id), status="failed")
                    raise
            self._store.mark_event_transport(
                str(record.event.event_id),
                status="published",
                technocore_seq=receipt.sequence,
            )

    def _new_event(
        self,
        *,
        run_id: str,
        kind: EventKind,
        sender: Role,
        attempt: int,
        summary: str,
        artifact: ArtifactReference | None = None,
    ) -> EventEnvelope:
        return EventEnvelope(
            v=1,
            event_id=uuid4(),
            run_id=run_id,
            task_id=self._config.task.id,
            kind=kind,
            sender=sender,
            attempt=attempt,
            created_at=datetime.now(UTC),
            summary=summary,
            artifact=artifact,
        )


def _event_summary(value: str) -> str:
    normalized = " ".join(value.split())
    if not normalized:
        raise ProtocolError("role summary became empty after normalization")
    return normalized[:600]


def _is_allowed_model_path(value: str, allowed_paths: tuple[str, ...]) -> bool:
    try:
        normalized = canonical_repository_path(value, allow_root=True)
    except ValueError:
        return False
    return any(
        allowed == "." or normalized == allowed or normalized.startswith(f"{allowed}/")
        for allowed in allowed_paths
    )


def _process_failure_facts(
    error: WorkflowError,
) -> tuple[TerminationReason | None, int | None]:
    raw_termination = error.context.get("termination")
    try:
        termination = TerminationReason(raw_termination) if raw_termination is not None else None
    except ValueError:
        termination = None
    raw_returncode = error.context.get("returncode")
    returncode = (
        raw_returncode
        if isinstance(raw_returncode, int) and not isinstance(raw_returncode, bool)
        else None
    )
    if returncode is not None and termination is None:
        termination = TerminationReason.EXITED
    return termination, returncode


def _model_result_matches(role: Role, phase: CollaborationPhase, result: ModelResult) -> bool:
    if phase is CollaborationPhase.CHALLENGE_PLAN:
        return role is Role.IMPLEMENTER and isinstance(result, PlanChallengeResult)
    expected = {
        Role.PLANNER: PlannerResult,
        Role.IMPLEMENTER: ImplementerResult,
        Role.REVIEWER: ReviewerResult,
    }.get(role)
    return expected is not None and isinstance(result, expected)


def _collaboration_phase(role: Role, revision_request: ReviewerResult | None) -> CollaborationPhase:
    if role is Role.PLANNER:
        return CollaborationPhase.PROPOSE_PLAN
    if role is Role.IMPLEMENTER:
        return (
            CollaborationPhase.REVISE
            if revision_request is not None
            else CollaborationPhase.IMPLEMENT
        )
    if role is Role.REVIEWER:
        return CollaborationPhase.REVIEW
    raise ProtocolError("model-driven role does not have a collaboration phase")


def _require_planner_result(stored: StoredRoleResult) -> PlannerResult:
    if not isinstance(stored.result, PlannerResult):
        raise StateError("stored role result is not a planner result")
    return stored.result


def _require_plan_challenge(stored: StoredRoleResult) -> PlanChallengeResult:
    if not isinstance(stored.result, PlanChallengeResult):
        raise StateError("stored role result is not a plan challenge")
    return stored.result


def _require_implementer_result(stored: StoredRoleResult) -> ImplementerResult:
    if not isinstance(stored.result, ImplementerResult):
        raise StateError("stored role result is not an implementer result")
    return stored.result


def _require_reviewer_result(stored: StoredRoleResult) -> ReviewerResult:
    if not isinstance(stored.result, ReviewerResult):
        raise StateError("stored role result is not a reviewer result")
    return stored.result


def _candidate_from_event(event: EventEnvelope) -> str:
    if (
        event.kind is not EventKind.IMPLEMENTATION_READY
        or event.artifact is None
        or event.artifact.type is not ArtifactType.GIT_COMMIT
    ):
        raise StateError("implementation checkpoint omitted its Git candidate")
    return event.artifact.value
