"""Windows kernel byte-range leases for cross-process local coordination."""

from __future__ import annotations

import errno
import msvcrt
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

from technocore_orchestrator.errors import StorageError


@dataclass(slots=True)
class WindowsFileLease:
    handle: BinaryIO

    def close(self) -> None:
        if self.handle.closed:
            return
        try:
            self.handle.seek(0)
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        finally:
            self.handle.close()


def acquire_windows_file_lock(
    path: Path,
    *,
    label: str,
    blocking: bool,
    unavailable_message: str,
) -> WindowsFileLease:
    lease = try_acquire_windows_file_lock(path, label=label, blocking=blocking)
    if lease is None:
        raise StorageError(unavailable_message)
    return lease


def try_acquire_windows_file_lock(
    path: Path,
    *,
    label: str,
    blocking: bool = False,
) -> WindowsFileLease | None:
    if path.is_symlink():
        raise StorageError(f"{label} must not be a symlink")
    descriptor: int | None = None
    handle: BinaryIO | None = None
    try:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise StorageError(f"{label} path is not a regular file")
        handle = os.fdopen(descriptor, "r+b", buffering=0)
        descriptor = None
        if info.st_size < 1:
            handle.write(b"0")
            handle.flush()
            os.fsync(handle.fileno())
        handle.seek(0)
        mode = msvcrt.LK_LOCK if blocking else msvcrt.LK_NBLCK
        try:
            msvcrt.locking(handle.fileno(), mode, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                handle.close()
                return None
            raise StorageError(f"unable to acquire the Windows {label}") from exc
        return WindowsFileLease(handle)
    except StorageError:
        if handle is not None:
            handle.close()
        elif descriptor is not None:
            os.close(descriptor)
        raise
    except OSError as exc:
        if handle is not None:
            handle.close()
        elif descriptor is not None:
            os.close(descriptor)
        raise StorageError(f"unable to open the {label}") from exc
