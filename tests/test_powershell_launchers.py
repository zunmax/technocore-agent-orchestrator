from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER_TOOLS = PROJECT_ROOT / "scripts" / "launcher-tools.ps1"


def _powershell_literal(path: Path) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def test_native_application_resolver_selects_the_explicit_exe(tmp_path: Path) -> None:
    powershell = shutil.which("pwsh")
    fixture_source = shutil.which("where.exe")
    assert powershell is not None
    assert fixture_source is not None

    fixture_exe = tmp_path / "fixture.exe"
    fixture_extensionless = tmp_path / "fixture"
    shutil.copy2(fixture_source, fixture_exe)
    shutil.copy2(fixture_source, fixture_extensionless)
    environment = os.environ.copy()
    environment["PATH"] = os.pathsep.join((str(tmp_path), environment["PATH"]))
    command = "; ".join(
        (
            f". {_powershell_literal(LAUNCHER_TOOLS)}",
            "$resolved = Resolve-NativeApplication -Name 'fixture'",
            "@{ resolved = $resolved } | ConvertTo-Json -Compress",
        )
    )

    completed = subprocess.run(  # noqa: S603 - fixed local PowerShell executable and arguments
        [powershell, "-NoProfile", "-NonInteractive", "-Command", command],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        env=environment,
        timeout=30,
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert Path(payload["resolved"]).resolve() == fixture_exe.resolve()


def test_launchers_use_the_shared_native_application_resolver() -> None:
    run_launcher = (PROJECT_ROOT / "run-workflow.ps1").read_text(encoding="utf-8")
    viewer_launcher = (PROJECT_ROOT / "open-chat.ps1").read_text(encoding="utf-8")

    assert "Resolve-NativeApplication -Name 'docker'" in run_launcher
    assert "Resolve-NativeApplication -Name 'git'" in run_launcher
    assert "Resolve-NativeApplication -Name 'uv'" in run_launcher
    assert "Resolve-NativeApplication -Name 'uv'" in viewer_launcher
    assert "Get-Command docker" not in run_launcher
