"""SQLite fact store with atomic, idempotent event acceptance."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from importlib import resources
from pathlib import Path

from technocore_orchestrator.domain.collaboration import (
    CandidateHandoff,
    CollaborationEnvelope,
    CollaborationKind,
    CollaborationPayload,
    FindingsResolutionResult,
    HandoffAcknowledgement,
    ModelResult,
    PlanChallengeResult,
    canonical_collaboration_envelope,
    canonical_collaboration_payload,
    collaboration_payload_sha256,
    parse_collaboration_payload,
)
from technocore_orchestrator.domain.models import (
    FULL_SHA_RE,
    RUN_ID_RE,
    SHA256_RE,
    EventEnvelope,
    HarnessKind,
    ImplementerResult,
    PlannerResult,
    ReviewerResult,
    Role,
    RunState,
    TerminationReason,
    canonical_event_json,
    canonical_repository_path,
    event_sha256,
)
from technocore_orchestrator.domain.state_machine import apply_transition
from technocore_orchestrator.domain.usage import ProviderUsage
from technocore_orchestrator.errors import ErrorCategory, StateError, StorageError
from technocore_orchestrator.identity import DID_RE
from technocore_orchestrator.windows_lock import acquire_windows_file_lock

MIGRATIONS = (
    "0001_initial.sql",
    "0002_identity_nonces.sql",
    "0003_worktrees.sql",
    "0004_role_results.sql",
    "0005_execution_evidence.sql",
    "0006_room_scoped_sequences.sql",
    "0007_participant_evidence.sql",
    "0008_provider_usage.sql",
    "0009_collaboration_messages.sql",
    "0010_plan_challenge_states.sql",
    "0011_candidate_handoffs.sql",
    "0012_candidate_changed_paths.sql",
)
LATEST_SCHEMA_VERSION = len(MIGRATIONS)
_IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
_MODEL_ROLES = frozenset({Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER})
_CHECK_FACT_COLUMNS = (
    "command_id",
    "ordinal",
    "required",
    "passed",
    "termination_reason",
    "returncode",
    "started_at",
    "ended_at",
    "duration_seconds",
    "stdout_sha256",
    "stderr_sha256",
)
_PARTICIPANT_FACT_COLUMNS = (
    "role",
    "did",
    "harness",
    "model",
    "cli_name",
    "cli_version",
    "executable_path",
    "executable_sha256",
    "executable_size_bytes",
    "structured_output",
    "resumable",
)


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    task_id: str
    config_digest: str
    repository_path: Path
    base_commit: str
    state: RunState
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class RunCounters:
    invocation_attempts: int
    revision_cycles: int


class TransportStatus(StrEnum):
    LOCAL_PENDING = "local_pending"
    PUBLISHED = "published"
    UNCERTAIN = "uncertain"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StoredEvent:
    event: EventEnvelope
    content_sha256: str
    accepted_at: datetime
    transport_status: TransportStatus
    technocore_seq: int | None


@dataclass(frozen=True, slots=True)
class StoredCollaborationMessage:
    envelope: CollaborationEnvelope
    payload: CollaborationPayload
    transport_status: TransportStatus
    technocore_seq: int | None


class WorktreeStatus(StrEnum):
    ACTIVE = "active"
    RETAINED = "retained"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class WorktreeRecord:
    worktree_id: int
    run_id: str
    role: Role
    path: Path
    branch: str | None
    writable: bool
    initial_commit: str
    status: WorktreeStatus
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class WorktreeObservation:
    observation_id: int
    worktree_id: int
    head_commit: str
    clean: bool
    changed_paths: tuple[str, ...]
    observed_at: datetime


@dataclass(frozen=True, slots=True)
class StoredRoleResult:
    run_id: str
    role: Role
    attempt: int
    result: ModelResult
    content_sha256: str
    created_at: datetime


class InvocationStatus(StrEnum):
    STARTED = "started"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELED = "canceled"


@dataclass(frozen=True, slots=True)
class InvocationRecord:
    invocation_id: int
    run_id: str
    role: Role
    attempt: int
    harness: HarnessKind
    status: InvocationStatus
    timeout_seconds: float
    output_limit_bytes: int
    started_at: datetime
    ended_at: datetime | None
    termination_reason: TerminationReason | None
    returncode: int | None
    duration_seconds: float | None
    stdout_sha256: str | None
    stderr_sha256: str | None
    result_sha256: str | None
    error_category: ErrorCategory | None
    usage: ProviderUsage | None


@dataclass(frozen=True, slots=True)
class CheckRecord:
    check_id: int
    run_id: str
    candidate_commit: str
    command_id: str
    ordinal: int
    required: bool
    passed: bool
    termination_reason: TerminationReason
    returncode: int
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True, slots=True)
class CheckEvidence:
    command_id: str
    ordinal: int
    required: bool
    passed: bool
    termination_reason: TerminationReason
    returncode: int
    started_at: datetime
    ended_at: datetime
    duration_seconds: float
    stdout_sha256: str
    stderr_sha256: str


@dataclass(frozen=True, slots=True)
class ParticipantEvidence:
    role: Role
    did: str
    harness: HarnessKind | None = None
    model: str | None = None
    cli_name: str | None = None
    cli_version: str | None = None
    executable_path: Path | None = None
    executable_sha256: str | None = None
    executable_size_bytes: int | None = None
    structured_output: bool | None = None
    resumable: bool | None = None
    recorded_at: datetime | None = None


class SQLiteStore:
    """Single-host durable store; callers serialize writes through this instance."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._closed = False

    @classmethod
    def open(cls, path: Path) -> SQLiteStore:
        connection: sqlite3.Connection | None = None
        migration_lease = None
        try:
            resolved = path.resolve()
            resolved.parent.mkdir(parents=True, exist_ok=True)
            migration_lease = acquire_windows_file_lock(
                resolved.with_name(resolved.name + ".migrate.lock"),
                label="database migration lock",
                blocking=True,
                unavailable_message="database migration lock remained busy for ten seconds",
            )
            connection = sqlite3.connect(
                resolved,
                isolation_level=None,
                timeout=5.0,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            store = cls(connection)
            store._migrate()
            return store
        except StorageError:
            if connection is not None:
                connection.close()
            raise
        except (OSError, sqlite3.Error) as exc:
            if connection is not None:
                connection.close()
            raise StorageError("unable to open run database", context={"reason": str(exc)}) from exc
        finally:
            if migration_lease is not None:
                migration_lease.close()

    def close(self) -> None:
        if not self._closed:
            self._connection.close()
            self._closed = True

    def __enter__(self) -> SQLiteStore:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _migrate(self) -> None:
        version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
        if version > LATEST_SCHEMA_VERSION:
            raise StorageError(
                "database schema is newer than this application",
                context={"database_version": version, "supported_version": LATEST_SCHEMA_VERSION},
            )
        while version < LATEST_SCHEMA_VERSION:
            next_version = version + 1
            migration_name = MIGRATIONS[next_version - 1]
            try:
                script = (
                    resources.files("technocore_orchestrator.storage.migrations")
                    .joinpath(migration_name)
                    .read_text(encoding="utf-8")
                )
                self._connection.executescript(script)
            except (OSError, sqlite3.Error) as exc:
                if self._connection.in_transaction:
                    self._connection.rollback()
                raise StorageError(
                    "database migration failed",
                    context={"migration": migration_name, "reason": str(exc)},
                ) from exc
            version = int(self._connection.execute("PRAGMA user_version").fetchone()[0])
            if version != next_version:
                raise StorageError(
                    "database migration did not set the expected version",
                    context={"migration": migration_name, "database_version": version},
                )
        violations = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise StorageError(
                "database migration left foreign-key violations",
                context={"violations": len(violations)},
            )

    @contextmanager
    def _write_transaction(self) -> Iterator[None]:
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    @contextmanager
    def read_snapshot(self) -> Iterator[None]:
        """Hold one consistent WAL snapshot across a related set of report reads."""

        if self._connection.in_transaction:
            raise StorageError("cannot open a read snapshot inside another transaction")
        try:
            self._connection.execute("BEGIN")
            yield
        except Exception:
            self._connection.rollback()
            raise
        else:
            self._connection.commit()

    def create_run(
        self,
        *,
        run_id: str,
        task_id: str,
        config_digest: str,
        repository_path: Path,
        base_commit: str,
        participants: tuple[ParticipantEvidence, ...] = (),
        created_at: datetime | None = None,
    ) -> RunRecord:
        now = _utc(created_at or datetime.now(UTC))
        participant_facts = _validated_participant_set(participants)
        try:
            with self._write_transaction():
                self._connection.execute(
                    """
                    INSERT INTO runs (
                        run_id, task_id, config_digest, repository_path, base_commit,
                        state, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        task_id,
                        config_digest,
                        str(repository_path.resolve()),
                        base_commit,
                        RunState.CREATED,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                for facts in participant_facts:
                    self._insert_participant(run_id, facts, now)
        except sqlite3.IntegrityError as exc:
            raise StorageError(
                "run record violates a storage invariant", context={"reason": str(exc)}
            ) from exc
        return self.get_run(run_id)

    def record_participant_set(
        self,
        run_id: str,
        participants: tuple[ParticipantEvidence, ...],
        *,
        recorded_at: datetime | None = None,
    ) -> tuple[ParticipantEvidence, ...]:
        """Persist an exact idempotent provider/identity preflight snapshot."""

        facts_set = _validated_participant_set(participants)
        if not facts_set:
            return ()
        now = _utc(recorded_at or datetime.now(UTC))
        try:
            with self._write_transaction():
                self._get_run_in_transaction(run_id)
                for facts in facts_set:
                    existing = self._connection.execute(
                        "SELECT * FROM run_participants WHERE run_id = ? AND role = ?",
                        (run_id, facts[0]),
                    ).fetchone()
                    if existing is None:
                        self._insert_participant(run_id, facts, now)
                        continue
                    actual = tuple(existing[column] for column in _PARTICIPANT_FACT_COLUMNS)
                    if actual != facts:
                        raise StorageError(
                            "participant role was reused with conflicting preflight evidence",
                            context={"run_id": run_id, "role": facts[0]},
                        )
            return self.list_participants(run_id)
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to record participant evidence", context={"reason": str(exc)}
            ) from exc

    def list_participants(self, run_id: str) -> tuple[ParticipantEvidence, ...]:
        try:
            rows = self._connection.execute(
                "SELECT * FROM run_participants WHERE run_id = ? ORDER BY participant_id",
                (run_id,),
            ).fetchall()
            return tuple(_participant_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError) as exc:
            raise StorageError(
                "unable to list participant evidence", context={"reason": str(exc)}
            ) from exc

    def _insert_participant(
        self, run_id: str, facts: tuple[object, ...], recorded_at: datetime
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO run_participants (
                run_id, role, did, harness, model, cli_name, cli_version,
                executable_path, executable_sha256, executable_size_bytes,
                structured_output, resumable, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (run_id, *facts, recorded_at.isoformat()),
        )

    def get_run(self, run_id: str) -> RunRecord:
        try:
            row = self._connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("unable to read run record", context={"reason": str(exc)}) from exc
        if row is None:
            raise StorageError("run does not exist", context={"run_id": run_id})
        return _run_from_row(row)

    def get_run_counters(self, run_id: str) -> RunCounters:
        """Derive bounded counters from immutable attempt and event facts."""

        try:
            row = self._connection.execute(
                """
                SELECT
                    (SELECT COUNT(*) FROM invocations WHERE run_id = ?) AS invocations,
                    (SELECT COUNT(*) FROM events
                        WHERE run_id = ? AND kind = 'revision_required') AS revisions
                WHERE EXISTS (SELECT 1 FROM runs WHERE run_id = ?)
                """,
                (run_id, run_id, run_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("unable to read run counters", context={"reason": str(exc)}) from exc
        if row is None:
            raise StorageError("run does not exist", context={"run_id": run_id})
        return RunCounters(
            invocation_attempts=row["invocations"],
            revision_cycles=row["revisions"],
        )

    def accept_event(self, event: EventEnvelope) -> None:
        envelope_json = canonical_event_json(event)
        digest = event_sha256(event)
        accepted_at = datetime.now(UTC).isoformat()
        try:
            with self._write_transaction():
                duplicate = self._connection.execute(
                    "SELECT content_sha256, envelope_json FROM events WHERE event_id = ?",
                    (str(event.event_id),),
                ).fetchone()
                if duplicate is not None:
                    if (
                        duplicate["content_sha256"] != digest
                        or duplicate["envelope_json"] != envelope_json
                    ):
                        raise StateError(
                            "event id was reused with different content",
                            context={"event_id": str(event.event_id)},
                        )
                    transition_row = self._connection.execute(
                        "SELECT 1 FROM transitions WHERE event_id = ?",
                        (str(event.event_id),),
                    ).fetchone()
                    if transition_row is None:
                        raise StorageError(
                            "accepted event is missing its transition",
                            context={"event_id": str(event.event_id)},
                        )
                    return

                run = self._get_run_in_transaction(event.run_id)
                if run.task_id != event.task_id:
                    raise StateError(
                        "event task does not match its run",
                        context={"run_task": run.task_id, "event_task": event.task_id},
                    )
                transition = apply_transition(run.state, event)
                self._connection.execute(
                    """
                    INSERT INTO events (
                        event_id, run_id, kind, sender, attempt, envelope_json,
                        content_sha256, accepted_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(event.event_id),
                        event.run_id,
                        event.kind,
                        event.sender,
                        event.attempt,
                        envelope_json,
                        digest,
                        accepted_at,
                    ),
                )
                self._connection.execute(
                    """
                    INSERT INTO transitions (
                        run_id, event_id, previous_state, current_state, created_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        event.run_id,
                        str(event.event_id),
                        transition.previous,
                        transition.current,
                        accepted_at,
                    ),
                )
                self._connection.execute(
                    "UPDATE runs SET state = ?, updated_at = ? WHERE run_id = ?",
                    (transition.current, accepted_at, event.run_id),
                )
                return
        except (StateError, StorageError):
            raise
        except sqlite3.Error as exc:
            raise StorageError("event acceptance failed", context={"reason": str(exc)}) from exc

    def list_events(self, run_id: str) -> tuple[EventEnvelope, ...]:
        try:
            rows = self._connection.execute(
                "SELECT envelope_json FROM events WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
            return tuple(EventEnvelope.model_validate_json(row["envelope_json"]) for row in rows)
        except (sqlite3.Error, ValueError) as exc:
            raise StorageError(
                "unable to read stored events", context={"reason": str(exc)}
            ) from exc

    def list_event_records(self, run_id: str) -> tuple[StoredEvent, ...]:
        try:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY rowid", (run_id,)
            ).fetchall()
            return tuple(_stored_event_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError) as exc:
            raise StorageError(
                "unable to read event transport records", context={"reason": str(exc)}
            ) from exc

    def published_sequence_before(self, event_id: str) -> int:
        try:
            row = self._connection.execute(
                """
                SELECT COUNT(DISTINCT current.event_id) AS found,
                    COALESCE(MAX(prior.technocore_seq), 0) AS last_seq
                FROM events AS current
                LEFT JOIN events AS prior
                    ON prior.run_id = current.run_id
                    AND prior.rowid < current.rowid
                    AND prior.transport_status = 'published'
                WHERE current.event_id = ?
                """,
                (event_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to read prior published cursor", context={"reason": str(exc)}
            ) from exc
        if row is None or row["found"] != 1:
            raise StorageError("event does not exist")
        return int(row["last_seq"])

    def allocate_nonce(self, did: str, room: str, *, now_milliseconds: int | None = None) -> int:
        """Atomically allocate a monotonic signed-write nonce without storing the room name."""

        if not did or not room:
            raise StorageError("DID and room are required to allocate a nonce")
        current = now_milliseconds if now_milliseconds is not None else time.time_ns() // 1_000_000
        if isinstance(current, bool) or not isinstance(current, int) or current < 0:
            raise StorageError("nonce clock must be a non-negative integer")
        room_digest = hashlib.sha256(room.encode()).hexdigest()
        updated_at = datetime.now(UTC).isoformat()
        try:
            with self._write_transaction():
                row = self._connection.execute(
                    "SELECT last_nonce FROM identity_nonces WHERE did = ? AND room_sha256 = ?",
                    (did, room_digest),
                ).fetchone()
                previous = int(row["last_nonce"]) if row is not None else -1
                nonce = max(current, previous + 1)
                if nonce > (1 << 63) - 1:
                    raise StorageError("nonce space is exhausted for this identity and room")
                self._connection.execute(
                    """
                    INSERT INTO identity_nonces (did, room_sha256, last_nonce, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT (did, room_sha256) DO UPDATE SET
                        last_nonce = excluded.last_nonce,
                        updated_at = excluded.updated_at
                    """,
                    (did, room_digest, nonce, updated_at),
                )
                return nonce
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError("nonce allocation failed", context={"reason": str(exc)}) from exc

    def record_worktree(
        self,
        *,
        run_id: str,
        role: Role,
        path: Path,
        branch: str | None,
        writable: bool,
        initial_commit: str,
        created_at: datetime | None = None,
    ) -> WorktreeRecord:
        """Persist one role worktree idempotently without overwriting conflicting facts."""

        if role not in {Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER, Role.VERIFIER}:
            raise StorageError("role does not own a persisted worktree")
        if not path.is_absolute():
            raise StorageError("persisted worktree path must be absolute")
        if not FULL_SHA_RE.fullmatch(initial_commit):
            raise StorageError("persisted worktree commit must be a lowercase full Git SHA")
        if not isinstance(writable, bool):
            raise StorageError("persisted worktree writable flag must be boolean")
        now = _utc(created_at or datetime.now(UTC))
        normalized_path = str(path.resolve(strict=False))
        expected = (
            role.value,
            normalized_path,
            branch,
            int(writable),
            initial_commit,
        )
        try:
            with self._write_transaction():
                existing = self._connection.execute(
                    "SELECT * FROM worktrees WHERE run_id = ? AND role = ?",
                    (run_id, role),
                ).fetchone()
                if existing is not None:
                    actual = (
                        existing["role"],
                        existing["path"],
                        existing["branch"],
                        existing["writable"],
                        existing["initial_commit"],
                    )
                    if actual != expected:
                        raise StorageError(
                            "worktree role was reused with conflicting facts",
                            context={"run_id": run_id, "role": role},
                        )
                    return _worktree_from_row(existing)
                self._connection.execute(
                    """
                    INSERT INTO worktrees (
                        run_id, role, path, branch, writable, initial_commit,
                        status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        role,
                        normalized_path,
                        branch,
                        int(writable),
                        initial_commit,
                        WorktreeStatus.ACTIVE,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            return self.get_worktree(run_id, role)
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError("unable to record worktree", context={"reason": str(exc)}) from exc

    def get_worktree(self, run_id: str, role: Role) -> WorktreeRecord:
        try:
            row = self._connection.execute(
                "SELECT * FROM worktrees WHERE run_id = ? AND role = ?", (run_id, role)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("unable to read worktree", context={"reason": str(exc)}) from exc
        if row is None:
            raise StorageError("worktree does not exist", context={"run_id": run_id, "role": role})
        return _worktree_from_row(row)

    def list_worktrees(self, run_id: str) -> tuple[WorktreeRecord, ...]:
        try:
            rows = self._connection.execute(
                "SELECT * FROM worktrees WHERE run_id = ? ORDER BY worktree_id", (run_id,)
            ).fetchall()
            return tuple(_worktree_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError) as exc:
            raise StorageError("unable to list worktrees", context={"reason": str(exc)}) from exc

    def relocate_run_paths(
        self,
        *,
        run_id: str,
        previous_storage_root: Path,
        current_storage_root: Path,
        previous_repository_path: Path,
        current_repository_path: Path,
    ) -> None:
        """Atomically rebase one structurally verified generated run after a directory move."""

        if not RUN_ID_RE.fullmatch(run_id):
            raise StorageError("relocated run id is invalid")
        previous_storage = previous_storage_root.resolve()
        current_storage = current_storage_root.resolve()
        previous_repository = previous_repository_path.resolve()
        current_repository = current_repository_path.resolve()
        generated_suffix = Path("generated-projects") / run_id / "source"
        if (
            previous_storage == current_storage
            or previous_repository != (previous_storage / generated_suffix).resolve()
            or current_repository != (current_storage / generated_suffix).resolve()
        ):
            raise StorageError("run path relocation does not match the generated-project layout")

        worktree_prefix = Path("worktrees") / run_id
        try:
            with self._write_transaction():
                run = self._connection.execute(
                    "SELECT repository_path FROM runs WHERE run_id = ?", (run_id,)
                ).fetchone()
                if run is None:
                    raise StorageError("cannot relocate an unknown run")
                durable_repository = Path(run["repository_path"]).resolve()
                if durable_repository == current_repository:
                    return
                if durable_repository != previous_repository:
                    raise StorageError(
                        "durable run repository does not match the relocation source"
                    )

                worktrees = self._connection.execute(
                    "SELECT worktree_id, path FROM worktrees WHERE run_id = ?", (run_id,)
                ).fetchall()
                relocated_worktrees: list[tuple[str, int]] = []
                for worktree in worktrees:
                    stored_path = Path(worktree["path"]).resolve()
                    try:
                        relative = stored_path.relative_to(previous_storage)
                        relative.relative_to(worktree_prefix)
                    except ValueError as exc:
                        raise StorageError(
                            "durable worktree path cannot be safely relocated"
                        ) from exc
                    relocated_worktrees.append(
                        (str((current_storage / relative).resolve()), worktree["worktree_id"])
                    )

                self._connection.execute(
                    "UPDATE runs SET repository_path = ? WHERE run_id = ?",
                    (str(current_repository), run_id),
                )
                self._connection.executemany(
                    "UPDATE worktrees SET path = ? WHERE worktree_id = ?",
                    relocated_worktrees,
                )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError("unable to relocate durable run paths") from exc

    def get_latest_worktree_observation(self, run_id: str, role: Role) -> WorktreeObservation:
        try:
            row = self._connection.execute(
                """
                SELECT observations.* FROM worktree_observations AS observations
                JOIN worktrees USING (worktree_id)
                WHERE worktrees.run_id = ? AND worktrees.role = ?
                ORDER BY observations.observation_id DESC LIMIT 1
                """,
                (run_id, role),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to read worktree observation", context={"reason": str(exc)}
            ) from exc
        if row is None:
            raise StorageError("worktree observation does not exist")
        return _worktree_observation_from_row(row)

    def observe_worktree(
        self,
        *,
        run_id: str,
        role: Role,
        head_commit: str,
        clean: bool,
        changed_paths: tuple[str, ...] = (),
        observed_at: datetime | None = None,
    ) -> WorktreeObservation:
        if not FULL_SHA_RE.fullmatch(head_commit):
            raise StorageError("observed worktree commit must be a lowercase full Git SHA")
        if not isinstance(clean, bool):
            raise StorageError("observed worktree cleanliness must be boolean")
        try:
            normalized_paths = tuple(canonical_repository_path(path) for path in changed_paths)
        except ValueError as exc:
            raise StorageError("observed worktree changed paths are invalid") from exc
        if len(normalized_paths) != len(set(normalized_paths)):
            raise StorageError("observed worktree changed paths must be unique")
        serialized_paths = json.dumps(normalized_paths, ensure_ascii=False, separators=(",", ":"))
        now = _utc(observed_at or datetime.now(UTC))
        try:
            with self._write_transaction():
                worktree = self._connection.execute(
                    "SELECT worktree_id, status FROM worktrees WHERE run_id = ? AND role = ?",
                    (run_id, role),
                ).fetchone()
                if worktree is None:
                    raise StorageError(
                        "cannot observe an unknown worktree",
                        context={"run_id": run_id, "role": role},
                    )
                if worktree["status"] == WorktreeStatus.REMOVED:
                    raise StorageError("cannot observe a removed worktree")
                cursor = self._connection.execute(
                    """
                    INSERT INTO worktree_observations (
                        worktree_id, head_commit, clean, changed_paths_json, observed_at
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        worktree["worktree_id"],
                        head_commit,
                        int(clean),
                        serialized_paths,
                        now.isoformat(),
                    ),
                )
                observation_id = cursor.lastrowid
                if observation_id is None:
                    raise StorageError("worktree observation did not receive an identifier")
                return WorktreeObservation(
                    observation_id=observation_id,
                    worktree_id=worktree["worktree_id"],
                    head_commit=head_commit,
                    clean=clean,
                    changed_paths=normalized_paths,
                    observed_at=now,
                )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to record worktree observation", context={"reason": str(exc)}
            ) from exc

    def mark_worktree_status(
        self,
        run_id: str,
        role: Role,
        status: WorktreeStatus,
        *,
        updated_at: datetime | None = None,
    ) -> WorktreeRecord:
        if status is WorktreeStatus.ACTIVE:
            raise StorageError("worktree cannot transition back to active")
        now = _utc(updated_at or datetime.now(UTC))
        try:
            with self._write_transaction():
                row = self._connection.execute(
                    "SELECT * FROM worktrees WHERE run_id = ? AND role = ?", (run_id, role)
                ).fetchone()
                if row is None:
                    raise StorageError("cannot update an unknown worktree")
                current = WorktreeStatus(row["status"])
                if current is WorktreeStatus.REMOVED and status is not WorktreeStatus.REMOVED:
                    raise StorageError("removed worktree status is immutable")
                self._connection.execute(
                    "UPDATE worktrees SET status = ?, updated_at = ? WHERE worktree_id = ?",
                    (status, now.isoformat(), row["worktree_id"]),
                )
            return self.get_worktree(run_id, role)
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to update worktree status", context={"reason": str(exc)}
            ) from exc

    def get_role_result(self, run_id: str, role: Role, attempt: int) -> StoredRoleResult:
        try:
            row = self._connection.execute(
                """
                SELECT * FROM role_results
                WHERE run_id = ? AND role = ? AND attempt = ?
                """,
                (run_id, role, attempt),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("unable to read role result", context={"reason": str(exc)}) from exc
        if row is None:
            raise StorageError("role result does not exist")
        return _stored_role_result_from_row(row)

    def list_role_results(
        self, run_id: str, role: Role | None = None
    ) -> tuple[StoredRoleResult, ...]:
        try:
            if role is None:
                rows = self._connection.execute(
                    "SELECT * FROM role_results WHERE run_id = ? ORDER BY rowid", (run_id,)
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    SELECT * FROM role_results WHERE run_id = ? AND role = ?
                    ORDER BY attempt
                    """,
                    (run_id, role),
                ).fetchall()
            return tuple(_stored_role_result_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError) as exc:
            raise StorageError("unable to list role results", context={"reason": str(exc)}) from exc

    def start_invocation(
        self,
        *,
        run_id: str,
        role: Role,
        attempt: int,
        harness: HarnessKind,
        timeout_seconds: float,
        output_limit_bytes: int,
        started_at: datetime | None = None,
    ) -> InvocationRecord:
        """Persist the durable start of one bounded model-harness attempt."""

        _validate_invocation_identity(role, attempt)
        _validate_invocation_limits(timeout_seconds, output_limit_bytes)
        now = _utc(started_at or datetime.now(UTC))
        expected = (
            harness.value,
            float(timeout_seconds),
            output_limit_bytes,
        )
        try:
            with self._write_transaction():
                existing = self._connection.execute(
                    """
                    SELECT * FROM invocations
                    WHERE run_id = ? AND role = ? AND attempt = ?
                    """,
                    (run_id, role, attempt),
                ).fetchone()
                if existing is not None:
                    actual = (
                        existing["harness"],
                        existing["timeout_seconds"],
                        existing["output_limit_bytes"],
                    )
                    if actual != expected:
                        raise StorageError(
                            "invocation attempt was reused with conflicting start facts"
                        )
                    return _invocation_from_row(existing)
                self._connection.execute(
                    """
                    INSERT INTO invocations (
                        run_id, role, attempt, harness, status, timeout_seconds,
                        output_limit_bytes, started_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        run_id,
                        role,
                        attempt,
                        harness,
                        InvocationStatus.STARTED,
                        float(timeout_seconds),
                        output_limit_bytes,
                        now.isoformat(),
                    ),
                )
            return self.get_invocation(run_id, role, attempt)
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to start invocation record", context={"reason": str(exc)}
            ) from exc

    def complete_invocation(
        self,
        *,
        run_id: str,
        role: Role,
        attempt: int,
        termination_reason: TerminationReason,
        returncode: int,
        stdout_sha256: str,
        stderr_sha256: str,
        result: ModelResult,
        duration_seconds: float,
        usage: ProviderUsage | None = None,
        ended_at: datetime | None = None,
    ) -> InvocationRecord:
        """Terminalize a successful attempt with content-addressed process evidence."""

        _validate_invocation_identity(role, attempt)
        if termination_reason is not TerminationReason.EXITED or returncode != 0:
            raise StorageError("successful invocation requires a normal zero exit")
        _validate_role_result_type(role, result)
        serialized = canonical_collaboration_payload(result)
        result_sha256 = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        _validate_digest(stdout_sha256, "invocation stdout")
        _validate_digest(stderr_sha256, "invocation stderr")
        _validate_duration(duration_seconds, "invocation duration")
        usage_facts = _usage_facts(usage)
        now = _utc(ended_at or datetime.now(UTC))
        return self._terminalize_invocation(
            run_id=run_id,
            role=role,
            attempt=attempt,
            status=InvocationStatus.SUCCEEDED,
            ended_at=now,
            duration_seconds=float(duration_seconds),
            termination_reason=termination_reason,
            returncode=returncode,
            stdout_sha256=stdout_sha256,
            stderr_sha256=stderr_sha256,
            result_sha256=result_sha256,
            error_category=None,
            role_result_json=serialized,
            usage_facts=usage_facts,
        )

    def fail_invocation(
        self,
        *,
        run_id: str,
        role: Role,
        attempt: int,
        error_category: ErrorCategory,
        termination_reason: TerminationReason | None = None,
        returncode: int | None = None,
        duration_seconds: float,
        ended_at: datetime | None = None,
    ) -> InvocationRecord:
        """Terminalize a failed attempt without persisting provider diagnostics."""

        _validate_invocation_identity(role, attempt)
        if termination_reason is None and returncode is not None:
            raise StorageError("invocation return code requires a termination reason")
        _validate_duration(duration_seconds, "invocation duration")
        now = _utc(ended_at or datetime.now(UTC))
        return self._terminalize_invocation(
            run_id=run_id,
            role=role,
            attempt=attempt,
            status=InvocationStatus.FAILED,
            ended_at=now,
            duration_seconds=float(duration_seconds),
            termination_reason=termination_reason,
            returncode=returncode,
            stdout_sha256=None,
            stderr_sha256=None,
            result_sha256=None,
            error_category=error_category,
            role_result_json=None,
            usage_facts=(None, None, None, None, None),
        )

    def cancel_invocation(
        self,
        *,
        run_id: str,
        role: Role,
        attempt: int,
        duration_seconds: float,
        ended_at: datetime | None = None,
    ) -> InvocationRecord:
        """Terminalize an attempt canceled by the enclosing run."""

        _validate_invocation_identity(role, attempt)
        _validate_duration(duration_seconds, "invocation duration")
        now = _utc(ended_at or datetime.now(UTC))
        return self._terminalize_invocation(
            run_id=run_id,
            role=role,
            attempt=attempt,
            status=InvocationStatus.CANCELED,
            ended_at=now,
            duration_seconds=float(duration_seconds),
            termination_reason=None,
            returncode=None,
            stdout_sha256=None,
            stderr_sha256=None,
            result_sha256=None,
            error_category=None,
            role_result_json=None,
            usage_facts=(None, None, None, None, None),
        )

    def get_invocation(self, run_id: str, role: Role, attempt: int) -> InvocationRecord:
        try:
            row = self._connection.execute(
                """
                SELECT * FROM invocations
                WHERE run_id = ? AND role = ? AND attempt = ?
                """,
                (run_id, role, attempt),
            ).fetchone()
            if row is None:
                raise StorageError("invocation does not exist")
            return _invocation_from_row(row)
        except StorageError:
            raise
        except (sqlite3.Error, ValueError) as exc:
            raise StorageError("unable to read invocation", context={"reason": str(exc)}) from exc

    def list_invocations(self, run_id: str) -> tuple[InvocationRecord, ...]:
        try:
            rows = self._connection.execute(
                "SELECT * FROM invocations WHERE run_id = ? ORDER BY invocation_id", (run_id,)
            ).fetchall()
            return tuple(_invocation_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError) as exc:
            raise StorageError("unable to list invocations", context={"reason": str(exc)}) from exc

    def record_check_set(
        self,
        *,
        run_id: str,
        candidate_commit: str,
        checks: tuple[CheckEvidence, ...],
    ) -> tuple[CheckRecord, ...]:
        """Atomically persist one complete deterministic verifier suite."""

        if not FULL_SHA_RE.fullmatch(candidate_commit):
            raise StorageError("check candidate must be a lowercase full Git SHA")
        if not checks:
            raise StorageError("check set must not be empty")
        expected_rows = tuple(_validated_check_facts(check) for check in checks)
        command_ids = tuple(row[0] for row in expected_rows)
        ordinals = tuple(row[1] for row in expected_rows)
        if len(set(command_ids)) != len(command_ids) or len(set(ordinals)) != len(ordinals):
            raise StorageError("check set contains a duplicate command id or ordinal")
        try:
            with self._write_transaction():
                for expected in expected_rows:
                    existing = self._connection.execute(
                        """
                        SELECT * FROM checks
                        WHERE run_id = ? AND candidate_commit = ? AND command_id = ?
                        """,
                        (run_id, candidate_commit, expected[0]),
                    ).fetchone()
                    if existing is not None:
                        actual = tuple(existing[key] for key in _CHECK_FACT_COLUMNS)
                        if actual != expected:
                            raise StorageError("check identity was reused with conflicting facts")
                        continue
                    self._connection.execute(
                        """
                        INSERT INTO checks (
                            run_id, candidate_commit, command_id, ordinal, required, passed,
                            termination_reason, returncode, started_at, ended_at,
                            duration_seconds, stdout_sha256, stderr_sha256
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (run_id, candidate_commit, *expected),
                    )
            return tuple(
                self.get_check(run_id, candidate_commit, command_id) for command_id in command_ids
            )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError("unable to record check set", context={"reason": str(exc)}) from exc

    def get_check(self, run_id: str, candidate_commit: str, command_id: str) -> CheckRecord:
        try:
            row = self._connection.execute(
                """
                SELECT * FROM checks
                WHERE run_id = ? AND candidate_commit = ? AND command_id = ?
                """,
                (run_id, candidate_commit, command_id),
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("unable to read check", context={"reason": str(exc)}) from exc
        if row is None:
            raise StorageError("check does not exist")
        return _check_from_row(row)

    def list_checks(self, run_id: str) -> tuple[CheckRecord, ...]:
        try:
            rows = self._connection.execute(
                "SELECT * FROM checks WHERE run_id = ? ORDER BY check_id", (run_id,)
            ).fetchall()
            return tuple(_check_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError) as exc:
            raise StorageError("unable to list checks", context={"reason": str(exc)}) from exc

    def _terminalize_invocation(
        self,
        *,
        run_id: str,
        role: Role,
        attempt: int,
        status: InvocationStatus,
        ended_at: datetime,
        duration_seconds: float,
        termination_reason: TerminationReason | None,
        returncode: int | None,
        stdout_sha256: str | None,
        stderr_sha256: str | None,
        result_sha256: str | None,
        error_category: ErrorCategory | None,
        role_result_json: str | None,
        usage_facts: tuple[int | None, int | None, int | None, int | None, int | None],
    ) -> InvocationRecord:
        try:
            with self._write_transaction():
                existing = self._connection.execute(
                    """
                    SELECT * FROM invocations
                    WHERE run_id = ? AND role = ? AND attempt = ?
                    """,
                    (run_id, role, attempt),
                ).fetchone()
                if existing is None:
                    raise StorageError("cannot terminalize an unknown invocation")
                if InvocationStatus(existing["status"]) is not InvocationStatus.STARTED:
                    raise StorageError("terminal invocation record is immutable")
                if status is InvocationStatus.SUCCEEDED:
                    if role_result_json is None or result_sha256 is None:
                        raise StorageError("successful invocation omitted its role result")
                    stored_result = self._connection.execute(
                        """
                        SELECT result_json, content_sha256 FROM role_results
                        WHERE run_id = ? AND role = ? AND attempt = ?
                        """,
                        (run_id, role, attempt),
                    ).fetchone()
                    if stored_result is not None and (
                        stored_result["result_json"] != role_result_json
                        or stored_result["content_sha256"] != result_sha256
                    ):
                        raise StorageError(
                            "role-result attempt was reused with conflicting content"
                        )
                    if stored_result is None:
                        self._connection.execute(
                            """
                            INSERT INTO role_results (
                                run_id, role, attempt, result_json, content_sha256, created_at
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            (
                                run_id,
                                role,
                                attempt,
                                role_result_json,
                                result_sha256,
                                ended_at.isoformat(),
                            ),
                        )
                self._connection.execute(
                    """
                    UPDATE invocations SET
                        status = ?, ended_at = ?, termination_reason = ?, returncode = ?,
                        duration_seconds = ?, stdout_sha256 = ?, stderr_sha256 = ?,
                        result_sha256 = ?, error_category = ?, input_tokens = ?,
                        output_tokens = ?, cache_read_input_tokens = ?,
                        cache_creation_input_tokens = ?, provider_turns = ?
                    WHERE invocation_id = ?
                    """,
                    (
                        status,
                        ended_at.isoformat(),
                        termination_reason,
                        returncode,
                        duration_seconds,
                        stdout_sha256,
                        stderr_sha256,
                        result_sha256,
                        error_category,
                        *usage_facts,
                        existing["invocation_id"],
                    ),
                )
            return self.get_invocation(run_id, role, attempt)
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to terminalize invocation", context={"reason": str(exc)}
            ) from exc

    def mark_event_transport(
        self,
        event_id: str,
        *,
        status: str,
        technocore_seq: int | None = None,
    ) -> None:
        if status not in {"published", "uncertain", "failed"}:
            raise StorageError("event transport status is invalid")
        if status == "published":
            if (
                isinstance(technocore_seq, bool)
                or not isinstance(technocore_seq, int)
                or technocore_seq < 1
            ):
                raise StorageError("published event requires a positive Technocore sequence")
        elif technocore_seq is not None:
            raise StorageError("unpublished event cannot have a Technocore sequence")
        try:
            with self._write_transaction():
                row = self._connection.execute(
                    "SELECT transport_status, technocore_seq FROM events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
                if row is None:
                    raise StorageError("cannot update transport for an unknown event")
                if row["transport_status"] == "published":
                    if status != "published" or row["technocore_seq"] != technocore_seq:
                        raise StorageError("published event transport record is immutable")
                    return
                self._connection.execute(
                    """
                    UPDATE events SET transport_status = ?, technocore_seq = ?
                    WHERE event_id = ?
                    """,
                    (status, technocore_seq, event_id),
                )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to update event transport", context={"reason": str(exc)}
            ) from exc

    def record_collaboration_message(
        self,
        envelope: CollaborationEnvelope,
        payload: CollaborationPayload,
    ) -> StoredCollaborationMessage:
        _validate_collaboration_message(envelope, payload)
        payload_json = canonical_collaboration_payload(payload)
        envelope_json = canonical_collaboration_envelope(envelope)
        try:
            with self._write_transaction():
                run = self._get_run_in_transaction(envelope.run_id)
                if run.task_id != envelope.task_id:
                    raise StorageError("collaboration task does not match its run")
                if envelope.reply_to is not None:
                    reply_id = str(envelope.reply_to)
                    collaboration_reply = self._connection.execute(
                        "SELECT run_id FROM collaboration_messages WHERE event_id = ?",
                        (reply_id,),
                    ).fetchone()
                    workflow_reply = self._connection.execute(
                        "SELECT run_id FROM events WHERE event_id = ?",
                        (reply_id,),
                    ).fetchone()
                    reply = collaboration_reply or workflow_reply
                    if reply is None or reply["run_id"] != envelope.run_id:
                        raise StorageError(
                            "collaboration reply target is missing or belongs to another run"
                        )
                existing = self._connection.execute(
                    "SELECT * FROM collaboration_messages WHERE event_id = ?",
                    (str(envelope.event_id),),
                ).fetchone()
                if existing is not None:
                    if (
                        existing["envelope_json"] != envelope_json
                        or existing["payload_json"] != payload_json
                    ):
                        raise StorageError(
                            "collaboration event id was reused with conflicting content"
                        )
                    return _stored_collaboration_from_row(existing)
                self._connection.execute(
                    """
                    INSERT INTO collaboration_messages (
                        event_id, run_id, task_id, kind, sender, reply_to, text,
                        payload_json, payload_sha256, envelope_json, created_at,
                        transport_status, technocore_seq
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'local_pending', NULL)
                    """,
                    (
                        str(envelope.event_id),
                        envelope.run_id,
                        envelope.task_id,
                        envelope.kind,
                        envelope.sender,
                        str(envelope.reply_to) if envelope.reply_to is not None else None,
                        envelope.text,
                        payload_json,
                        envelope.payload_sha256,
                        envelope_json,
                        envelope.created_at.isoformat(),
                    ),
                )
            return self.get_collaboration_message(str(envelope.event_id))
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to record collaboration message", context={"reason": str(exc)}
            ) from exc

    def get_collaboration_message(self, event_id: str) -> StoredCollaborationMessage:
        try:
            row = self._connection.execute(
                "SELECT * FROM collaboration_messages WHERE event_id = ?", (event_id,)
            ).fetchone()
        except sqlite3.Error as exc:
            raise StorageError("unable to read collaboration message") from exc
        if row is None:
            raise StorageError("collaboration message does not exist")
        return _stored_collaboration_from_row(row)

    def list_collaboration_messages(self, run_id: str) -> tuple[StoredCollaborationMessage, ...]:
        try:
            rows = self._connection.execute(
                "SELECT * FROM collaboration_messages WHERE run_id = ? ORDER BY rowid",
                (run_id,),
            ).fetchall()
            return tuple(_stored_collaboration_from_row(row) for row in rows)
        except (sqlite3.Error, ValueError) as exc:
            raise StorageError(
                "unable to list collaboration messages", context={"reason": str(exc)}
            ) from exc

    def mark_collaboration_transport(
        self,
        event_id: str,
        *,
        status: str,
        technocore_seq: int | None = None,
    ) -> None:
        if status not in {"published", "uncertain", "failed"}:
            raise StorageError("collaboration transport status is invalid")
        if status == "published":
            if (
                isinstance(technocore_seq, bool)
                or not isinstance(technocore_seq, int)
                or technocore_seq < 1
            ):
                raise StorageError("published collaboration requires a positive sequence")
        elif technocore_seq is not None:
            raise StorageError("unpublished collaboration cannot have a sequence")
        try:
            with self._write_transaction():
                row = self._connection.execute(
                    """
                    SELECT transport_status, technocore_seq
                    FROM collaboration_messages WHERE event_id = ?
                    """,
                    (event_id,),
                ).fetchone()
                if row is None:
                    raise StorageError("cannot update transport for unknown collaboration")
                if row["transport_status"] == "published":
                    if status != "published" or row["technocore_seq"] != technocore_seq:
                        raise StorageError("published collaboration transport is immutable")
                    return
                self._connection.execute(
                    """
                    UPDATE collaboration_messages
                    SET transport_status = ?, technocore_seq = ? WHERE event_id = ?
                    """,
                    (status, technocore_seq, event_id),
                )
        except StorageError:
            raise
        except sqlite3.Error as exc:
            raise StorageError(
                "unable to update collaboration transport", context={"reason": str(exc)}
            ) from exc

    def _get_run_in_transaction(self, run_id: str) -> RunRecord:
        row = self._connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise StorageError("run does not exist", context={"run_id": run_id})
        return _run_from_row(row)


def _run_from_row(row: sqlite3.Row) -> RunRecord:
    return RunRecord(
        run_id=row["run_id"],
        task_id=row["task_id"],
        config_digest=row["config_digest"],
        repository_path=Path(row["repository_path"]),
        base_commit=row["base_commit"],
        state=RunState(row["state"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _stored_event_from_row(row: sqlite3.Row) -> StoredEvent:
    return StoredEvent(
        event=EventEnvelope.model_validate_json(row["envelope_json"]),
        content_sha256=row["content_sha256"],
        accepted_at=datetime.fromisoformat(row["accepted_at"]),
        transport_status=TransportStatus(row["transport_status"]),
        technocore_seq=row["technocore_seq"],
    )


def _stored_collaboration_from_row(row: sqlite3.Row) -> StoredCollaborationMessage:
    envelope = CollaborationEnvelope.model_validate_json(row["envelope_json"])
    payload = parse_collaboration_payload(envelope.kind, row["payload_json"])
    if canonical_collaboration_envelope(envelope) != row["envelope_json"]:
        raise ValueError("stored collaboration envelope is not canonical")
    if canonical_collaboration_payload(payload) != row["payload_json"]:
        raise ValueError("stored collaboration payload is not canonical")
    if collaboration_payload_sha256(payload) != row["payload_sha256"]:
        raise ValueError("stored collaboration payload digest does not match")
    return StoredCollaborationMessage(
        envelope=envelope,
        payload=payload,
        transport_status=TransportStatus(row["transport_status"]),
        technocore_seq=row["technocore_seq"],
    )


def _worktree_from_row(row: sqlite3.Row) -> WorktreeRecord:
    return WorktreeRecord(
        worktree_id=row["worktree_id"],
        run_id=row["run_id"],
        role=Role(row["role"]),
        path=Path(row["path"]),
        branch=row["branch"],
        writable=bool(row["writable"]),
        initial_commit=row["initial_commit"],
        status=WorktreeStatus(row["status"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
    )


def _worktree_observation_from_row(row: sqlite3.Row) -> WorktreeObservation:
    changed_paths_value = json.loads(row["changed_paths_json"])
    if not isinstance(changed_paths_value, list) or any(
        not isinstance(path, str) for path in changed_paths_value
    ):
        raise ValueError("stored worktree changed paths are invalid")
    changed_paths = tuple(canonical_repository_path(path) for path in changed_paths_value)
    if len(changed_paths) != len(set(changed_paths)):
        raise ValueError("stored worktree changed paths are not unique")
    return WorktreeObservation(
        observation_id=row["observation_id"],
        worktree_id=row["worktree_id"],
        head_commit=row["head_commit"],
        clean=bool(row["clean"]),
        changed_paths=changed_paths,
        observed_at=datetime.fromisoformat(row["observed_at"]),
    )


def _stored_role_result_from_row(row: sqlite3.Row) -> StoredRoleResult:
    role = Role(row["role"])
    model: (
        type[PlannerResult]
        | type[PlanChallengeResult]
        | type[ImplementerResult]
        | type[ReviewerResult]
    )
    if role is Role.PLANNER:
        model = PlannerResult
    elif role is Role.IMPLEMENTER:
        try:
            document = json.loads(row["result_json"])
        except json.JSONDecodeError as exc:
            raise StorageError("stored role result is invalid") from exc
        model = (
            PlanChallengeResult
            if isinstance(document, dict) and "issues" in document and "decision" in document
            else ImplementerResult
        )
    else:
        model = ReviewerResult
    try:
        result = model.model_validate_json(row["result_json"])
    except ValueError as exc:
        raise StorageError("stored role result is invalid") from exc
    return StoredRoleResult(
        run_id=row["run_id"],
        role=role,
        attempt=row["attempt"],
        result=result,
        content_sha256=row["content_sha256"],
        created_at=datetime.fromisoformat(row["created_at"]),
    )


def _invocation_from_row(row: sqlite3.Row) -> InvocationRecord:
    termination = row["termination_reason"]
    category = row["error_category"]
    usage_values = (
        row["input_tokens"],
        row["output_tokens"],
        row["cache_read_input_tokens"],
        row["cache_creation_input_tokens"],
        row["provider_turns"],
    )
    usage = (
        ProviderUsage(
            input_tokens=usage_values[0],
            output_tokens=usage_values[1],
            cache_read_input_tokens=usage_values[2],
            cache_creation_input_tokens=usage_values[3],
            turns=usage_values[4],
        )
        if any(value is not None for value in usage_values)
        else None
    )
    return InvocationRecord(
        invocation_id=row["invocation_id"],
        run_id=row["run_id"],
        role=Role(row["role"]),
        attempt=row["attempt"],
        harness=HarnessKind(row["harness"]),
        status=InvocationStatus(row["status"]),
        timeout_seconds=row["timeout_seconds"],
        output_limit_bytes=row["output_limit_bytes"],
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=datetime.fromisoformat(row["ended_at"]) if row["ended_at"] else None,
        termination_reason=TerminationReason(termination) if termination else None,
        returncode=row["returncode"],
        duration_seconds=row["duration_seconds"],
        stdout_sha256=row["stdout_sha256"],
        stderr_sha256=row["stderr_sha256"],
        result_sha256=row["result_sha256"],
        error_category=ErrorCategory(category) if category else None,
        usage=usage,
    )


def _check_from_row(row: sqlite3.Row) -> CheckRecord:
    return CheckRecord(
        check_id=row["check_id"],
        run_id=row["run_id"],
        candidate_commit=row["candidate_commit"],
        command_id=row["command_id"],
        ordinal=row["ordinal"],
        required=bool(row["required"]),
        passed=bool(row["passed"]),
        termination_reason=TerminationReason(row["termination_reason"]),
        returncode=row["returncode"],
        started_at=datetime.fromisoformat(row["started_at"]),
        ended_at=datetime.fromisoformat(row["ended_at"]),
        duration_seconds=row["duration_seconds"],
        stdout_sha256=row["stdout_sha256"],
        stderr_sha256=row["stderr_sha256"],
    )


def _participant_from_row(row: sqlite3.Row) -> ParticipantEvidence:
    return ParticipantEvidence(
        role=Role(row["role"]),
        did=row["did"],
        harness=HarnessKind(row["harness"]) if row["harness"] is not None else None,
        model=row["model"],
        cli_name=row["cli_name"],
        cli_version=row["cli_version"],
        executable_path=Path(row["executable_path"])
        if row["executable_path"] is not None
        else None,
        executable_sha256=row["executable_sha256"],
        executable_size_bytes=row["executable_size_bytes"],
        structured_output=bool(row["structured_output"])
        if row["structured_output"] is not None
        else None,
        resumable=bool(row["resumable"]) if row["resumable"] is not None else None,
        recorded_at=datetime.fromisoformat(row["recorded_at"]).astimezone(UTC),
    )


def _validated_participant_set(
    participants: tuple[ParticipantEvidence, ...],
) -> tuple[tuple[object, ...], ...]:
    roles = tuple(participant.role for participant in participants)
    if len(set(roles)) != len(roles):
        raise StorageError("participant evidence contains a duplicate role")
    return tuple(_participant_facts(participant) for participant in participants)


def _participant_facts(participant: ParticipantEvidence) -> tuple[object, ...]:
    if participant.role not in {
        Role.SUPERVISOR,
        Role.PLANNER,
        Role.IMPLEMENTER,
        Role.REVIEWER,
        Role.VERIFIER,
    }:
        raise StorageError("participant evidence contains an unsupported role")
    if not DID_RE.fullmatch(participant.did):
        raise StorageError("participant DID is not a canonical Ed25519 did:key")
    model_role = participant.role in _MODEL_ROLES
    provider_values = (
        participant.harness,
        participant.model,
        participant.cli_name,
        participant.cli_version,
        participant.executable_path,
        participant.executable_sha256,
        participant.executable_size_bytes,
        participant.structured_output,
        participant.resumable,
    )
    if model_role and any(value is None for value in provider_values):
        raise StorageError("model participant evidence is incomplete")
    if not model_role and any(value is not None for value in provider_values):
        raise StorageError("non-model participant evidence contains provider facts")
    if model_role:
        if (
            not isinstance(participant.harness, HarnessKind)
            or participant.harness is HarnessKind.FAKE
        ):
            raise StorageError("real participant evidence cannot name the fake harness")
        for label, value, maximum in (
            ("model", participant.model, 200),
            ("CLI name", participant.cli_name, 200),
            ("CLI version", participant.cli_version, 256),
        ):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > maximum
                or any(character in value for character in "\x00\r\n")
            ):
                raise StorageError(f"participant {label} is invalid")
        if (
            participant.executable_path is None
            or not isinstance(participant.executable_path, Path)
            or not participant.executable_path.is_absolute()
        ):
            raise StorageError("participant executable path must be absolute")
        if participant.executable_sha256 is None:
            raise StorageError("participant executable digest is missing")
        _validate_digest(participant.executable_sha256, "participant executable")
        if (
            isinstance(participant.executable_size_bytes, bool)
            or not isinstance(participant.executable_size_bytes, int)
            or participant.executable_size_bytes < 1
        ):
            raise StorageError("participant executable size must be positive")
        if not isinstance(participant.structured_output, bool) or not isinstance(
            participant.resumable, bool
        ):
            raise StorageError("participant capability flags must be boolean")
    return (
        participant.role.value,
        participant.did,
        participant.harness.value if participant.harness else None,
        participant.model,
        participant.cli_name,
        participant.cli_version,
        str(participant.executable_path) if participant.executable_path else None,
        participant.executable_sha256,
        participant.executable_size_bytes,
        int(participant.structured_output) if participant.structured_output is not None else None,
        int(participant.resumable) if participant.resumable is not None else None,
    )


def _validate_role_result_type(role: Role, result: ModelResult) -> None:
    expected = {
        Role.PLANNER: PlannerResult,
        Role.IMPLEMENTER: (PlanChallengeResult, ImplementerResult),
        Role.REVIEWER: ReviewerResult,
    }.get(role)
    if expected is None or not isinstance(result, expected):
        raise StorageError("role result type does not match its role")


def _validate_collaboration_message(
    envelope: CollaborationEnvelope,
    payload: CollaborationPayload,
) -> None:
    expected_payload = {
        CollaborationKind.PLAN_PROPOSED: PlannerResult,
        CollaborationKind.PLAN_CHALLENGED: PlanChallengeResult,
        CollaborationKind.PLAN_FINALIZED: PlannerResult,
        CollaborationKind.HANDOFF_ACKNOWLEDGED: HandoffAcknowledgement,
        CollaborationKind.IMPLEMENTATION_SUBMITTED: ImplementerResult,
        CollaborationKind.CANDIDATE_READY: CandidateHandoff,
        CollaborationKind.FINDINGS_RESOLVED: FindingsResolutionResult,
        CollaborationKind.REVIEW_SUBMITTED: ReviewerResult,
    }[envelope.kind]
    expected_sender = {
        CollaborationKind.PLAN_PROPOSED: {Role.PLANNER},
        CollaborationKind.PLAN_CHALLENGED: {Role.IMPLEMENTER},
        CollaborationKind.PLAN_FINALIZED: {Role.PLANNER},
        CollaborationKind.HANDOFF_ACKNOWLEDGED: {
            Role.PLANNER,
            Role.IMPLEMENTER,
            Role.REVIEWER,
        },
        CollaborationKind.IMPLEMENTATION_SUBMITTED: {Role.IMPLEMENTER},
        CollaborationKind.CANDIDATE_READY: {Role.IMPLEMENTER},
        CollaborationKind.FINDINGS_RESOLVED: {Role.IMPLEMENTER},
        CollaborationKind.REVIEW_SUBMITTED: {Role.REVIEWER},
    }[envelope.kind]
    if not isinstance(payload, expected_payload):
        raise StorageError("collaboration payload type does not match its message kind")
    if envelope.sender not in expected_sender:
        raise StorageError("collaboration sender is not authorized for its message kind")
    if collaboration_payload_sha256(payload) != envelope.payload_sha256:
        raise StorageError("collaboration payload does not match its signed digest")
    reply_required = envelope.kind is not CollaborationKind.PLAN_PROPOSED
    if reply_required != (envelope.reply_to is not None):
        raise StorageError("collaboration reply relationship does not match its message kind")
    if isinstance(payload, HandoffAcknowledgement) and payload.event_id != envelope.reply_to:
        raise StorageError("acknowledgement payload does not match its reply target")


def _validate_invocation_identity(role: Role, attempt: int) -> None:
    if role not in _MODEL_ROLES:
        raise StorageError("role does not own a model-harness invocation")
    if isinstance(attempt, bool) or not isinstance(attempt, int) or not 1 <= attempt <= 100:
        raise StorageError("invocation attempt must be between 1 and 100")


def _usage_facts(
    usage: ProviderUsage | None,
) -> tuple[int | None, int | None, int | None, int | None, int | None]:
    if usage is None:
        return (None, None, None, None, None)
    if not isinstance(usage, ProviderUsage):
        raise StorageError("invocation usage must use the validated provider usage contract")
    return (
        usage.input_tokens,
        usage.output_tokens,
        usage.cache_read_input_tokens,
        usage.cache_creation_input_tokens,
        usage.turns,
    )


def _validated_check_facts(
    check: CheckEvidence,
) -> tuple[str, int, int, int, str, int, str, str, float, str, str]:
    if not _IDENTIFIER_RE.fullmatch(check.command_id):
        raise StorageError("check command id is invalid")
    if isinstance(check.ordinal, bool) or not isinstance(check.ordinal, int) or check.ordinal < 1:
        raise StorageError("check ordinal must be a positive integer")
    if not isinstance(check.required, bool) or not isinstance(check.passed, bool):
        raise StorageError("check required and passed values must be boolean")
    if isinstance(check.returncode, bool) or not isinstance(check.returncode, int):
        raise StorageError("check return code must be an integer")
    started = _utc(check.started_at)
    ended = _utc(check.ended_at)
    if ended < started:
        raise StorageError("check end timestamp precedes its start timestamp")
    _validate_duration(check.duration_seconds, "check duration")
    _validate_digest(check.stdout_sha256, "check stdout")
    _validate_digest(check.stderr_sha256, "check stderr")
    return (
        check.command_id,
        check.ordinal,
        int(check.required),
        int(check.passed),
        check.termination_reason.value,
        check.returncode,
        started.isoformat(),
        ended.isoformat(),
        float(check.duration_seconds),
        check.stdout_sha256,
        check.stderr_sha256,
    )


def _validate_invocation_limits(timeout_seconds: float, output_limit_bytes: int) -> None:
    if (
        isinstance(timeout_seconds, bool)
        or not isinstance(timeout_seconds, (int, float))
        or not math.isfinite(timeout_seconds)
        or not 0 < timeout_seconds <= 7_200
    ):
        raise StorageError("invocation timeout must be finite and at most two hours")
    if (
        isinstance(output_limit_bytes, bool)
        or not isinstance(output_limit_bytes, int)
        or not 1 <= output_limit_bytes <= 10 * 1024 * 1024
    ):
        raise StorageError("invocation output limit must be between 1 byte and 10 MiB")


def _validate_duration(value: float, label: str) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise StorageError(f"{label} must be finite and non-negative")


def _validate_digest(value: str, label: str) -> None:
    if not isinstance(value, str) or not SHA256_RE.fullmatch(value):
        raise StorageError(f"{label} digest must be lowercase SHA-256")


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise StorageError("storage timestamp must be timezone-aware")
    return value.astimezone(UTC)
