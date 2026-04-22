from __future__ import annotations

from types import MappingProxyType

import pytest

from deckr.plugin.config import (
    _install_plugin_config,
    get_config_value,
    get_plugin_config,
)


def teardown_function() -> None:
    _install_plugin_config(None)


def test_get_plugin_config_defaults_to_empty_mapping() -> None:
    config = get_plugin_config()

    assert isinstance(config, MappingProxyType)
    assert dict(config) == {}


def test_install_plugin_config_freezes_nested_values() -> None:
    _install_plugin_config(
        {
            "url": "http://openhab.local:8080",
            "scenes": [{"name": "Bright"}, {"name": "Dim"}],
            "auth": {"token": "secret"},
        }
    )

    config = get_plugin_config()

    assert config["url"] == "http://openhab.local:8080"
    assert isinstance(config["auth"], MappingProxyType)
    assert config["scenes"] == (MappingProxyType({"name": "Bright"}), MappingProxyType({"name": "Dim"}))
    with pytest.raises(TypeError):
        config["url"] = "http://mutated"


def test_get_config_value_resolves_dotted_paths() -> None:
    _install_plugin_config(
        {
            "url": "http://openhab.local:8080",
            "auth": {"api_key": "secret"},
        }
    )

    assert get_config_value("url") == "http://openhab.local:8080"
    assert get_config_value("auth.api_key") == "secret"
    assert get_config_value("auth.missing", default="fallback") == "fallback"
