"""Evidence-based comparison of supervisor-only and Technocore challenge reports."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from technocore_orchestrator.errors import ConfigurationError

_MAX_REPORT_BYTES = 10 * 1024 * 1024


def compare_run_reports(
    mode_a_path: Path,
    mode_b_path: Path,
    *,
    seeded_criteria: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Compare matched run reports without treating message volume as quality."""

    mode_a = _load_report(mode_a_path)
    mode_b = _load_report(mode_b_path)
    _require_matched_tasks(mode_a, mode_b)
    expected_criteria = {
        f"criterion_{index}"
        for index, _description in enumerate(mode_b["task"]["acceptance_criteria"], start=1)
    }
    seeded = set(seeded_criteria)
    if not seeded.issubset(expected_criteria):
        raise ConfigurationError("seeded criterion is not part of the matched task")
    a_metrics = _quality_metrics(mode_a, seeded)
    b_metrics = _quality_metrics(mode_b, seeded)
    verdict = _comparison_verdict(a_metrics, b_metrics, seeded)
    return {
        "comparison_version": 1,
        "matched_task_id": mode_a["run"]["task_id"],
        "base_commit": mode_a["repository"]["base_commit"],
        "seeded_criteria": sorted(seeded),
        "mode_a": a_metrics,
        "mode_b": b_metrics,
        "verdict": verdict,
        "claim_limit": (
            "Message count is excluded from the verdict. Improvement requires better seeded-"
            "criterion detection or criterion coverage without a worse terminal outcome."
        ),
    }


def _load_report(path: Path) -> dict[str, Any]:
    try:
        source = path.resolve(strict=True)
        if not source.is_file() or source.stat().st_size > _MAX_REPORT_BYTES:
            raise ConfigurationError("comparison report must be a regular file up to 10 MiB")
        document = json.loads(source.read_text(encoding="utf-8"))
    except ConfigurationError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("unable to read comparison report") from exc
    if not isinstance(document, dict) or document.get("report_version") != 3:
        raise ConfigurationError("comparison requires Technocore example report version 3")
    for field in ("run", "task", "repository", "roles", "verification", "metrics", "transport"):
        if field not in document:
            raise ConfigurationError("comparison report is missing required evidence")
    try:
        run = document["run"]
        task = document["task"]
        repository = document["repository"]
        roles = document["roles"]
        verification = document["verification"]
        metrics = document["metrics"]
        transport = document["transport"]
        valid = (
            isinstance(run, dict)
            and isinstance(run.get("task_id"), str)
            and isinstance(run.get("outcome"), str)
            and isinstance(task, dict)
            and isinstance(task.get("acceptance_criteria"), list)
            and bool(task["acceptance_criteria"])
            and all(isinstance(value, str) for value in task["acceptance_criteria"])
            and isinstance(repository, dict)
            and isinstance(repository.get("base_commit"), str)
            and len(repository["base_commit"]) == 40
            and isinstance(roles, list)
            and all(
                isinstance(role, dict)
                and isinstance(role.get("attempts"), list)
                and all(isinstance(attempt, dict) for attempt in role["attempts"])
                for role in roles
            )
            and isinstance(verification, dict)
            and verification.get("passed") in {True, False, None}
            and isinstance(metrics, dict)
            and all(
                isinstance(metrics.get(field), int | float)
                and not isinstance(metrics.get(field), bool)
                for field in (
                    "revision_cycles",
                    "invocation_attempts",
                    "invocation_wall_seconds",
                )
            )
            and isinstance(transport, dict)
            and isinstance(transport.get("gaps"), list)
        )
    except (KeyError, TypeError):
        valid = False
    if not valid:
        raise ConfigurationError("comparison report contains invalid evidence types")
    return document


def _require_matched_tasks(mode_a: dict[str, Any], mode_b: dict[str, Any]) -> None:
    comparable = (
        mode_a["run"]["task_id"] == mode_b["run"]["task_id"]
        and mode_a["repository"]["base_commit"] == mode_b["repository"]["base_commit"]
        and mode_a["task"]["acceptance_criteria"] == mode_b["task"]["acceptance_criteria"]
    )
    if not comparable:
        raise ConfigurationError(
            "quality comparison requires the same task, base commit and acceptance criteria"
        )


def _quality_metrics(report: dict[str, Any], seeded: set[str]) -> dict[str, Any]:
    results = tuple(
        attempt["result"]
        for role in report["roles"]
        for attempt in role["attempts"]
        if isinstance(attempt.get("result"), dict)
    )
    challenge_issues = tuple(
        issue
        for result in results
        if "issues" in result and "decision" in result
        for issue in result.get("issues", [])
        if isinstance(issue, dict)
    )
    review_findings = tuple(
        finding
        for result in results
        if "findings" in result
        for finding in result.get("findings", [])
        if isinstance(finding, dict)
    )
    detected = {
        value
        for item in (*challenge_issues, *review_findings)
        if isinstance((value := item.get("criterion_id")), str)
    }
    implementation_coverage = {
        value
        for result in results
        for evidence in result.get("criterion_evidence", [])
        if isinstance(evidence, dict) and isinstance((value := evidence.get("criterion_id")), str)
    }
    review_coverage = {
        value
        for result in results
        for value in result.get("acceptance_coverage", [])
        if isinstance(value, str)
    }
    return {
        "completed": report["run"]["outcome"] == "completed",
        "verification_passed": report["verification"]["passed"],
        "challenge_issues": len(challenge_issues),
        "review_findings": len(review_findings),
        "seeded_criteria_detected": sorted(detected & seeded),
        "implementation_criteria_covered": sorted(implementation_coverage),
        "review_criteria_covered": sorted(review_coverage),
        "revision_cycles": report["metrics"]["revision_cycles"],
        "transport_sequence_gaps": len(report["transport"]["gaps"]),
        "model_invocations": report["metrics"]["invocation_attempts"],
        "invocation_wall_seconds": report["metrics"]["invocation_wall_seconds"],
    }


def _comparison_verdict(mode_a: dict[str, Any], mode_b: dict[str, Any], seeded: set[str]) -> str:
    if mode_a["completed"] and not mode_b["completed"]:
        return "regressed"
    if seeded and len(mode_b["seeded_criteria_detected"]) > len(mode_a["seeded_criteria_detected"]):
        return "improved"
    b_coverage = len(mode_b["implementation_criteria_covered"]) + len(
        mode_b["review_criteria_covered"]
    )
    a_coverage = len(mode_a["implementation_criteria_covered"]) + len(
        mode_a["review_criteria_covered"]
    )
    if b_coverage > a_coverage and mode_b["completed"] >= mode_a["completed"]:
        return "improved"
    return "inconclusive"
