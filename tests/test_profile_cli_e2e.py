from __future__ import annotations

import json
from pathlib import Path

from pytest import CaptureFixture

from technocore_orchestrator import cli
from technocore_orchestrator.cli import main


def test_reusable_profile_generates_a_fresh_project_and_exports_output(
    tmp_path: Path, capsys: CaptureFixture[str]
) -> None:
    profile = tmp_path / "workflow.toml"
    profile.write_text(
        f"""\
schema_version = 4

[task]
prompt = "Implement the requested product change."

[roles]
planner = "fake"
implementer = "fake"
reviewer = "fake"

[providers]

[storage]
root = '{(tmp_path / "state").as_posix()}'

[output]
root = '{(tmp_path / "output").as_posix()}'
""",
        encoding="utf-8",
    )

    exit_code = main(
        [
            "run",
            "--config",
            str(profile),
            "--run-id",
            "run_profilee2e",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    output = Path(payload["output_directory"])
    assert output.is_dir()
    assert output.name.startswith("task_")
    assert output.name.endswith("__run_profilee2e")
    assert (output / "code" / "product.txt").read_text(encoding="utf-8") == (
        "feature from fake harness\n"
    )
    assert len(tuple((output / "agent-outputs").glob("*.json"))) == 5
    assert (output / "reports" / "conversation.jsonl").is_file()
    resolved_path = tmp_path / "state" / "resolved-configs" / "run_profilee2e.json"
    resolved = json.loads(resolved_path.read_text(encoding="utf-8"))
    repository = Path(resolved["config"]["repository"]["path"])
    assert repository == tmp_path / "state" / "generated-projects" / "run_profilee2e" / "source"
    assert {path.name for path in repository.iterdir()} == {".git"}

    status_exit = main(["status", "run_profilee2e", "--config", str(profile), "--json"])
    assert status_exit == 0
    assert json.loads(capsys.readouterr().out)["state"] == "completed"


def test_cli_converts_unexpected_failures_to_a_stable_non_secret_error(
    monkeypatch, capsys: CaptureFixture[str]
) -> None:
    def fail(_config, *, as_json: bool) -> int:
        del as_json
        raise RuntimeError("sensitive internal detail")

    monkeypatch.setattr(cli, "_doctor", fail)

    assert main(["doctor"]) == 70
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "error[internal]: unexpected internal failure\n"
    assert "sensitive" not in captured.err
