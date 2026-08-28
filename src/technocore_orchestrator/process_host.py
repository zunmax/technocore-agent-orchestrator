"""Trusted launch gate that starts provider processes only after Job Object assignment."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

_PROTOCOL_VERSION = 1
_HEADER_LENGTH_BYTES = 4
_MAX_HEADER_BYTES = 1 << 20
_MAX_STDIN_BYTES = 4 << 20
_MAX_ARGUMENTS = 4096
_HOST_FAILURE_EXIT_CODE = 253


def main() -> int:
    try:
        header_length = int.from_bytes(_read_exact(_HEADER_LENGTH_BYTES), "big")
        if not 1 <= header_length <= _MAX_HEADER_BYTES:
            raise ValueError("launch header length is invalid")
        document = json.loads(_read_exact(header_length))
        executable, arguments, cwd, stdin_length = _validate_header(document)
        provider_input = _read_exact(stdin_length)
        if sys.stdin.buffer.read(1):
            raise ValueError("launch frame has trailing data")
        _verify_executable(document, executable)
        options: dict[str, Any] = {
            "cwd": cwd,
            "stdin": subprocess.DEVNULL,
            "stdout": None,
            "stderr": None,
            "shell": False,
            "close_fds": True,
        }
        if stdin_length:
            options.pop("stdin")
            options["input"] = provider_input
        completed = subprocess.run([str(executable), *arguments], **options)  # noqa: S603
        return int(completed.returncode)
    except (OSError, UnicodeError, ValueError, json.JSONDecodeError, subprocess.SubprocessError):
        print("trusted process host rejected the provider launch", file=sys.stderr, flush=True)
        return _HOST_FAILURE_EXIT_CODE


def _read_exact(length: int) -> bytes:
    value = sys.stdin.buffer.read(length)
    if len(value) != length:
        raise ValueError("launch frame ended early")
    return value


def _validate_header(document: object) -> tuple[Path, tuple[str, ...], Path, int]:
    if not isinstance(document, dict) or set(document) != {
        "version",
        "executable",
        "resolved_executable",
        "executable_sha256",
        "executable_size_bytes",
        "arguments",
        "cwd",
        "stdin_length",
    }:
        raise ValueError("launch header fields are invalid")
    if document["version"] != _PROTOCOL_VERSION:
        raise ValueError("launch protocol version is invalid")
    executable = _absolute_path(document["executable"])
    cwd = _absolute_path(document["cwd"])
    arguments_value = document["arguments"]
    if (
        not isinstance(arguments_value, list)
        or len(arguments_value) > _MAX_ARGUMENTS
        or any(not isinstance(value, str) or "\0" in value for value in arguments_value)
    ):
        raise ValueError("launch arguments are invalid")
    stdin_length = document["stdin_length"]
    if (
        isinstance(stdin_length, bool)
        or not isinstance(stdin_length, int)
        or not 0 <= stdin_length <= _MAX_STDIN_BYTES
    ):
        raise ValueError("launch stdin length is invalid")
    return executable, tuple(arguments_value), cwd, stdin_length


def _verify_executable(document: dict[str, object], executable: Path) -> None:
    resolved_value = document["resolved_executable"]
    digest_value = document["executable_sha256"]
    size_value = document["executable_size_bytes"]
    if (
        not isinstance(resolved_value, str)
        or not isinstance(digest_value, str)
        or isinstance(size_value, bool)
        or not isinstance(size_value, int)
    ):
        raise ValueError("launch executable identity is invalid")
    resolved = executable.resolve(strict=True)
    if not resolved.is_file() or str(resolved) != resolved_value:
        raise ValueError("launch executable target changed")
    info = resolved.stat()
    if info.st_size != size_value or _sha256_file(resolved) != digest_value:
        raise ValueError("launch executable content changed")


def _absolute_path(value: object) -> Path:
    if not isinstance(value, str) or not value or "\0" in value:
        raise ValueError("launch path is invalid")
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("launch path must be absolute")
    return path


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    raise SystemExit(main())
