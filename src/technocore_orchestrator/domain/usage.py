"""Validated provider-reported usage facts."""

from __future__ import annotations

from dataclasses import dataclass

_MAX_TOKEN_COUNT = (1 << 63) - 1


@dataclass(frozen=True, slots=True)
class ProviderUsage:
    """Optional facts emitted by a provider, never inferred from missing fields."""

    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_read_input_tokens: int | None = None
    cache_creation_input_tokens: int | None = None
    turns: int | None = None

    def __post_init__(self) -> None:
        counts = {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_input_tokens": self.cache_read_input_tokens,
            "cache_creation_input_tokens": self.cache_creation_input_tokens,
            "turns": self.turns,
        }
        for name, value in counts.items():
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, int)
                or not 0 <= value <= _MAX_TOKEN_COUNT
            ):
                raise ValueError(f"provider {name} must be a non-negative 64-bit integer")
        if all(value is None for value in counts.values()):
            raise ValueError("provider usage must contain at least one reported fact")
