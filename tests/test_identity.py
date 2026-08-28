from __future__ import annotations

from pathlib import Path

import pytest

from technocore_orchestrator.errors import IdentityError
from technocore_orchestrator.identity import (
    PROTECTED_IDENTITY_HEADER,
    create_protected_identity,
    load_protected_identity,
)


def test_protected_identity_round_trips_without_persisting_plaintext_key_material(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "technocore_orchestrator.identity.secrets.token_bytes", lambda size: b"\xaa" * size
    )
    path = tmp_path / "planner.identity.dpapi"

    public = create_protected_identity(path)
    content = path.read_text(encoding="ascii")

    assert content.startswith(PROTECTED_IDENTITY_HEADER + "\n")
    assert "aa" * 32 not in content.casefold()
    assert load_protected_identity(path).public == public


def test_protected_identity_never_overwrites_an_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "implementer.identity.dpapi"
    path.write_text("existing\n", encoding="ascii")

    with pytest.raises(IdentityError, match="never overwritten"):
        create_protected_identity(path)

    assert path.read_text(encoding="ascii") == "existing\n"


def test_protected_identity_loader_refuses_a_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target.identity.dpapi"
    create_protected_identity(target)
    link = tmp_path / "planner.identity.dpapi"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("creating symlinks is unavailable")

    with pytest.raises(IdentityError, match="symlink"):
        load_protected_identity(link)


def test_protected_identity_rejects_ciphertext_tampering(tmp_path: Path) -> None:
    path = tmp_path / "reviewer.identity.dpapi"
    create_protected_identity(path)
    content = path.read_text(encoding="ascii")
    replacement = "A" if content[-3] != "A" else "B"
    path.write_text(content[:-3] + replacement + content[-2:], encoding="ascii")

    with pytest.raises(IdentityError, match="DPAPI"):
        load_protected_identity(path)
