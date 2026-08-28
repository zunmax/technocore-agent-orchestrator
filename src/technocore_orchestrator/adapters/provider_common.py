"""Shared strict parsing and validation for real provider CLI adapters."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from technocore_orchestrator.domain.collaboration import (
    CollaborationPhase,
    ModelResult,
    PlanChallengeResult,
)
from technocore_orchestrator.domain.models import (
    ImplementerResult,
    PlannerResult,
    ReviewerResult,
    Role,
)
from technocore_orchestrator.errors import (
    ExecutionError,
    PreflightError,
    ProtocolError,
    RoleResultValidationError,
)
from technocore_orchestrator.execution import ProcessResult, TerminationReason

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")
_PROHIBITED_SECRET_KEYS = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CURSOR_API_KEY",
        "OPENAI_API_KEY",
    }
)


def validate_adapter_inputs(
    *,
    model: str,
    expected_version: str,
    environment: tuple[tuple[str, str], ...],
) -> None:
    if (
        not model
        or len(model) > 200
        or any(unicodedata.category(character).startswith("C") for character in model)
    ):
        raise ValueError("provider model identifier is invalid")
    if not VERSION_RE.fullmatch(expected_version):
        raise ValueError("expected provider CLI version is invalid")
    secret_keys = sorted(key for key, _ in environment if key.upper() in _PROHIBITED_SECRET_KEYS)
    if secret_keys:
        raise PreflightError(
            "raw provider credentials are prohibited in adapter environments",
            context={"keys": secret_keys},
        )


def schema_path_for_invocation(schema_root: Path, role: Role, phase: CollaborationPhase) -> Path:
    names = {
        Role.PLANNER: "planner-result.schema.json",
        Role.IMPLEMENTER: "implementer-result.schema.json",
        Role.REVIEWER: "reviewer-result.schema.json",
    }
    name = (
        "plan-challenge-result.schema.json"
        if phase is CollaborationPhase.CHALLENGE_PLAN
        else names.get(role)
    )
    if name is None:
        raise ProtocolError("provider adapter received an unsupported role")
    try:
        path = (schema_root / name).resolve(strict=True)
        raw = path.read_bytes()
        if len(raw) > 256 * 1024:
            raise PreflightError("role schema exceeds the 256 KiB adapter limit")
        document = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError("unable to load checked-in role schema") from exc
    if not isinstance(document, dict):
        raise PreflightError("checked-in role schema is not a JSON object")
    return path


def compact_schema(path: Path) -> str:
    try:
        document = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError) as exc:
        raise PreflightError("unable to serialize role schema") from exc
    return json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def parse_direct_model_result(role: Role, phase: CollaborationPhase, raw: bytes) -> ModelResult:
    model = _model_for_invocation(role, phase)
    try:
        return model.model_validate_json(raw)
    except ValidationError as exc:
        raise RoleResultValidationError(
            "provider final result failed its closed role schema",
            context={"validation_errors": exc.error_count()},
        ) from exc


def parse_result_wrapper(raw: bytes) -> dict[str, Any]:
    try:
        wrapper = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RoleResultValidationError("provider CLI returned invalid JSON") from exc
    if not isinstance(wrapper, dict):
        raise RoleResultValidationError("provider CLI result wrapper is not an object")
    return wrapper


def parse_wrapped_model_result_document(
    role: Role, phase: CollaborationPhase, wrapper: dict[str, Any]
) -> ModelResult:
    if (
        wrapper.get("type") != "result"
        or wrapper.get("subtype") != "success"
        or wrapper.get("is_error") is not False
    ):
        raise ProtocolError("provider CLI did not return a successful terminal result")
    structured = wrapper.get("structured_output")
    if structured is not None:
        if not isinstance(structured, dict):
            raise RoleResultValidationError("provider structured_output is not an object")
        candidate: Any = structured
    else:
        result_text = wrapper.get("result")
        if not isinstance(result_text, str):
            raise RoleResultValidationError("provider result wrapper omitted its final text")
        try:
            candidate = json.loads(result_text)
        except json.JSONDecodeError as exc:
            raise RoleResultValidationError("provider final text is not a JSON object") from exc
    model = _model_for_invocation(role, phase)
    try:
        return model.model_validate(candidate)
    except ValidationError as exc:
        raise RoleResultValidationError(
            "provider final result failed its closed role schema",
            context={"validation_errors": exc.error_count()},
        ) from exc


def require_successful_process(process: ProcessResult, provider: str) -> None:
    if process.termination_reason is not TerminationReason.EXITED:
        raise ExecutionError(
            f"{provider} CLI did not exit normally",
            context={"termination": process.termination_reason},
        )
    if process.returncode != 0:
        raise ExecutionError(
            f"{provider} CLI exited unsuccessfully", context={"returncode": process.returncode}
        )


def parse_probe_version(raw: bytes, *, prefix: str, expected: str) -> str:
    try:
        text = raw.decode("utf-8", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise ProtocolError("provider version output is not UTF-8") from exc
    match = VERSION_RE.search(text)
    if match is None or (prefix and not text.startswith(prefix)):
        raise ProtocolError("provider version output has an unknown format")
    version = match.group(0)
    if version != expected:
        raise PreflightError(
            "provider CLI version is unsupported",
            context={"expected": expected, "installed": version},
        )
    return version


def _model_for_invocation(
    role: Role, phase: CollaborationPhase
) -> (
    type[PlannerResult] | type[PlanChallengeResult] | type[ImplementerResult] | type[ReviewerResult]
):
    if phase is CollaborationPhase.CHALLENGE_PLAN:
        if role is not Role.IMPLEMENTER:
            raise ProtocolError("plan challenge phase requires the implementer role")
        return PlanChallengeResult
    if role is Role.PLANNER:
        return PlannerResult
    if role is Role.IMPLEMENTER:
        return ImplementerResult
    if role is Role.REVIEWER:
        return ReviewerResult
    raise ProtocolError("provider adapter received an unsupported role")
