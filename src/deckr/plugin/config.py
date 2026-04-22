from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

_EMPTY_CONFIG = MappingProxyType({})
_plugin_config: Mapping[str, Any] = _EMPTY_CONFIG
_plugin_configs: Mapping[str, Any] = _EMPTY_CONFIG


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(_freeze(item) for item in value)
    return value


def _install_plugin_config(config: Mapping[str, Any] | None) -> None:
    global _plugin_config
    if config is None:
        _plugin_config = _EMPTY_CONFIG
        return
    frozen = _freeze(config)
    if isinstance(frozen, Mapping):
        _plugin_config = frozen
        return
    raise TypeError("Plugin config root must be a mapping")


def _install_plugin_configs(configs: Mapping[str, Any] | None) -> None:
    global _plugin_configs
    if configs is None:
        _plugin_configs = _EMPTY_CONFIG
        return
    frozen = _freeze(configs)
    if isinstance(frozen, Mapping):
        _plugin_configs = frozen
        return
    raise TypeError("Plugin configs root must be a mapping")


def get_plugin_config() -> Mapping[str, Any]:
    return _plugin_config


def get_plugin_configs() -> Mapping[str, Any]:
    return _plugin_configs


def get_config_value(path: str, default: Any = None) -> Any:
    if not path:
        return get_plugin_config()
    current: Any = _plugin_config
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return default
        if segment not in current:
            return default
        current = current[segment]
    return current


def get_named_plugin_config(plugin_id: str) -> Mapping[str, Any]:
    config = _plugin_configs.get(plugin_id, _EMPTY_CONFIG)
    if isinstance(config, Mapping):
        return config
    return _EMPTY_CONFIG


def get_named_config_value(plugin_id: str, path: str, default: Any = None) -> Any:
    if not path:
        return get_named_plugin_config(plugin_id)
    current: Any = get_named_plugin_config(plugin_id)
    for segment in path.split("."):
        if not isinstance(current, Mapping):
            return default
        if segment not in current:
            return default
        current = current[segment]
    return current
