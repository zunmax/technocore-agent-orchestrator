"""Strict, secret-free configuration loading."""

from __future__ import annotations

import copy
import hashlib
import ipaddress
import json
import re
import tomllib
import unicodedata
from pathlib import Path, PurePosixPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationError,
    field_validator,
    model_validator,
)

from technocore_orchestrator.domain.models import HarnessKind
from technocore_orchestrator.errors import ConfigurationError

MAX_CONFIG_BYTES = 1 << 20
FULL_SHA_RE = re.compile(r"[0-9a-fA-F]{40}")
IDENTIFIER_RE = re.compile(r"[a-z][a-z0-9_-]{0,63}")
PROVIDER_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?")


class ClosedModel(BaseModel):
    """Pydantic base that rejects drift and prevents in-process mutation."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def _validate_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.fullmatch(value):
        raise ValueError(f"{label} must match {IDENTIFIER_RE.pattern!r}")
    return value


def _validate_relative_path(value: str) -> str:
    if value == ".":
        return value
    if (
        not value
        or "\\" in value
        or ":" in value
        or any(unicodedata.category(character).startswith("C") for character in value)
    ):
        raise ValueError("path must be a canonical slash-separated repository-relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("path must not be absolute or contain empty, '.' or '..' components")
    return path.as_posix()


class RepositoryConfig(ClosedModel):
    path: Path
    base_commit: StrictStr
    allowed_paths: tuple[StrictStr, ...] = (".",)

    @field_validator("base_commit")
    @classmethod
    def validate_base_commit(cls, value: str) -> str:
        if not FULL_SHA_RE.fullmatch(value):
            raise ValueError("base_commit must be a full 40-character Git commit SHA")
        return value.lower()

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("at least one allowed repository path is required")
        normalized = tuple(_validate_relative_path(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_paths contains a duplicate")
        return normalized


class TaskConfig(ClosedModel):
    id: StrictStr
    title: Annotated[StrictStr, Field(min_length=1, max_length=160)]
    brief: Annotated[StrictStr, Field(min_length=1, max_length=20_000)]
    acceptance_criteria: tuple[Annotated[StrictStr, Field(min_length=1, max_length=1_000)], ...]

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, "task id")

    @field_validator("acceptance_criteria")
    @classmethod
    def validate_acceptance_criteria(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("at least one acceptance criterion is required")
        return values


class RoleAssignments(ClosedModel):
    planner: HarnessKind
    implementer: HarnessKind
    reviewer: HarnessKind

    @model_validator(mode="after")
    def validate_provider_set(self) -> RoleAssignments:
        assignments = {self.planner, self.implementer, self.reviewer}
        if HarnessKind.FAKE in assignments:
            if assignments != {HarnessKind.FAKE}:
                raise ValueError("fake roles cannot be mixed with real provider roles")
            return self
        if len(assignments) < 2:
            raise ValueError("real workflows require at least two distinct providers")
        return self


class HarnessProfile(ClosedModel):
    executable: Annotated[StrictStr, Field(min_length=1, max_length=4_096)]
    model: Annotated[StrictStr, Field(min_length=1, max_length=200)]
    expected_version: Annotated[StrictStr, Field(min_length=1, max_length=256)]

    @field_validator("executable", "model", "expected_version")
    @classmethod
    def validate_single_line(cls, value: str) -> str:
        if "\x00" in value or "\r" in value or "\n" in value:
            raise ValueError("harness profile values must be single-line strings")
        return value

    @field_validator("expected_version")
    @classmethod
    def validate_expected_version(cls, value: str) -> str:
        if not PROVIDER_VERSION_RE.fullmatch(value):
            raise ValueError("expected provider version must be an exact semantic version")
        return value


class ClaudeHarnessProfile(HarnessProfile):
    pass


class ProviderProfiles(ClosedModel):
    codex: HarnessProfile | None = None
    claude: ClaudeHarnessProfile | None = None


class ExecutionLimits(ClosedModel):
    run_wall_seconds: Annotated[StrictInt, Field(ge=60, le=86_400)] = 7_200
    invocation_wall_seconds: Annotated[StrictInt, Field(ge=10, le=7_200)] = 1_800
    max_revision_cycles: Annotated[StrictInt, Field(ge=0, le=5)] = 2
    max_model_invocations: Annotated[StrictInt, Field(ge=5, le=100)] = 20
    claude_max_turns: Annotated[StrictInt, Field(ge=1, le=100)] = 20
    max_schema_repairs: Annotated[StrictInt, Field(ge=0, le=2)] = 1
    max_output_bytes: Annotated[StrictInt, Field(ge=4_096, le=10_485_760)] = 1_048_576


class TechnocoreConfig(ClosedModel):
    base_url: StrictStr = "http://127.0.0.1:8080"
    expected_version: Literal["0.8.0"] = "0.8.0"
    expected_commit: Literal["d8775c2c03e4fc96c24022ffa7103cc765ea94fc"] = (
        "d8775c2c03e4fc96c24022ffa7103cc765ea94fc"
    )
    long_poll_seconds: Annotated[StrictInt, Field(ge=1, le=10)] = 10

    @model_validator(mode="after")
    def validate_base_url(self) -> TechnocoreConfig:
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("Technocore base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Technocore base_url must not contain credentials, query or fragment")
        if parsed.path not in {"", "/"}:
            raise ValueError("Technocore base_url must not contain an application path")
        if not _is_literal_loopback(parsed.hostname):
            raise ValueError("Technocore base_url must use a literal loopback IP address")
        return self


def _is_literal_loopback(hostname: str) -> bool:
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


class VerificationCommand(ClosedModel):
    id: StrictStr
    argv: tuple[Annotated[StrictStr, Field(min_length=1, max_length=4_096)], ...]
    timeout_seconds: Annotated[StrictInt, Field(ge=1, le=7_200)] = 600
    required: StrictBool = True

    @field_validator("id")
    @classmethod
    def validate_id(cls, value: str) -> str:
        return _validate_identifier(value, "verification command id")

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("verification command argv must not be empty")
        if any("\x00" in value or "\r" in value or "\n" in value for value in values):
            raise ValueError("verification command arguments must be single-line strings")
        return values


class VerificationConfig(ClosedModel):
    commands: tuple[VerificationCommand, ...]

    @field_validator("commands")
    @classmethod
    def validate_commands(
        cls, values: tuple[VerificationCommand, ...]
    ) -> tuple[VerificationCommand, ...]:
        if not values:
            raise ValueError("at least one verification command is required")
        ids = [command.id for command in values]
        if len(set(ids)) != len(ids):
            raise ValueError("verification command ids must be unique")
        return values


class StorageConfig(ClosedModel):
    root: Path = Path(".local")


class OutputConfig(ClosedModel):
    root: Path = Path("output")


class WorkflowConfig(ClosedModel):
    schema_version: Literal[3]
    repository: RepositoryConfig
    task: TaskConfig
    roles: RoleAssignments
    providers: ProviderProfiles = ProviderProfiles()
    limits: ExecutionLimits = ExecutionLimits()
    technocore: TechnocoreConfig = TechnocoreConfig()
    verification: VerificationConfig
    storage: StorageConfig = StorageConfig()
    output: OutputConfig = OutputConfig()

    @model_validator(mode="after")
    def validate_storage_output_separation(self) -> WorkflowConfig:
        _require_separate_storage_and_output(self.storage.root, self.output.root)
        return self


class PromptTaskConfig(ClosedModel):
    prompt: Annotated[StrictStr, Field(min_length=1, max_length=20_000)]
    allowed_paths: tuple[StrictStr, ...] = (".",)
    acceptance_criteria: tuple[
        Annotated[StrictStr, Field(min_length=1, max_length=1_000)], ...
    ] = ()

    @field_validator("allowed_paths")
    @classmethod
    def validate_allowed_paths(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if not values:
            raise ValueError("at least one allowed repository path is required")
        normalized = tuple(_validate_relative_path(value) for value in values)
        if len(set(normalized)) != len(normalized):
            raise ValueError("allowed_paths contains a duplicate")
        return normalized


class ProfileVerificationConfig(ClosedModel):
    commands: tuple[VerificationCommand, ...] = ()
    include_git_diff_check: StrictBool = True

    @model_validator(mode="after")
    def require_one_verification_source(self) -> ProfileVerificationConfig:
        if not self.commands and not self.include_git_diff_check:
            raise ValueError("profile must enable a built-in check or provide a command")
        ids = [command.id for command in self.commands]
        if len(set(ids)) != len(ids):
            raise ValueError("verification command ids must be unique")
        if self.include_git_diff_check and "git_diff_check" in ids:
            raise ValueError("git_diff_check is reserved for the built-in verification")
        return self


class WorkflowProfile(ClosedModel):
    schema_version: Literal[4]
    task: PromptTaskConfig
    roles: RoleAssignments
    providers: ProviderProfiles
    limits: ExecutionLimits = ExecutionLimits()
    technocore: TechnocoreConfig = TechnocoreConfig()
    verification: ProfileVerificationConfig = ProfileVerificationConfig()
    storage: StorageConfig = StorageConfig()
    output: OutputConfig = OutputConfig()

    @model_validator(mode="after")
    def validate_storage_output_separation(self) -> WorkflowProfile:
        _require_separate_storage_and_output(self.storage.root, self.output.root)
        return self


class ConfigPathRelocation(BaseModel):
    """Verified previous roots carried only long enough to repair durable local paths."""

    model_config = ConfigDict(frozen=True)

    previous_storage_root: Path
    previous_repository_path: Path


class LoadedConfig(BaseModel):
    """Validated config plus provenance needed for reproducible reports."""

    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    config: WorkflowConfig
    source_path: Path
    sha256: StrictStr
    path_relocation: ConfigPathRelocation | None = None


class LoadedProfile(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True, frozen=True)

    profile: WorkflowProfile
    source_path: Path
    sha256: StrictStr


def load_profile(path: Path) -> LoadedProfile:
    """Read one reusable prompt profile for fresh generated codebases."""

    try:
        source = path.resolve(strict=True)
        if not source.is_file():
            raise ConfigurationError("profile path is not a regular file")
        if source.stat().st_size > MAX_CONFIG_BYTES:
            raise ConfigurationError("profile exceeds the 1 MiB limit")
        with source.open("rb") as handle:
            raw = tomllib.load(handle)
    except ConfigurationError:
        raise
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigurationError(
            "unable to read workflow profile", context={"reason": str(exc)}
        ) from exc

    normalized = copy.deepcopy(raw)
    try:
        storage = normalized.setdefault("storage", {})
        output = normalized.setdefault("output", {})
        storage["root"] = str(_resolve_from(source.parent, storage.get("root", ".local")))
        output["root"] = str(_resolve_from(source.parent, output.get("root", "output")))
        profile = WorkflowProfile.model_validate(normalized)
    except (TypeError, ValidationError, ValueError, OSError) as exc:
        raise ConfigurationError(
            "workflow profile validation failed", context={"reason": str(exc)}
        ) from exc

    canonical = _canonical_model_bytes(profile)
    return LoadedProfile(
        profile=profile,
        source_path=source,
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def loaded_config_from_model(config: WorkflowConfig, source_path: Path) -> LoadedConfig:
    canonical = _canonical_model_bytes(config)
    return LoadedConfig(
        config=config,
        source_path=source_path.resolve(),
        sha256=hashlib.sha256(canonical).hexdigest(),
    )


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return json.dumps(
        model.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _resolve_from(base: Path, raw: object, *, must_exist: bool = False) -> Path:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise ValueError("configured path must be a non-empty string")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = base / candidate
    resolved = candidate.resolve(strict=must_exist)
    if must_exist and not resolved.is_dir():
        raise ValueError(f"configured repository path is not a directory: {resolved}")
    return resolved


def _require_separate_storage_and_output(storage: Path, output: Path) -> None:
    storage_root = storage.resolve()
    output_root = output.resolve()
    if (
        storage_root == output_root
        or storage_root.is_relative_to(output_root)
        or output_root.is_relative_to(storage_root)
    ):
        raise ValueError("workflow storage and exported output roots must not overlap")
