"""Versioned, redacted reports derived from durable workflow facts."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import tempfile
import unicodedata
from collections import Counter
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path
from typing import Any
from uuid import uuid4

from technocore_orchestrator import __version__
from technocore_orchestrator.config import LoadedConfig
from technocore_orchestrator.domain.collaboration import CandidateHandoff
from technocore_orchestrator.domain.models import (
    RUN_ID_RE,
    SHA256_RE,
    EventKind,
    HarnessKind,
    ImplementerResult,
    Role,
    RunState,
    canonical_repository_path,
)
from technocore_orchestrator.domain.usage import ProviderUsage
from technocore_orchestrator.errors import StorageError
from technocore_orchestrator.storage import (
    InvocationRecord,
    SQLiteStore,
    StoredCollaborationMessage,
    StoredEvent,
)
from technocore_orchestrator.windows_lock import WindowsFileLease, acquire_windows_file_lock

REPORT_VERSION = 3
_ROOM_RE = re.compile(
    r"(?<![a-z0-9_-])(?:d-p-[a-z0-9][a-z0-9_-]{6,43}|"
    r"p-[a-z0-9][a-z0-9_-]{6,45})(?![a-z0-9_-])"
)
_PEM_RE = re.compile(r"-----BEGIN [^-\r\n]+-----.*?-----END [^-\r\n]+-----", re.DOTALL)
_BEARER_RE = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/=-]{8,}")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|password|passphrase|secret|seed|sign_seed)\b"
    r"(\s*[:=]\s*)([^\s,;]+)"
)
_TOKEN_RE = re.compile(
    r"(?<![A-Za-z0-9])(?:sk-(?:ant-|proj-)?[A-Za-z0-9_-]{12,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|xox[baprs]-[A-Za-z0-9-]{12,})(?![A-Za-z0-9])"
)


@dataclass(frozen=True, slots=True)
class ReportArtifacts:
    directory: Path
    run_json: Path
    events_jsonl: Path
    conversation_jsonl: Path
    report_markdown: Path
    run_json_sha256: str
    events_jsonl_sha256: str
    conversation_jsonl_sha256: str
    report_markdown_sha256: str


class Redactor:
    """Remove known secret forms and unsafe display controls before persistence."""

    def __init__(self, *, secret_values: tuple[str, ...] = ()) -> None:
        self._secret_values = tuple(
            sorted({value for value in secret_values if len(value) >= 4}, key=len, reverse=True)
        )

    def text(self, value: str) -> str:
        redacted = value
        for secret in self._secret_values:
            redacted = redacted.replace(secret, "[REDACTED]")
        redacted = _PEM_RE.sub("[REDACTED PEM]", redacted)
        redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
        redacted = _TOKEN_RE.sub("[REDACTED TOKEN]", redacted)
        redacted = _ROOM_RE.sub("[REDACTED ROOM]", redacted)
        redacted = _SECRET_ASSIGNMENT_RE.sub(r"\1\2[REDACTED]", redacted)
        return "".join(
            " " if _is_unsafe_display_character(character) else character for character in redacted
        )

    def value(self, value: Any) -> Any:
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.value(item) for item in value]
        if isinstance(value, tuple):
            return [self.value(item) for item in value]
        if isinstance(value, dict):
            return {str(key): self.value(item) for key, item in value.items()}
        return value


def generate_reports(
    *,
    store: SQLiteStore,
    loaded_config: LoadedConfig,
    run_id: str,
    output_root: Path,
    room_hash: str | None = None,
    secret_values: tuple[str, ...] = (),
    generated_at: datetime | None = None,
) -> ReportArtifacts:
    """Regenerate one internally consistent report set using atomic file replacement."""

    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("report run id is invalid", context={"run_id": run_id})
    if room_hash is not None and not SHA256_RE.fullmatch(room_hash):
        raise StorageError("report room hash is invalid")
    now = (generated_at or datetime.now(UTC)).astimezone(UTC)
    generation_id = str(uuid4())
    redactor = Redactor(secret_values=secret_values)
    directory = _prepare_report_directory(output_root, run_id)
    lease = _acquire_generation_lock(directory / ".generation.lock")
    try:
        with store.read_snapshot():
            payload = _build_run_payload(
                store=store,
                loaded_config=loaded_config,
                run_id=run_id,
                generated_at=now,
                generation_id=generation_id,
                redactor=redactor,
                room_hash=room_hash,
            )
            event_bytes = _events_jsonl(
                store.list_event_records(run_id),
                generation_id=generation_id,
                redactor=redactor,
            )
            conversation_bytes = _conversation_jsonl(
                store.list_collaboration_messages(run_id),
                generation_id=generation_id,
                redactor=redactor,
            )
        event_digest = _sha256(event_bytes)
        conversation_digest = _sha256(conversation_bytes)
        markdown_bytes = _markdown_report(
            payload,
            event_digest=event_digest,
            conversation_digest=conversation_digest,
        ).encode("utf-8")
        markdown_digest = _sha256(markdown_bytes)
        payload["artifacts"] = {
            "events.jsonl": {"sha256": event_digest, "bytes": len(event_bytes)},
            "conversation.jsonl": {
                "sha256": conversation_digest,
                "bytes": len(conversation_bytes),
            },
            "report.md": {"sha256": markdown_digest, "bytes": len(markdown_bytes)},
        }
        run_bytes = (
            json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")

        events_path = directory / "events.jsonl"
        conversation_path = directory / "conversation.jsonl"
        markdown_path = directory / "report.md"
        run_path = directory / "run.json"
        _atomic_write(events_path, event_bytes)
        _atomic_write(conversation_path, conversation_bytes)
        _atomic_write(markdown_path, markdown_bytes)
        _atomic_write(run_path, run_bytes)
    finally:
        lease.close()
    return ReportArtifacts(
        directory=directory,
        run_json=run_path,
        events_jsonl=events_path,
        conversation_jsonl=conversation_path,
        report_markdown=markdown_path,
        run_json_sha256=_sha256(run_bytes),
        events_jsonl_sha256=event_digest,
        conversation_jsonl_sha256=conversation_digest,
        report_markdown_sha256=markdown_digest,
    )


def build_status_payload(store: SQLiteStore, run_id: str, *, recent: int = 5) -> dict[str, Any]:
    """Build a bounded status view without exposing event summaries or private paths."""

    if not RUN_ID_RE.fullmatch(run_id):
        raise StorageError("status run id is invalid", context={"run_id": run_id})
    if isinstance(recent, bool) or not 0 <= recent <= 100:
        raise ValueError("recent event count must be between zero and 100")
    run = store.get_run(run_id)
    events = store.list_event_records(run_id)
    collaboration = store.list_collaboration_messages(run_id)
    counters = store.get_run_counters(run_id)
    invocations = store.list_invocations(run_id)
    selected = events[-recent:] if recent else ()
    return {
        "run_id": run.run_id,
        "task_id": run.task_id,
        "state": run.state.value,
        "terminal": run.state.is_terminal,
        "created_at": run.created_at.isoformat(),
        "updated_at": run.updated_at.isoformat(),
        "invocation_attempts": counters.invocation_attempts,
        "revision_cycles": counters.revision_cycles,
        "collaboration_messages": len(collaboration),
        "active_invocations": sum(
            1 for invocation in invocations if invocation.status.value == "started"
        ),
        "transport": dict(
            sorted(Counter(event.transport_status.value for event in events).items())
        ),
        "recent_events": [
            {
                "event_id": str(record.event.event_id),
                "kind": record.event.kind.value,
                "sender": record.event.sender.value,
                "attempt": record.event.attempt,
                "created_at": record.event.created_at.isoformat(),
                "transport_status": record.transport_status.value,
                "technocore_seq": record.technocore_seq,
            }
            for record in selected
        ],
    }


def _build_run_payload(
    *,
    store: SQLiteStore,
    loaded_config: LoadedConfig,
    run_id: str,
    generated_at: datetime,
    generation_id: str,
    redactor: Redactor,
    room_hash: str | None,
) -> dict[str, Any]:
    run = store.get_run(run_id)
    config = loaded_config.config
    if (
        run.config_digest != loaded_config.sha256
        or run.repository_path != config.repository.path
        or run.base_commit != config.repository.base_commit
        or run.task_id != config.task.id
    ):
        raise StorageError("report configuration does not match the durable run record")

    events = store.list_event_records(run_id)
    collaboration = store.list_collaboration_messages(run_id)
    invocations = store.list_invocations(run_id)
    role_results = store.list_role_results(run_id)
    participants = store.list_participants(run_id)
    checks = store.list_checks(run_id)
    counters = store.get_run_counters(run_id)
    candidate = _latest_candidate(events)
    changed_paths = _candidate_changed_paths(
        store,
        run_id,
        candidate,
        collaboration,
        role_results,
    )
    published_sequences = sorted(
        [record.technocore_seq for record in events if record.technocore_seq is not None]
        + [
            message.technocore_seq
            for message in collaboration
            if message.technocore_seq is not None
        ]
    )
    if len(published_sequences) != len(set(published_sequences)):
        raise StorageError("report found duplicate Technocore sequence assignments")
    gaps = _sequence_gaps(published_sequences)
    security_events = [
        record for record in events if record.event.kind is EventKind.SECURITY_WARNING
    ]
    latest_results = {(result.role, result.attempt): result for result in role_results}
    harness_by_role = {
        Role.PLANNER: config.roles.planner,
        Role.IMPLEMENTER: config.roles.implementer,
        Role.REVIEWER: config.roles.reviewer,
    }
    participant_by_role = {participant.role: participant for participant in participants}

    roles: list[dict[str, Any]] = []
    for role in (Role.PLANNER, Role.IMPLEMENTER, Role.REVIEWER):
        harness = harness_by_role[role]
        evidence = participant_by_role.get(role)
        profile = {
            HarnessKind.CODEX: config.providers.codex,
            HarnessKind.CLAUDE: config.providers.claude,
            HarnessKind.FAKE: None,
        }[harness]
        attempts = []
        for invocation in (item for item in invocations if item.role is role):
            if invocation.harness is not harness:
                raise StorageError(
                    "durable invocation harness does not match the validated role assignment"
                )
            result = latest_results.get((role, invocation.attempt))
            limits: dict[str, int | float] = {
                "wall_seconds": invocation.timeout_seconds,
                "output_bytes": invocation.output_limit_bytes,
            }
            if harness is HarnessKind.CLAUDE and profile is not None:
                limits["provider_max_turns"] = config.limits.claude_max_turns
            attempts.append(
                {
                    "attempt": invocation.attempt,
                    "status": invocation.status.value,
                    "started_at": invocation.started_at.isoformat(),
                    "ended_at": invocation.ended_at.isoformat() if invocation.ended_at else None,
                    "duration_seconds": invocation.duration_seconds,
                    "termination_reason": invocation.termination_reason.value
                    if invocation.termination_reason
                    else None,
                    "returncode": invocation.returncode,
                    "stdout_sha256": invocation.stdout_sha256,
                    "stderr_sha256": invocation.stderr_sha256,
                    "result_sha256": result.content_sha256 if result else None,
                    "result_type": type(result.result).__name__ if result else None,
                    "result": result.result.model_dump(mode="json") if result else None,
                    "error_category": invocation.error_category.value
                    if invocation.error_category
                    else None,
                    "limits": limits,
                    "provider_usage": _usage_payload(invocation.usage),
                }
            )
        roles.append(
            {
                "role": role.value,
                "harness": harness.value,
                "did": evidence.did if evidence else None,
                "model": evidence.model if evidence else (profile.model if profile else None),
                "version": evidence.cli_version
                if evidence
                else (
                    profile.expected_version
                    if profile
                    else ("1" if harness is HarnessKind.FAKE else None)
                ),
                "executable_sha256": evidence.executable_sha256 if evidence else None,
                "executable_size_bytes": evidence.executable_size_bytes if evidence else None,
                "executable_path_sha256": hashlib.sha256(
                    str(evidence.executable_path).encode("utf-8")
                ).hexdigest()
                if evidence and evidence.executable_path
                else None,
                "structured_output": evidence.structured_output if evidence else None,
                "resumable": evidence.resumable if evidence else None,
                "attempts": attempts,
            }
        )

    required_checks = [check for check in checks if check.required]
    verification_passed = all(check.passed for check in required_checks) if checks else None
    successful_invocations = tuple(
        invocation for invocation in invocations if invocation.status.value == "succeeded"
    )
    usage_reporting_attempts = sum(
        invocation.usage is not None for invocation in successful_invocations
    )
    usage_coverage = {
        "successful_invocations": len(successful_invocations),
        "invocations_with_any_reported_usage": usage_reporting_attempts,
        "input_tokens": _usage_field_count(successful_invocations, "input_tokens"),
        "output_tokens": _usage_field_count(successful_invocations, "output_tokens"),
        "cache_read_input_tokens": _usage_field_count(
            successful_invocations, "cache_read_input_tokens"
        ),
        "cache_creation_input_tokens": _usage_field_count(
            successful_invocations, "cache_creation_input_tokens"
        ),
        "turns": _usage_field_count(successful_invocations, "turns"),
    }
    limitations = ["The private Technocore room capability is intentionally absent from reports."]
    if successful_invocations and any(
        usage_coverage[field] < len(successful_invocations)
        for field in (
            "input_tokens",
            "output_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "turns",
        )
    ):
        limitations.append(
            "One or more provider usage fields are unavailable for successful invocations; "
            "missing values remain null and the affected aggregate totals are withheld."
        )
    if any(
        role["harness"] != HarnessKind.FAKE.value and role["executable_sha256"] is None
        for role in roles
    ):
        limitations.append(
            "Real executable content digests are unavailable for this historical run."
        )
    if not security_events:
        limitations.append("No persisted secret-canary measurement is available for this run.")

    raw_payload: dict[str, Any] = {
        "report_version": REPORT_VERSION,
        "generation_id": generation_id,
        "generated_at": generated_at.isoformat(),
        "run": {
            "id": run.run_id,
            "task_id": run.task_id,
            "outcome": run.state.value,
            "started_at": run.created_at.isoformat(),
            "finished_at": run.updated_at.isoformat() if run.state.is_terminal else None,
            "configuration_sha256": run.config_digest,
        },
        "environment": {
            "application_version": __version__,
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "technocore": {
                "version": config.technocore.expected_version,
                "commit": config.technocore.expected_commit,
            },
            "harnesses": [
                {
                    "role": item["role"],
                    "name": item["harness"],
                    "model": item["model"],
                    "version": item["version"],
                    "did": item["did"],
                    "executable_sha256": item["executable_sha256"],
                    "executable_size_bytes": item["executable_size_bytes"],
                    "executable_path_sha256": item["executable_path_sha256"],
                    "structured_output": item["structured_output"],
                    "resumable": item["resumable"],
                }
                for item in roles
            ],
            "role_identities": [
                {"role": participant.role.value, "did": participant.did}
                for participant in participants
            ],
        },
        "task": {
            "title": config.task.title,
            "acceptance_criteria": list(config.task.acceptance_criteria),
        },
        "repository": {
            "base_commit": run.base_commit,
            "candidate_commit": candidate,
            "changed_paths": list(changed_paths),
        },
        "transport": {
            "room_hash": room_hash,
            "first_event_seq": published_sequences[0] if published_sequences else None,
            "last_event_seq": published_sequences[-1] if published_sequences else None,
            "gaps": gaps,
            "events": len(events),
            "collaboration_messages": len(collaboration),
            "statuses": dict(
                sorted(
                    Counter(
                        [record.transport_status.value for record in events]
                        + [message.transport_status.value for message in collaboration]
                    ).items()
                )
            ),
        },
        "conversation": [
            {
                "sequence": message.technocore_seq,
                "event_id": str(message.envelope.event_id),
                "created_at": message.envelope.created_at.isoformat(),
                "sender": message.envelope.sender.value,
                "kind": message.envelope.kind.value,
                "reply_to": str(message.envelope.reply_to) if message.envelope.reply_to else None,
                "text": message.envelope.text,
                "payload_sha256": message.envelope.payload_sha256,
                "transport_status": message.transport_status.value,
            }
            for message in collaboration
        ],
        "roles": roles,
        "verification": {
            "passed": verification_passed,
            "candidate_commit": candidate if checks else None,
            "checks": [
                {
                    "id": check.command_id,
                    "ordinal": check.ordinal,
                    "required": check.required,
                    "passed": check.passed,
                    "termination_reason": check.termination_reason.value,
                    "returncode": check.returncode,
                    "started_at": check.started_at.isoformat(),
                    "ended_at": check.ended_at.isoformat(),
                    "duration_seconds": check.duration_seconds,
                    "stdout_sha256": check.stdout_sha256,
                    "stderr_sha256": check.stderr_sha256,
                }
                for check in checks
            ],
        },
        "metrics": {
            "invocation_attempts": counters.invocation_attempts,
            "revision_cycles": counters.revision_cycles,
            "invocation_wall_seconds": sum(
                invocation.duration_seconds or 0.0 for invocation in invocations
            ),
            "provider_usage_coverage": usage_coverage,
            "provider_input_tokens": _complete_usage_sum(successful_invocations, "input_tokens"),
            "provider_output_tokens": _complete_usage_sum(successful_invocations, "output_tokens"),
            "provider_cache_read_input_tokens": _complete_usage_sum(
                successful_invocations, "cache_read_input_tokens"
            ),
            "provider_cache_creation_input_tokens": _complete_usage_sum(
                successful_invocations, "cache_creation_input_tokens"
            ),
            "provider_turns": _complete_usage_sum(successful_invocations, "turns"),
        },
        "security": {
            "canaries_exposed": None,
            "violations": [
                {
                    "event_id": str(record.event.event_id),
                    "summary": record.event.summary,
                }
                for record in security_events
            ],
        },
        "limitations": limitations,
    }
    return redactor.value(raw_payload)


def _usage_payload(usage: ProviderUsage | None) -> dict[str, int | None] | None:
    if usage is None:
        return None
    return {
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cache_read_input_tokens": usage.cache_read_input_tokens,
        "cache_creation_input_tokens": usage.cache_creation_input_tokens,
        "turns": usage.turns,
    }


def _complete_usage_sum(invocations: tuple[InvocationRecord, ...], field: str) -> int | None:
    if not invocations:
        return None
    values: list[int] = []
    for invocation in invocations:
        if invocation.usage is None:
            return None
        value = getattr(invocation.usage, field)
        if value is None:
            return None
        values.append(value)
    return sum(values)


def _usage_field_count(invocations: tuple[InvocationRecord, ...], field: str) -> int:
    return sum(
        invocation.usage is not None and getattr(invocation.usage, field) is not None
        for invocation in invocations
    )


def _events_jsonl(
    records: tuple[StoredEvent, ...], *, generation_id: str, redactor: Redactor
) -> bytes:
    lines: list[str] = []
    for record in records:
        envelope = redactor.value(record.event.model_dump(mode="json"))
        payload = {
            "report_version": REPORT_VERSION,
            "generation_id": generation_id,
            "redacted": True,
            "event": envelope,
            "local": {
                "accepted_at": record.accepted_at.isoformat(),
                "content_sha256": record.content_sha256,
                "transport_status": record.transport_status.value,
                "technocore_seq": record.technocore_seq,
            },
        }
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def _conversation_jsonl(
    records: tuple[StoredCollaborationMessage, ...],
    *,
    generation_id: str,
    redactor: Redactor,
) -> bytes:
    lines: list[str] = []
    for record in records:
        payload = {
            "report_version": REPORT_VERSION,
            "generation_id": generation_id,
            "redacted": True,
            "message": redactor.value(record.envelope.model_dump(mode="json")),
            "local": {
                "transport_status": record.transport_status.value,
                "technocore_seq": record.technocore_seq,
            },
        }
        lines.append(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return (("\n".join(lines) + "\n") if lines else "").encode("utf-8")


def _markdown_report(
    payload: dict[str, Any], *, event_digest: str, conversation_digest: str
) -> str:
    run = payload["run"]
    repository = payload["repository"]
    verification = payload["verification"]
    transport = payload["transport"]
    metrics = payload["metrics"]
    task = payload["task"]
    lines = [
        "# Technocore Agent Orchestrator Report",
        "",
        f"Generation: `{payload['generation_id']}`  ",
        f"Run: `{run['id']}`  ",
        f"Outcome: **{_md(run['outcome'])}**",
        "",
        "## Conclusion",
        "",
        _conclusion(run["outcome"], verification["passed"]),
        "",
        "## Task and repository",
        "",
        f"Task: {_md(task['title'])}  ",
        f"Base commit: `{repository['base_commit']}`  ",
        f"Candidate commit: `{repository['candidate_commit'] or 'not produced'}`",
        "",
        "Acceptance criteria:",
        "",
        *[f"- {_md(item)}" for item in task["acceptance_criteria"]],
        "",
        "Changed paths:",
        "",
        *([f"- `{_md(path)}`" for path in repository["changed_paths"]] or ["- None recorded."]),
        "",
        "## Role execution",
        "",
        "| Role | Harness | Attempts | Successful | Wall seconds |",
        "|---|---|---:|---:|---:|",
    ]
    for role in payload["roles"]:
        attempts = role["attempts"]
        lines.append(
            "| "
            + " | ".join(
                (
                    _md(role["role"]),
                    _md(role["harness"]),
                    str(len(attempts)),
                    str(sum(1 for item in attempts if item["status"] == "succeeded")),
                    f"{sum(item['duration_seconds'] or 0.0 for item in attempts):.3f}",
                )
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Deterministic verification",
            "",
            f"Overall result: **{_md(_tri_state(verification['passed']))}**",
            "",
            "| Check | Required | Result | Return code | Wall seconds |",
            "|---|---:|---|---:|---:|",
        ]
    )
    for check in verification["checks"]:
        lines.append(
            f"| {_md(check['id'])} | {check['required']} | "
            f"{_md(_tri_state(check['passed']))} | {check['returncode']} | "
            f"{check['duration_seconds']:.3f} |"
        )
    if not verification["checks"]:
        lines.append("| None recorded | - | not available | - | - |")
    lines.extend(
        [
            "",
            "## Transport and metrics",
            "",
            f"Accepted events: {transport['events']}  ",
            f"Shared conversation messages: {transport['collaboration_messages']}  ",
            f"Published sequence range: {transport['first_event_seq']}-{transport['last_event_seq']}  ",
            f"Detected sequence gaps: {len(transport['gaps'])}  ",
            f"Invocation attempts: {metrics['invocation_attempts']}  ",
            f"Revision cycles: {metrics['revision_cycles']}  ",
            f"Invocation wall time: {metrics['invocation_wall_seconds']:.3f} seconds  ",
            "Provider usage reports: "
            f"{metrics['provider_usage_coverage']['invocations_with_any_reported_usage']}/"
            f"{metrics['provider_usage_coverage']['successful_invocations']} successful attempts",
            "",
            "## Security and limitations",
            "",
            f"Recorded security warnings: {len(payload['security']['violations'])}. The private "
            "room capability, prompts, raw provider output, credentials, and encrypted identities "
            "are intentionally absent.",
            "",
            *[f"- {_md(item)}" for item in payload["limitations"]],
            "",
            "## Artifact integrity",
            "",
            f"`events.jsonl` SHA-256: `{event_digest}`",
            f"`conversation.jsonl` SHA-256: `{conversation_digest}`",
            "",
            "Regenerate with the same application version, validated configuration, and retained "
            "SQLite database. A regenerated timestamp and generation ID will differ.",
            "",
        ]
    )
    return "\n".join(lines)


def _prepare_report_directory(output_root: Path, run_id: str) -> Path:
    try:
        root = output_root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        root = root.resolve(strict=True)
        directory = root / run_id
        if directory.is_symlink():
            raise StorageError("report directory must not be a symlink")
        directory.mkdir(mode=0o700, exist_ok=True)
        resolved = directory.resolve(strict=True)
    except StorageError:
        raise
    except OSError as exc:
        raise StorageError(
            "unable to prepare report directory", context={"reason": str(exc)}
        ) from exc
    if resolved.parent != root or resolved != directory:
        raise StorageError("report directory escaped its configured root")
    return resolved


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor: int | None = None
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = None
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        temporary = None
    except OSError as exc:
        raise StorageError("unable to write report artifact", context={"file": path.name}) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink(missing_ok=True)


def _acquire_generation_lock(path: Path) -> WindowsFileLease:
    return acquire_windows_file_lock(
        path,
        label="report generation lock",
        blocking=False,
        unavailable_message="another process is generating this run report",
    )


def _latest_candidate(events: tuple[StoredEvent, ...]) -> str | None:
    candidates = [
        record.event.artifact.value
        for record in events
        if record.event.kind is EventKind.IMPLEMENTATION_READY and record.event.artifact is not None
    ]
    return candidates[-1] if candidates else None


def _latest_changed_paths(role_results: tuple[Any, ...]) -> tuple[str, ...]:
    implementations = [
        result.result
        for result in role_results
        if result.role is Role.IMPLEMENTER and isinstance(result.result, ImplementerResult)
    ]
    return implementations[-1].declared_changed_paths if implementations else ()


def _candidate_changed_paths(
    store: SQLiteStore,
    run_id: str,
    candidate: str | None,
    collaboration: tuple[StoredCollaborationMessage, ...],
    role_results: tuple[Any, ...],
) -> tuple[str, ...]:
    if candidate is None:
        return ()
    observation = store.get_latest_worktree_observation(run_id, Role.IMPLEMENTER)
    if observation.head_commit != candidate:
        raise StorageError("reported candidate differs from the latest implementer observation")
    if observation.changed_paths:
        return observation.changed_paths
    handoffs = tuple(
        message.payload
        for message in collaboration
        if isinstance(message.payload, CandidateHandoff)
        and message.payload.candidate_commit == candidate
    )
    if len(handoffs) > 1:
        raise StorageError("reported candidate has multiple collaboration handoffs")
    if handoffs:
        return tuple(canonical_repository_path(path) for path in handoffs[0].changed_paths)
    return tuple(canonical_repository_path(path) for path in _latest_changed_paths(role_results))


def _sequence_gaps(sequences: list[int]) -> list[dict[str, int]]:
    return [
        {"after": left, "before": right, "missing": right - left - 1}
        for left, right in pairwise(sequences)
        if right > left + 1
    ]


def _is_unsafe_display_character(character: str) -> bool:
    category = unicodedata.category(character)
    return category.startswith("C") and character not in {"\n", "\t"}


def _md(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("`", "\\`").replace("\r", " ").replace("\n", " ")


def _tri_state(value: bool | None) -> str:
    if value is None:
        return "not available"
    return "passed" if value else "failed"


def _conclusion(outcome: str, verification: bool | None) -> str:
    if outcome == RunState.COMPLETED.value and verification is True:
        return "The workflow completed and every required deterministic verification check passed."
    if outcome in {RunState.FAILED.value, RunState.CANCELED.value}:
        return f"The workflow ended as {outcome}; retained evidence remains available for review."
    return "The workflow is not terminal; this report is a point-in-time status artifact."


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()
