"""Tests for deckr.core.util.host_id resolution."""

from __future__ import annotations

import uuid

import pytest
from deckr.core.util import host_id as host_id_mod


def test_resolve_host_id_cli_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOST_ID", raising=False)
    assert (
        host_id_mod.resolve_host_id(
            cli_value="from-cli",
            fallback_to_hostname=False,
            fallback_to_uuid=False,
        )
        == "from-cli"
    )


def test_resolve_host_id_env_second(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_ID", "from-env")
    assert (
        host_id_mod.resolve_host_id(
            fallback_to_hostname=False,
            fallback_to_uuid=False,
        )
        == "from-env"
    )


def test_resolve_host_id_hostname_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOST_ID", raising=False)
    monkeypatch.setattr(host_id_mod.socket, "gethostname", lambda: "box.local")
    assert (
        host_id_mod.resolve_host_id(
            fallback_to_hostname=True,
            fallback_to_uuid=False,
        )
        == "box.local"
    )


def test_resolve_host_id_requires_source_when_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HOST_ID", raising=False)
    with pytest.raises(ValueError, match="Host ID"):
        host_id_mod.resolve_host_id(
            fallback_to_hostname=False,
            fallback_to_uuid=False,
        )


def test_resolve_controller_id_uuid_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTROLLER_ID", raising=False)
    fixed = uuid.UUID("12345678-1234-5678-1234-567812345678")
    monkeypatch.setattr(host_id_mod.uuid, "uuid4", lambda: fixed)
    assert (
        host_id_mod.resolve_controller_id(
            fallback_to_uuid=True,
        )
        == str(fixed)
    )


def test_resolve_controller_id_strict_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTROLLER_ID", raising=False)
    with pytest.raises(ValueError, match="Controller ID"):
        host_id_mod.resolve_controller_id(
            fallback_to_uuid=False,
        )
