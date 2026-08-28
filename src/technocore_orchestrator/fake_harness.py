"""External deterministic harness fixture; never calls a model provider."""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Any

_MAX_INPUT_BYTES = 1024 * 1024


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Technocore Agent Orchestrator fake harness")
    parser.add_argument("--probe", action="store_true")
    parser.add_argument("--role", choices=("planner", "implementer", "reviewer"))
    parser.add_argument(
        "--phase",
        choices=(
            "propose_plan",
            "challenge_plan",
            "finalize_plan",
            "implement",
            "revise",
            "review",
        ),
    )
    parser.add_argument(
        "--scenario",
        choices=(
            "success",
            "revision_cycle",
        ),
        default="success",
    )
    parser.add_argument("--git")
    parser.add_argument("--edit-path", default="product.txt")
    arguments = parser.parse_args(argv)
    if arguments.probe:
        _write_json({"name": "technocore-orchestrator-fake-harness", "protocol": "1"})
        return 0
    if arguments.role is None or arguments.phase is None:
        parser.error("--role and --phase are required unless --probe is used")

    try:
        payload = _read_payload()
        _validate_payload(payload, arguments.role, arguments.phase)
        task_data = _read_length_framed_task(payload["prompt"])
        criterion_ids = _criterion_ids(task_data)
        verification_ids = _verification_ids(task_data)
        if arguments.phase == "challenge_plan":
            _write_json(
                {
                    "decision": "approved",
                    "summary": "The deterministic fixture plan covers the bounded task.",
                    "issues": [],
                }
            )
        elif arguments.role == "planner":
            _write_json(
                {
                    "summary": "Implement the deterministic fixture change.",
                    "steps": [
                        {
                            "id": "edit_product",
                            "description": "Update the scoped product fixture.",
                            "expected_paths": [arguments.edit_path],
                            "criterion_ids": criterion_ids,
                        }
                    ],
                    "risks": [],
                    "verification_suggestions": ["Run configured deterministic checks."],
                    "challenge_dispositions": [],
                    "blocked_reason": None,
                }
            )
        elif arguments.role == "implementer":
            revised = arguments.scenario == "revision_cycle" and arguments.phase == "revise"
            _implement(
                arguments.edit_path,
                "feature from revised fake harness\n" if revised else "feature from fake harness\n",
            )
            _write_json(
                {
                    "outcome": "changes_ready",
                    "summary": (
                        "Applied the deterministic review revision for supervisor commit."
                        if revised
                        else "Applied the deterministic fixture change for supervisor commit."
                    ),
                    "candidate_commit": None,
                    "declared_changed_paths": [arguments.edit_path],
                    "focused_checks": [],
                    "criterion_evidence": [
                        {
                            "criterion_id": criterion_id,
                            "changed_paths": [arguments.edit_path],
                            "verification_command_ids": verification_ids,
                            "evidence": [
                                "The deterministic verification command covers the change."
                            ],
                        }
                        for criterion_id in criterion_ids
                    ],
                    "remaining_concerns": [],
                    "blocked_reason": None,
                }
            )
        elif arguments.scenario == "revision_cycle" and payload["attempt"] == 1:
            _write_json(
                {
                    "decision": "revision_required",
                    "summary": "The deterministic candidate requires one bounded revision.",
                    "findings": [
                        {
                            "id": "finding_1",
                            "severity": "important",
                            "criterion_id": criterion_ids[0],
                            "path": arguments.edit_path,
                            "problem": "The fixture does not yet contain the reviewed value.",
                            "required_fix": "Replace it with the reviewed fixture value.",
                        }
                    ],
                    "acceptance_coverage": criterion_ids,
                    "residual_risks": [],
                }
            )
        else:
            _write_json(
                {
                    "decision": "approved",
                    "summary": "The deterministic fixture candidate is acceptable.",
                    "findings": [],
                    "acceptance_coverage": criterion_ids,
                    "residual_risks": [],
                }
            )
    except (OSError, ValueError, json.JSONDecodeError):
        sys.stderr.write("fake harness input or fixture operation failed\n")
        return 2
    return 0


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.buffer.read(_MAX_INPUT_BYTES + 1)
    if len(raw) > _MAX_INPUT_BYTES:
        raise ValueError("invocation input is oversized")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("invocation input must be an object")
    return payload


