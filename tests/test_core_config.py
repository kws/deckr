from __future__ import annotations

from pathlib import Path

import pytest

from deckr.core.config import (
    ConfigDocument,
    load_config_document,
    substitute_config_environment,
)


def test_load_config_document_requires_deckr_namespace(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text("[controller]\nlog_level = 'info'\n")

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


def test_load_config_document_uses_default_empty_document(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    document = load_config_document(None)

    assert document.raw == {"deckr": {}}
    assert document.source_path is None
    assert document.base_dir == tmp_path
    assert document.deckr == {}


def test_load_config_document_accepts_default_text(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.chdir(tmp_path)

    document = load_config_document(
        None,
        default_text="[deckr.runtime]\nname = 'default'\n",
    )

    assert document.namespace("deckr.runtime") == {"name": "default"}
    assert document.source_path is None


@pytest.mark.parametrize(
    "source_block,match",
    (
        ("sources = 'runtime.toml'", "array of tables"),
        ("sources = [1]", "sources\\[0\\] must be a table"),
    ),
)
def test_load_config_document_rejects_invalid_config_source_shape(
    tmp_path: Path,
    source_block: str,
    match: str,
) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(f"[deckr.config]\n{source_block}\n")

    with pytest.raises(ValueError, match=match):
        load_config_document(config_path)


def test_file_config_source_rejects_missing_relative_path(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[[deckr.config.sources]]
id = "runtime"
source = "dev.deckr.config.files"
paths = ["missing/*.toml"]
""".strip()
    )

    with pytest.raises(ValueError, match="path pattern matched nothing"):
        load_config_document(config_path)


def test_file_config_source_rejects_invalid_env_defaults(tmp_path: Path) -> None:
    config_path = tmp_path / "deckr.toml"
    config_path.write_text(
        """
[[deckr.config.sources]]
id = "runtime"
source = "dev.deckr.config.files"
paths = ["runtime.toml"]
env_defaults = []
""".strip()
    )
    (tmp_path / "runtime.toml").write_text("[deckr]\n")

    with pytest.raises(ValueError, match="env_defaults must be a table"):
        load_config_document(config_path)


def test_substitute_config_environment_rejects_malformed_placeholder() -> None:
    with pytest.raises(ValueError, match="Invalid configuration environment"):
        substitute_config_environment("${DECKR_HOST:bad}", {})


def test_config_document_namespace_and_children_branches(tmp_path: Path) -> None:
    document = ConfigDocument(
        raw={
            "deckr": {
                "components": {
                    "instances": {
                        "controller": {"component": "dev.deckr.controller"},
                        "disabled": "not-a-table",
                    }
                },
                "runtime": "not-a-table",
            }
        },
        source_path=None,
        base_dir=tmp_path,
    )

    assert document.namespace("") == document.raw
    assert document.namespace("deckr.runtime.name") is None
    assert document.namespace("deckr.missing") is None
    assert document.children("deckr.components.instances") == {
        "controller": {"component": "dev.deckr.controller"}
    }
    assert document.children("deckr.runtime") == {}
    assert document.resolve_path("/tmp/deckr") == Path("/tmp/deckr")
