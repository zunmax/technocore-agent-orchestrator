"""Stable public error categories for the control plane."""

from __future__ import annotations

from enum import IntEnum, StrEnum
from typing import Any


class ExitCode(IntEnum):
    """Process exit codes exposed by the CLI."""

    SUCCESS = 0
    CONFIGURATION = 2
    PREFLIGHT = 3
    PROTOCOL = 4
    STATE = 5
    STORAGE = 6
    IDENTITY = 7
    TRANSPORT = 8
    EXECUTION = 9
    INTERNAL = 70


class ErrorCategory(StrEnum):
    """Machine-readable categories retained in run records and reports."""

    CONFIGURATION = "configuration"
    PREFLIGHT = "preflight"
    PROTOCOL = "protocol"
    STATE = "state"
    STORAGE = "storage"
    IDENTITY = "identity"
    TRANSPORT = "transport"
    EXECUTION = "execution"
    INTERNAL = "internal"


_CATEGORY_EXIT_CODES = {
    ErrorCategory.CONFIGURATION: ExitCode.CONFIGURATION,
    ErrorCategory.PREFLIGHT: ExitCode.PREFLIGHT,
    ErrorCategory.PROTOCOL: ExitCode.PROTOCOL,
    ErrorCategory.STATE: ExitCode.STATE,
    ErrorCategory.STORAGE: ExitCode.STORAGE,
    ErrorCategory.IDENTITY: ExitCode.IDENTITY,
    ErrorCategory.TRANSPORT: ExitCode.TRANSPORT,
    ErrorCategory.EXECUTION: ExitCode.EXECUTION,
    ErrorCategory.INTERNAL: ExitCode.INTERNAL,
}


class WorkflowError(Exception):
    """Base error with a safe user message and non-secret diagnostic context."""

    def __init__(
        self,
        category: ErrorCategory,
        message: str,
        *,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.message = message
        self.context = context or {}

    @property
    def exit_code(self) -> ExitCode:
        return _CATEGORY_EXIT_CODES[self.category]


class ConfigurationError(WorkflowError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.CONFIGURATION, message, context=context)


class PreflightError(WorkflowError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.PREFLIGHT, message, context=context)


class ProtocolError(WorkflowError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.PROTOCOL, message, context=context)


class RoleResultValidationError(ProtocolError):
    """A provider response can receive only the configured bounded schema repair."""


class StateError(WorkflowError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.STATE, message, context=context)


class StorageError(WorkflowError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.STORAGE, message, context=context)


class IdentityError(WorkflowError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.IDENTITY, message, context=context)


class TransportError(WorkflowError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.TRANSPORT, message, context=context)


class ExecutionError(WorkflowError):
    def __init__(self, message: str, *, context: dict[str, Any] | None = None) -> None:
        super().__init__(ErrorCategory.EXECUTION, message, context=context)
