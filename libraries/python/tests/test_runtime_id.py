"""Tests for generic deckr.core.util.runtime_id normalization."""

from __future__ import annotations

import pytest

from deckr.core.util import runtime_id as runtime_id_mod


def test_normalize_runtime_id_preserves_valid_identifier() -> None:
    assert runtime_id_mod.normalize_runtime_id("controller-main") == "controller-main"


def test_normalize_runtime_id_rewrites_invalid_characters() -> None:
    assert runtime_id_mod.normalize_runtime_id(" controller::main / test ") == (
        "controller-main-test"
    )


def test_require_runtime_id_accepts_normalized_value() -> None:
    assert runtime_id_mod.require_runtime_id(
        " host::main ",
        label="Host ID",
        source_hint="Set it somewhere explicit.",
    ) == "host-main"


def test_require_runtime_id_rejects_missing_value() -> None:
    with pytest.raises(ValueError, match="Host ID is required"):
        runtime_id_mod.require_runtime_id(
            None,
            label="Host ID",
            source_hint="Set it somewhere explicit.",
        )
