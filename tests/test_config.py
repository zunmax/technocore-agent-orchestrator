from __future__ import annotations

import pytest
from pydantic import ValidationError

from technocore_orchestrator.config import TechnocoreConfig, load_profile
from technocore_orchestrator.errors import ConfigurationError


@pytest.mark.parametrize(
    "base_url",
    (
        "http://127.0.0.1:8080",
        "https://127.0.0.2:8443",
        "http://[::1]:8080",
    ),
)
def test_technocore_accepts_only_literal_loopback_addresses(base_url: str) -> None:
    assert TechnocoreConfig(base_url=base_url).base_url == base_url


@pytest.mark.parametrize(
    "base_url",
    (
        "http://localhost:8080",
        "http://0.0.0.0:8080",
        "http://192.168.1.10:8080",
        "https://technocore.example:443",
    ),
)
def test_technocore_rejects_hostnames_and_non_loopback_addresses(base_url: str) -> None:
    with pytest.raises(ValidationError, match="literal loopback IP address"):
        TechnocoreConfig(base_url=base_url)


def test_removed_remote_escape_hatch_is_rejected() -> None:
    with pytest.raises(ValidationError, match="allow_remote"):
        TechnocoreConfig.model_validate(
            {"base_url": "http://127.0.0.1:8080", "allow_remote": False}
        )


def test_profile_rejects_overlapping_storage_and_output_roots(tmp_path) -> None:
    profile = tmp_path / "workflow.toml"
    profile.write_text(
        """\
schema_version = 4
[task]
prompt = "Build the requested project."
[roles]
planner = "fake"
implementer = "fake"
reviewer = "fake"
[providers]
[storage]
root = "state"
[output]
root = "state/output"
""",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="workflow profile validation failed") as failure:
        load_profile(profile)
    assert "must not overlap" in str(failure.value.context["reason"])
