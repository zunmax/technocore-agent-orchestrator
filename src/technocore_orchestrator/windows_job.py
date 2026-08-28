"""Windows Job Object ownership for one subprocess tree."""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from typing import Any

from technocore_orchestrator.errors import ExecutionError

_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
_PROCESS_TERMINATE = 0x0001
_PROCESS_SET_QUOTA = 0x0100
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259


class _JobObjectBasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_int64),
        ("PerJobUserTimeLimit", ctypes.c_int64),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class _IoCounters(ctypes.Structure):
    _fields_ = [
        ("ReadOperationCount", ctypes.c_uint64),
        ("WriteOperationCount", ctypes.c_uint64),
        ("OtherOperationCount", ctypes.c_uint64),
        ("ReadTransferCount", ctypes.c_uint64),
        ("WriteTransferCount", ctypes.c_uint64),
        ("OtherTransferCount", ctypes.c_uint64),
    ]


class _JobObjectExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _JobObjectBasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


@dataclass(slots=True)
class WindowsJob:
    """Own a Windows process tree until explicitly closed."""

    _handle: int | None

    @classmethod
    def create_for_process(cls, pid: int) -> WindowsJob:
        if isinstance(pid, bool) or not isinstance(pid, int) or pid < 1:
            raise ValueError("Windows process ID must be a positive integer")

        kernel32 = _kernel32()
        job_handle = kernel32.CreateJobObjectW(None, None)
        if not job_handle:
            _raise_windows_error("create a Windows Job Object")
        job = cls(int(job_handle))
        try:
            limits = _JobObjectExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            configured = kernel32.SetInformationJobObject(
                job._handle,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            )
            if not configured:
                _raise_windows_error("configure Windows process-tree ownership")

            process_handle = kernel32.OpenProcess(
                _PROCESS_TERMINATE | _PROCESS_SET_QUOTA | _PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid,
            )
            if not process_handle:
                _raise_windows_error("open the spawned Windows process")
            try:
                if not kernel32.AssignProcessToJobObject(job._handle, process_handle):
                    if _process_has_exited(kernel32, process_handle):
                        job.close()
                        return job
                    _raise_windows_error("assign the spawned process to its Windows Job Object")
            finally:
                kernel32.CloseHandle(process_handle)
        except Exception:
            job.close()
            raise
        return job

    def terminate(self) -> None:
        if self._handle is None:
            return
        if not _kernel32().TerminateJobObject(self._handle, 1):
            _raise_windows_error("terminate the Windows process tree")

    def close(self) -> None:
        handle = self._handle
        self._handle = None
        if handle is not None and not _kernel32().CloseHandle(handle):
            _raise_windows_error("close the Windows Job Object")


def _kernel32() -> Any:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.SetInformationJobObject.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    )
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    return kernel32


def _process_has_exited(kernel32: Any, process_handle: int) -> bool:
    exit_code = wintypes.DWORD()
    if not kernel32.GetExitCodeProcess(process_handle, ctypes.byref(exit_code)):
        _raise_windows_error("query the spawned Windows process status")
    return exit_code.value != _STILL_ACTIVE


def _raise_windows_error(action: str) -> None:
    error = ctypes.get_last_error()
    raise ExecutionError(
        f"unable to {action}",
        context={"winerror": error, "reason": ctypes.FormatError(error).strip()},
    )
