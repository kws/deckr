from __future__ import annotations

import os
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from glob import glob
from importlib.metadata import entry_points
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

DEFAULT_CONFIG_FILENAME = "deckr.toml"
CONFIG_SOURCE_ENTRYPOINT_GROUP = "deckr.config_sources"
BUILTIN_FILE_CONFIG_SOURCE_ID = "com.k-si.deckr.config.files"
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
class ConfigResolutionEvent:
    source_id: str
    message: str
    path: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfigResolutionReport:
    events: tuple[ConfigResolutionEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class ConfigSourceContext:
    source_id: str
    source_config: Mapping[str, Any]
    base_dir: Path
    env: Mapping[str, str]


class ConfigSourceLoader(Protocol):
    def __call__(self, context: ConfigSourceContext) -> Sequence[Mapping[str, Any]]: ...


@dataclass(frozen=True, slots=True)
class ConfigSourceDefinition:
    source_id: str
    load: ConfigSourceLoader


@dataclass(frozen=True, slots=True)
class ConfigDocument:
    raw: Mapping[str, Any]
    source_path: Path | None
    base_dir: Path
    config_report: ConfigResolutionReport = field(
        default_factory=ConfigResolutionReport
    )

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


def _validate_config_source_fragment(fragment: Mapping[str, Any], *, source_id: str) -> None:
    deckr = fragment.get("deckr")
    if not isinstance(deckr, Mapping):
        return
    config = deckr.get("config")
    if isinstance(config, Mapping) and "sources" in config:
        raise ValueError(
            f"Config source {source_id!r} must not contribute deckr.config.sources"
        )
    components = deckr.get("components")
    if isinstance(components, Mapping) and "instance_sources" in components:
        raise ValueError(
            f"Config source {source_id!r} must not contribute "
            "deckr.components.instance_sources"
        )


def _deep_merge(
    base: Mapping[str, Any],
    overlay: Mapping[str, Any],
    *,
    source_id: str,
    path: tuple[str, ...],
    events: list[ConfigResolutionEvent],
) -> dict[str, Any]:
    result = dict(base)
    for key, value in overlay.items():
        key_text = str(key)
        current_path = (*path, key_text)
        existing = result.get(key_text)
        if isinstance(existing, Mapping) and isinstance(value, Mapping):
            result[key_text] = _deep_merge(
                existing,
                value,
                source_id=source_id,
                path=current_path,
                events=events,
            )
            continue
        if key_text in result:
            events.append(
                ConfigResolutionEvent(
                    source_id=source_id,
                    path=current_path,
                    message="overrode configuration value",
                )
            )
        else:
            events.append(
                ConfigResolutionEvent(
                    source_id=source_id,
                    path=current_path,
                    message="contributed configuration value",
                )
            )
        result[key_text] = value
    return result


def _source_declarations(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    deckr = payload.get("deckr")
    if not isinstance(deckr, Mapping):
        return ()
    config = deckr.get("config")
    if not isinstance(config, Mapping):
        return ()
    sources = config.get("sources")
    if sources is None:
        return ()
    if isinstance(sources, str) or not isinstance(sources, Sequence):
        raise ValueError("deckr.config.sources must be an array of tables")
    declarations: list[Mapping[str, Any]] = []
    for index, item in enumerate(sources):
        if not isinstance(item, Mapping):
            raise ValueError(f"deckr.config.sources[{index}] must be a table")
        declarations.append(item)
    return tuple(declarations)


def _config_source_id(source: Mapping[str, Any], *, index: int) -> tuple[str, str]:
    declaration_id = source.get("id")
    source_id = source.get("source")
    if not isinstance(declaration_id, str) or not declaration_id.strip():
        raise ValueError(f"deckr.config.sources[{index}].id must be a non-empty string")
    if not isinstance(source_id, str) or not source_id.strip():
        raise ValueError(
            f"deckr.config.sources[{index}].source must be a non-empty string"
        )
    return declaration_id.strip(), source_id.strip()


def _file_config_source(context: ConfigSourceContext) -> Sequence[Mapping[str, Any]]:
    allowed = {"id", "source", "paths", "env_template", "env_defaults"}
    unknown = sorted(set(context.source_config) - allowed)
    if unknown:
        names = ", ".join(unknown)
        raise ValueError(f"Config source {context.source_id!r} unknown field(s): {names}")

    paths = context.source_config.get("paths")
    if isinstance(paths, str) or not isinstance(paths, Sequence):
        raise ValueError(f"Config source {context.source_id!r}.paths must be a list")
    env_template = context.source_config.get("env_template", False)
    if not isinstance(env_template, bool):
        raise ValueError(
            f"Config source {context.source_id!r}.env_template must be a boolean"
        )
    env_defaults = context.source_config.get("env_defaults")
    merged_env = dict(context.env)
    if env_defaults is not None:
        if not isinstance(env_defaults, Mapping):
            raise ValueError(
                f"Config source {context.source_id!r}.env_defaults must be a table"
            )
        for key, value in env_defaults.items():
            if not isinstance(key, str) or not _ENV_NAME_RE.fullmatch(key):
                raise ValueError(
                    f"Config source {context.source_id!r}.env_defaults keys must be "
                    "environment variable names"
                )
            if key not in merged_env:
                merged_env[key] = str(value)

    payloads: list[Mapping[str, Any]] = []
    for index, item in enumerate(paths):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(
                f"Config source {context.source_id!r}.paths[{index}] "
                "must be a non-empty string"
            )
        pattern_path = Path(item).expanduser()
        pattern = str(
            pattern_path if pattern_path.is_absolute() else context.base_dir / pattern_path
        )
        matches = sorted(Path(path) for path in glob(pattern))
        if not matches:
            raise ValueError(
                f"Config source {context.source_id!r} path pattern matched nothing: "
                f"{item!r}"
            )
        for match in matches:
            text = match.read_text()
            if env_template:
                text = substitute_config_environment(text, merged_env)
            payload = tomllib.loads(text)
            if not isinstance(payload, dict):
                raise ValueError(f"Config source {context.source_id!r} loaded no table")
            invalid = sorted(name for name in payload if name != "deckr")
            if invalid:
                names = ", ".join(invalid)
                raise ValueError(
                    f"Config source {context.source_id!r} loaded unsupported "
                    f"top-level table(s): {names}"
                )
            payloads.append(payload)
    return tuple(payloads)


def _builtin_config_source_definition(source_id: str) -> ConfigSourceDefinition | None:
    if source_id == BUILTIN_FILE_CONFIG_SOURCE_ID:
        return ConfigSourceDefinition(source_id=source_id, load=_file_config_source)
    return None


def load_config_source_definition(source_id: str) -> ConfigSourceDefinition | None:
    builtin = _builtin_config_source_definition(source_id)
    if builtin is not None:
        return builtin
    for entry_point in entry_points().select(group=CONFIG_SOURCE_ENTRYPOINT_GROUP):
        if entry_point.name != source_id:
            continue
        definition = entry_point.load()
        if not isinstance(definition, ConfigSourceDefinition):
            raise TypeError(
                f"Entry point {source_id!r} did not load a ConfigSourceDefinition"
            )
        if definition.source_id != source_id:
            raise ValueError(
                f"Config source entry point {source_id!r} loaded source "
                f"{definition.source_id!r}"
            )
        return definition
    return None


def _apply_config_sources(
    payload: Mapping[str, Any],
    *,
    base_dir: Path,
    env: Mapping[str, str],
) -> tuple[Mapping[str, Any], ConfigResolutionReport]:
    resolved: Mapping[str, Any] = payload
    events: list[ConfigResolutionEvent] = []
    for index, declaration in enumerate(_source_declarations(payload)):
        declaration_id, source_id = _config_source_id(declaration, index=index)
        definition = load_config_source_definition(source_id)
        if definition is None:
            raise ValueError(f"Unknown Deckr config source: {source_id}")
        events.append(
            ConfigResolutionEvent(
                source_id=source_id,
                message=f"ran config source {declaration_id}",
            )
        )
        context = ConfigSourceContext(
            source_id=source_id,
            source_config=declaration,
            base_dir=base_dir,
            env=env,
        )
        for fragment in definition.load(context):
            _validate_config_source_fragment(fragment, source_id=source_id)
            resolved = _deep_merge(
                resolved,
                fragment,
                source_id=source_id,
                path=(),
                events=events,
            )
    return resolved, ConfigResolutionReport(events=tuple(events))


def load_config_document(
    path: Path | None,
    *,
    default_text: str | None = None,
    env: Mapping[str, str] | None = None,
) -> ConfigDocument:
    payload, source_path = _load_payload(
        path,
        default_text=default_text,
        expand_env=False,
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
    resolved_payload, report = _apply_config_sources(
        payload,
        base_dir=base_dir,
        env=os.environ if env is None else env,
    )
    document = ConfigDocument(
        raw=_freeze(resolved_payload),
        source_path=source_path,
        base_dir=base_dir,
        config_report=report,
    )
    return document
