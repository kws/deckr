from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

DEFAULT_CONFIG_FILENAME = "deckr.toml"
_EMPTY_MAPPING = MappingProxyType({})
_ENV_PLACEHOLDER_RE = re.compile(r"\$\{([^}]*)\}")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(_freeze(item) for item in value)
    return value


def substitute_config_environment(
    text: str,
    env: Mapping[str, str],
) -> str:
    """Replace ${VAR} and ${VAR:-default} placeholders before TOML parsing."""

    def replace(match: re.Match[str]) -> str:
        inner = match.group(1)
        name, separator, default = inner.partition(":-")
        if not separator and ":" in name:
            raise ValueError(f"Invalid configuration environment placeholder: {inner!r}")
        if not _ENV_NAME_RE.fullmatch(name):
            raise ValueError(f"Invalid configuration environment variable name: {name!r}")
        value = env.get(name)
        if value:
            return value
        if separator:
            return default
        raise ValueError(f"Missing environment variable for configuration: {name}")

    return _ENV_PLACEHOLDER_RE.sub(replace, text)


@dataclass(frozen=True, slots=True)
class ConfigDocument:
    raw: Mapping[str, Any]
    source_path: Path | None
    base_dir: Path

    def namespace(self, path: str) -> Mapping[str, Any] | None:
        current: Any = self.raw
        if not path:
            return current if isinstance(current, Mapping) else None
        for segment in path.split("."):
            if not isinstance(current, Mapping):
                return None
            current = current.get(segment)
        return current if isinstance(current, Mapping) else None

    def children(self, path: str) -> dict[str, Mapping[str, Any]]:
        namespace = self.namespace(path)
        if namespace is None:
            return {}
        return {
            str(name): value
            for name, value in namespace.items()
            if isinstance(value, Mapping)
        }

    def resolve_path(self, value: Path | str) -> Path:
        path = value if isinstance(value, Path) else Path(value)
        if path.is_absolute():
            return path
        return (self.base_dir / path).resolve()

    @property
    def deckr(self) -> Mapping[str, Any]:
        return self.namespace("deckr") or _EMPTY_MAPPING


def _load_payload(
    path: Path | None,
    *,
    default_text: str | None,
    expand_env: bool,
    env: Mapping[str, str],
) -> tuple[dict[str, Any], Path | None]:
    if path is not None:
        resolved = path.expanduser().resolve()
        text = resolved.read_text()
        if expand_env:
            text = substitute_config_environment(text, env)
        payload = tomllib.loads(text)
        return payload, resolved

    candidate = (Path.cwd() / DEFAULT_CONFIG_FILENAME).resolve()
    if candidate.exists():
        text = candidate.read_text()
        if expand_env:
            text = substitute_config_environment(text, env)
        payload = tomllib.loads(text)
        return payload, candidate

    if default_text is not None:
        text = default_text
        if expand_env:
            text = substitute_config_environment(text, env)
        return tomllib.loads(text), None

    return {"deckr": {}}, None


def load_config_document(
    path: Path | None,
    *,
    default_text: str | None = None,
    expand_env: bool = False,
    env: Mapping[str, str] | None = None,
) -> ConfigDocument:
    payload, source_path = _load_payload(
        path,
        default_text=default_text,
        expand_env=expand_env,
        env=os.environ if env is None else env,
    )
    if not isinstance(payload, dict):
        raise ValueError("Configuration document root must be a table")

    invalid = sorted(name for name in payload if name != "deckr")
    if invalid:
        names = ", ".join(invalid)
        raise ValueError(
            f"Unsupported top-level configuration tables: {names}. "
            "Use [deckr.*] namespaces."
        )

    deckr_payload = payload.get("deckr")
    if deckr_payload is None:
        raise ValueError("Configuration document must define a [deckr] table")
    if not isinstance(deckr_payload, dict):
        raise ValueError("[deckr] must be a table")

    base_dir = source_path.parent if source_path is not None else Path.cwd()
    document = ConfigDocument(
        raw=_freeze(payload),
        source_path=source_path,
        base_dir=base_dir,
    )
    return document
