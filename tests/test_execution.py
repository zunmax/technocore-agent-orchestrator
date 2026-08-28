from __future__ import annotations

import asyncio
import ctypes
import os
import sys
from ctypes import wintypes
from pathlib import Path

from technocore_orchestrator.domain.models import TerminationReason
from technocore_orchestrator.execution import ProcessRunner, ProcessSpec, TrustedExecutable

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


def _environment() -> tuple[tuple[str, str], ...]:
    system_root = os.environ["SYSTEMROOT"]
    return (("PATH", str(Path(system_root) / "System32")), ("SYSTEMROOT", system_root))


def _python_spec(*arguments: str, timeout: float, output_limit: int) -> ProcessSpec:
    return ProcessSpec(
        executable=TrustedExecutable.capture(Path(sys.executable)),
        arguments=("-I", *arguments),
        cwd=Path.cwd().resolve(),
        environment=_environment(),
        timeout_seconds=timeout,
        output_limit_bytes=output_limit,
        termination_grace_seconds=0.2,
    )


def _is_process_running(pid: int) -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def test_timeout_terminates_the_descendant_process_tree() -> None:
    script = (
        "import subprocess,sys,time; "
        "child=subprocess.Popen([sys.executable,'-I','-c','import time; time.sleep(30)']); "
        "print(child.pid,flush=True); time.sleep(30)"
    )

    result = asyncio.run(
        ProcessRunner().run(_python_spec("-c", script, timeout=0.5, output_limit=4_096))
    )

    assert result.termination_reason is TerminationReason.TIMED_OUT
    descendant_pid = int(result.stdout.strip())
    assert not _is_process_running(descendant_pid)


def test_output_limit_terminates_the_process() -> None:
    result = asyncio.run(
        ProcessRunner().run(
            _python_spec(
                "-c",
                "import sys,time; sys.stdout.write('x'*100000); sys.stdout.flush(); time.sleep(30)",
                timeout=5,
                output_limit=4_096,
            )
        )
    )

    assert result.termination_reason is TerminationReason.OUTPUT_LIMIT_EXCEEDED
    assert len(result.stdout) == 4_096


def test_provider_starts_inside_the_supervisors_job_object() -> None:
    script = (
        "import ctypes; from ctypes import wintypes; "
        "k=ctypes.WinDLL('kernel32',use_last_error=True); "
        "k.GetCurrentProcess.restype=wintypes.HANDLE; "
        "k.IsProcessInJob.argtypes=(wintypes.HANDLE,wintypes.HANDLE,ctypes.POINTER(wintypes.BOOL)); "
        "inside=wintypes.BOOL(); "
        "ok=k.IsProcessInJob(k.GetCurrentProcess(),None,ctypes.byref(inside)); "
        "print(f'{int(ok)}:{int(inside.value)}')"
    )

    result = asyncio.run(
        ProcessRunner().run(_python_spec("-c", script, timeout=5, output_limit=4_096))
    )

    assert result.termination_reason is TerminationReason.EXITED
    assert result.returncode == 0
    assert result.stdout.strip() == b"1:1"
