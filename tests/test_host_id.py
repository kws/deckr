"""Tests for strict deckr.core.util.host_id resolution."""

from __future__ import annotations

import pytest

from deckr.core.util import host_id as host_id_mod


def test_resolve_host_id_cli_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("HOST_ID", raising=False)
    assert host_id_mod.resolve_host_id(cli_value="from-cli") == "from-cli"


def test_resolve_host_id_env_second(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOST_ID", "from-env")
    assert host_id_mod.resolve_host_id() == "from-env"


def test_resolve_host_id_requires_source_when_no_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HOST_ID", raising=False)
    with pytest.raises(ValueError, match="Host ID"):
        host_id_mod.resolve_host_id()


def test_resolve_controller_id_strict_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CONTROLLER_ID", raising=False)
    with pytest.raises(ValueError, match="Controller ID"):
        host_id_mod.resolve_controller_id()


def test_resolve_controller_id_env_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CONTROLLER_ID", "controller-main")
    assert host_id_mod.resolve_controller_id() == "controller-main"
