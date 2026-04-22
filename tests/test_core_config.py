from __future__ import annotations

from pathlib import Path

import pytest

from deckr.core.config import load_config_document


def test_load_config_document_requires_deckr_namespace(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[controller]\nlog_level = 'info'\n")

    with pytest.raises(ValueError, match="Use \\[deckr\\.\\*\\] namespaces"):
        load_config_document(config_path)


def test_load_config_document_rejects_non_deckr_top_level_namespaces(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.controller]
log_level = "info"

[plugin.openhab]
url = "http://example.invalid"
""".strip()
    )

    with pytest.raises(ValueError, match="Use \\[deckr\\.\\*\\] namespaces"):
        load_config_document(config_path)


def test_load_config_document_preserves_namespaced_children(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.controller]
log_level = "debug"

[deckr.plugin_hosts.python.instances.main]
enabled = false

[deckr.plugins.openhab]
url = "http://openhab.local:8080"
""".strip()
    )

    document = load_config_document(config_path)

    assert document.source_path == config_path.resolve()
    assert document.base_dir == tmp_path.resolve()
    assert document.namespace("deckr.controller") == {"log_level": "debug"}
    assert document.children("deckr.plugin_hosts") == {
        "python": {"instances": {"main": {"enabled": False}}}
    }
    assert document.namespace("deckr.plugins.openhab") == {
        "url": "http://openhab.local:8080"
    }


def test_config_document_resolves_relative_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[deckr.controller]\n")

    document = load_config_document(config_path)

    assert document.resolve_path("settings") == (tmp_path / "settings").resolve()