def _validate_payload(payload: dict[str, Any], role: str, phase: str) -> None:
    if set(payload) != {"v", "run_id", "task_id", "role", "phase", "attempt", "prompt"}:
        raise ValueError("invocation input has unknown or missing fields")
    if payload["v"] != 1 or payload["role"] != role or payload["phase"] != phase:
        raise ValueError("invocation input contract does not match")
    if (
        isinstance(payload["attempt"], bool)
        or not isinstance(payload["attempt"], int)
        or not 1 <= payload["attempt"] <= 100
    ):
        raise ValueError("invocation attempt is invalid")
    if not isinstance(payload["prompt"], str) or not payload["prompt"]:
        raise ValueError("invocation prompt is empty")


def _read_length_framed_task(prompt: str) -> dict[str, Any]:
    start_marker = "BEGIN_LENGTH_FRAMED_TASK_JSON bytes="
    end_marker = "\nEND_LENGTH_FRAMED_TASK_JSON\n"
    marker_index = prompt.find(start_marker)
    if marker_index < 0:
        raise ValueError("fake harness prompt omitted its task frame")
    length_end = prompt.find("\n", marker_index)
    if length_end < 0:
        raise ValueError("fake harness prompt has an invalid task frame")
    expected_bytes = int(prompt[marker_index + len(start_marker) : length_end])
    json_start = length_end + 1
    json_end = prompt.find(end_marker, json_start)
    if json_end < 0:
        raise ValueError("fake harness prompt omitted its task frame terminator")
    serialized = prompt[json_start:json_end]
    if len(serialized.encode("utf-8")) != expected_bytes:
        raise ValueError("fake harness prompt task length does not match")
    task = json.loads(serialized)
    if not isinstance(task, dict):
        raise ValueError("fake harness prompt task must be an object")
    return task


def _criterion_ids(task: dict[str, Any]) -> list[str]:
    criteria = task.get("acceptance_criteria")
    if not isinstance(criteria, list) or not criteria:
        raise ValueError("fake harness task requires acceptance criteria")
    ids: list[str] = []
    for item in criteria:
        value = item.get("id") if isinstance(item, dict) else None
        if not isinstance(value, str) or not value:
            raise ValueError("fake harness criteria require stable ids")
        ids.append(value)
    return ids


def _verification_ids(task: dict[str, Any]) -> list[str]:
    values = task.get("verification_command_ids")
    if not isinstance(values, list) or not values:
        raise ValueError("fake harness task requires verification commands")
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("fake harness verification ids are invalid")
    return values


def _implement(edit_path: str, content: str) -> None:
    relative = PurePosixPath(edit_path)
    if (
        not edit_path
        or "\\" in edit_path
        or ":" in edit_path
        or any(unicodedata.category(character).startswith("C") for character in edit_path)
        or relative.is_absolute()
        or any(part in {"", ".", ".."} for part in relative.parts)
    ):
        raise ValueError("fake edit path is unsafe")
    worktree = Path.cwd().resolve(strict=True)
    parent = worktree
    for component in relative.parts[:-1]:
        parent /= component
        if parent.is_symlink():
            raise ValueError("fake edit path traverses a symlink")
        parent.mkdir(exist_ok=True)
        parent.resolve(strict=True).relative_to(worktree)
    target = parent / relative.name
    resolved_target = target.resolve(strict=False)
    resolved_target.relative_to(worktree)
    if resolved_target.exists() and (not resolved_target.is_file() or resolved_target.is_symlink()):
        raise ValueError("fake edit target must be a regular non-symlink file")
    resolved_target.write_text(content, encoding="utf-8")


def _write_json(value: object) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
