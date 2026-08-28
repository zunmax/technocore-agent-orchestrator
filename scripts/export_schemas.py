"""Export or verify the checked-in protocol JSON Schemas."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from technocore_orchestrator.domain.collaboration import PlanChallengeResult
from technocore_orchestrator.domain.models import (
    EventEnvelope,
    ImplementerResult,
    PlannerResult,
    ReviewerResult,
)

SCHEMAS = {
    "event.schema.json": EventEnvelope,
    "implementer-result.schema.json": ImplementerResult,
    "planner-result.schema.json": PlannerResult,
    "plan-challenge-result.schema.json": PlanChallengeResult,
    "reviewer-result.schema.json": ReviewerResult,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output_dir = (
        Path(__file__).resolve().parents[1] / "src" / "technocore_orchestrator" / "schemas" / "v1"
    )
    mismatches: list[str] = []
    for filename, model in SCHEMAS.items():
        rendered = json.dumps(model.model_json_schema(), indent=2, sort_keys=True) + "\n"
        target = output_dir / filename
        if args.check:
            if not target.is_file() or target.read_text(encoding="utf-8") != rendered:
                mismatches.append(filename)
        else:
            target.write_text(rendered, encoding="utf-8", newline="\n")
    if mismatches:
        parser.error("schema files are stale or missing: " + ", ".join(mismatches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
