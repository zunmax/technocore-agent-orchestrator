from __future__ import annotations

import json

import pytest

from technocore_orchestrator.errors import ConfigurationError
from technocore_orchestrator.evaluation import compare_run_reports


def _report(*, task_id: str = "task", challenge: bool, coverage: bool) -> dict[str, object]:
    results: list[dict[str, object]] = []
    if challenge:
        results.append(
            {
                "decision": "changes_requested",
                "summary": "The seeded edge case is missing.",
                "issues": [
                    {
                        "id": "seeded_case",
                        "criterion_id": "criterion_1",
                        "severity": "important",
                        "rationale": "The seeded case is not covered.",
                        "recommendation": "Add the seeded case.",
                    }
                ],
            }
        )
    if coverage:
        results.append(
            {
                "outcome": "changes_ready",
                "criterion_evidence": [{"criterion_id": "criterion_1"}],
            }
        )
        results.append(
            {
                "decision": "approved",
                "findings": [],
                "acceptance_coverage": ["criterion_1"],
            }
        )
    return {
        "report_version": 3,
        "run": {"task_id": task_id, "outcome": "completed"},
        "task": {"acceptance_criteria": ["Detect the seeded case."]},
        "repository": {"base_commit": "a" * 40},
        "roles": [
            {"attempts": [{"result": result, "result_type": "fixture"} for result in results]}
        ],
        "verification": {"passed": True},
        "metrics": {
            "revision_cycles": 0,
            "invocation_attempts": len(results),
            "invocation_wall_seconds": 1.0,
        },
        "transport": {"gaps": [], "collaboration_messages": 500 if challenge else 0},
    }


def test_comparison_requires_quality_evidence_not_message_volume(tmp_path) -> None:
    mode_a = tmp_path / "mode-a.json"
    mode_b = tmp_path / "mode-b.json"
    mode_a.write_text(json.dumps(_report(challenge=False, coverage=False)), encoding="utf-8")
    mode_b.write_text(json.dumps(_report(challenge=True, coverage=True)), encoding="utf-8")

    result = compare_run_reports(
        mode_a,
        mode_b,
        seeded_criteria=("criterion_1",),
    )

    assert result["verdict"] == "improved"
    assert result["mode_b"]["seeded_criteria_detected"] == ["criterion_1"]
    assert "Message count is excluded" in result["claim_limit"]


def test_comparison_rejects_unmatched_tasks(tmp_path) -> None:
    mode_a = tmp_path / "mode-a.json"
    mode_b = tmp_path / "mode-b.json"
    mode_a.write_text(json.dumps(_report(challenge=False, coverage=False)), encoding="utf-8")
    mode_b.write_text(
        json.dumps(_report(task_id="different", challenge=True, coverage=True)),
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="same task"):
        compare_run_reports(mode_a, mode_b)
