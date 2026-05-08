from __future__ import annotations

from pathlib import Path

import pytest

from deckr_python_runtime.config import (
    load_config_document,
    substitute_config_environment,
)


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
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[action_provider.openhab]
url = "http://example.invalid"
""".strip()
    )

    with pytest.raises(ValueError, match="Use \\[deckr\\.\\*\\] namespaces"):
        load_config_document(config_path)


def test_load_config_document_preserves_namespaced_children(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.components.instances.controller_main]
component = "dev.deckr.controller"
instance_id = "main"

[deckr.components.instances.controller_main.config]
log_level = "debug"

[deckr.actions.providers.openhab]
url = "http://openhab.local:8080"
""".strip()
    )

    document = load_config_document(config_path)

    assert document.source_path == config_path.resolve()
    assert document.base_dir == tmp_path.resolve()
    assert document.namespace("deckr.components.instances.controller_main.config") == {
        "log_level": "debug"
    }
    assert document.namespace("deckr.actions.providers.openhab") == {
        "url": "http://openhab.local:8080"
    }


def test_config_document_resolves_relative_paths(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[deckr]\n")

    document = load_config_document(config_path)

    assert document.resolve_path("settings") == (tmp_path / "settings").resolve()


def test_load_config_document_preserves_env_placeholders_in_bootstrap(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[deckr.runtime.substrate]
url = "nats://${DECKR_HOST}:4222"
""".strip()
    )

    document = load_config_document(config_path)

    assert document.namespace("deckr.runtime.substrate") == {
        "url": "nats://${DECKR_HOST}:4222",
    }


def test_file_config_source_expands_env_placeholders_before_parsing(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    fragment = tmp_path / "runtime.toml"
    config_path.write_text(
        """
[[deckr.config.sources]]
id = "runtime"
source = "dev.deckr.config.files"
paths = ["runtime.toml"]
env_template = true
""".strip()
    )
    fragment.write_text(
        """
[deckr.components.instances.action_runtime]
component = "dev.deckr.action_provider_runtime.python"
instance_id = "main"

[deckr.components.instances.action_runtime.config.runtime]
bind_host = "${DECKR_BIND_HOST:-0.0.0.0}"
bind_port = ${DECKR_BIND_PORT}
provider_ids = ${DECKR_PROVIDER_IDS:-["deckr-plugin-clock"]}
""".strip()
    )

    document = load_config_document(
        config_path,
        env={"DECKR_BIND_PORT": "9000"},
    )

    assert document.namespace(
        "deckr.components.instances.action_runtime.config.runtime"
    ) == {
        "bind_host": "0.0.0.0",
        "bind_port": 9000,
        "provider_ids": ("deckr-plugin-clock",),
    }


def test_file_config_source_expands_process_environment(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "deckr.toml"
    fragment = tmp_path / "runtime.toml"
    config_path.write_text(
        """
[[deckr.config.sources]]
id = "runtime"
source = "dev.deckr.config.files"
paths = ["runtime.toml"]
env_template = true
""".strip()
    )
    fragment.write_text(
        """
[deckr.runtime.substrate]
port = ${DECKR_NATS_PORT}
""".strip()
    )
    monkeypatch.setenv("DECKR_NATS_PORT", "4222")

    document = load_config_document(config_path)

    assert document.namespace("deckr.runtime.substrate") == {"port": 4222}


def test_file_config_source_rejects_missing_env_placeholder(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    fragment = tmp_path / "runtime.toml"
    config_path.write_text(
        """
[[deckr.config.sources]]
id = "runtime"
source = "dev.deckr.config.files"
paths = ["runtime.toml"]
env_template = true
""".strip()
    )
    fragment.write_text(
        """
[deckr.components.instances.action_runtime.config.runtime]
bind_port = ${DECKR_BIND_PORT}
""".strip()
    )

    with pytest.raises(ValueError, match="DECKR_BIND_PORT"):
        load_config_document(config_path, env={})


def test_substitute_config_environment_rejects_invalid_variable_name() -> None:
    with pytest.raises(ValueError, match="Invalid configuration environment"):
        substitute_config_environment("${DECKR-HOST}", {})
