"""Tests for generic deckr.core.util.runtime_id normalization."""

from __future__ import annotations

import pytest

from deckr.core.util import runtime_id as runtime_id_mod


def test_require_runtime_id_rejects_missing_value() -> None:
    with pytest.raises(ValueError, match="Host ID is required"):
        runtime_id_mod.require_runtime_id(
            None,
            label="Host ID",
            source_hint="Set it somewhere explicit.",
        )
