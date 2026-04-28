from __future__ import annotations

from pathlib import Path

import pytest

from deckr.core.config import load_config_document, substitute_config_environment


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


def test_load_config_document_preserves_env_placeholders_by_default(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.substrates.nats.instances.controller]
url = "nats://${DECKR_HOST}:4222"
""".strip()
    )

    document = load_config_document(config_path)

    assert document.namespace("deckr.substrates.nats.instances.controller") == {
        "url": "nats://${DECKR_HOST}:4222",
    }


def test_load_config_document_expands_env_placeholders_before_parsing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.plugin_hosts.python.instances.main.runtime]
bind_host = "${DECKR_BIND_HOST:-0.0.0.0}"
bind_port = ${DECKR_BIND_PORT}
plugin_ids = ${DECKR_PLUGIN_IDS:-["deckr-plugin-clock"]}
""".strip()
    )

    document = load_config_document(
        config_path,
        expand_env=True,
        env={"DECKR_BIND_PORT": "9000"},
    )

    assert document.namespace("deckr.plugin_hosts.python.instances.main.runtime") == {
        "bind_host": "0.0.0.0",
        "bind_port": 9000,
        "plugin_ids": ("deckr-plugin-clock",),
    }


def test_load_config_document_expands_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.substrates.nats.instances.controller]
port = ${DECKR_NATS_PORT}
""".strip()
    )
    monkeypatch.setenv("DECKR_NATS_PORT", "4222")

    document = load_config_document(config_path, expand_env=True)

    assert document.namespace("deckr.substrates.nats.instances.controller") == {
        "port": 4222
    }


def test_load_config_document_rejects_missing_env_placeholder(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.plugin_hosts.python.instances.main.runtime]
bind_port = ${DECKR_BIND_PORT}
""".strip()
    )

    with pytest.raises(ValueError, match="DECKR_BIND_PORT"):
        load_config_document(config_path, expand_env=True, env={})


def test_substitute_config_environment_rejects_invalid_variable_name() -> None:
    with pytest.raises(ValueError, match="Invalid configuration environment"):
        substitute_config_environment("${DECKR-HOST}", {})
