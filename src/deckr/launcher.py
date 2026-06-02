from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import anyio

from deckr.components import resolve_component_host_plan, start_components
from deckr.contracts.lanes import MessageContractRegistry
from deckr.core.config import ConfigDocument, load_config_document
from deckr.core.util.anyio import add_signal_handler
from deckr.lanes import MessageBus
from deckr.runtime import Deckr
from deckr.substrates.nats import NatsSubstrate
from deckr.substrates.supervised_nats import SupervisedNatsSubstrate

DocumentHook: TypeAlias = Callable[[ConfigDocument], None]
DocumentLoader: TypeAlias = Callable[[Path | None], ConfigDocument]
DocumentRunner: TypeAlias = Callable[[ConfigDocument], Awaitable[None]]

_DEFAULT_CONFIG_DOCUMENT_TEXT = """# Deckr configuration document
#
# Configure component instances under [deckr.components.instances.<name>].
#
# Examples:
#   [deckr.runtime.substrate]
#   kind = "nats"
#   supervised = true
#
#   [deckr.components.instances.controller_main]
#   component = "dev.deckr.controller"
#   instance_id = "main"

[deckr]
"""


async def run_configured_deckr(document: ConfigDocument) -> None:
    plan = resolve_component_host_plan(document)
    message_bus = build_runtime_substrate(document, lane_contracts=plan.lane_contracts)
    async with Deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
        message_bus=message_bus,
    ) as deckr, start_components(deckr, plan):
        await anyio.sleep_forever()


def build_runtime_substrate(
    document: ConfigDocument,
    *,
    lane_contracts: MessageContractRegistry,
) -> MessageBus:
    source = document.namespace("deckr.runtime.substrate")
    if source is None:
        return NatsSubstrate(lane_contracts=lane_contracts)
    if not isinstance(source, Mapping):
        raise ValueError("[deckr.runtime.substrate] must be a table")
    kind = str(source.get("kind", "nats")).strip().lower()
    if kind == "nats":
        if _bool_config(source, "supervised", default=False):
            if "url" in source:
                raise ValueError(
                    "[deckr.runtime.substrate].url is not used when supervised = true"
                )
            auth_enabled, auth_token = _supervised_auth_config(source)
            server_path = _absolute_path_config(source, "server_path")
            runtime_dir = _relative_path_config(document, source, "runtime_dir")
            store_dir = _relative_path_config(document, source, "store_dir")
            return SupervisedNatsSubstrate(
                lane_contracts=lane_contracts,
                auth_enabled=auth_enabled,
                auth_token=auth_token,
                server_path=server_path,
                runtime_dir=runtime_dir,
                store_dir=store_dir,
                host=_string_config(source, "host", default="127.0.0.1"),
                port=_int_config(source, "port", default=-1),
                startup_timeout=_float_config(
                    source,
                    "startup_timeout",
                    default=10.0,
                ),
                shutdown_timeout=_float_config(
                    source,
                    "shutdown_timeout",
                    default=5.0,
                ),
                log_buffer_lines=_int_config(
                    source,
                    "log_buffer_lines",
                    default=200,
                ),
                minimum_server_version=_string_config(
                    source,
                    "minimum_server_version",
                    default="2.14.0",
                ),
            )
        url = str(source.get("url", "nats://127.0.0.1:4222")).strip()
        if not url:
            raise ValueError("[deckr.runtime.substrate].url must not be empty")
        auth_token = _optional_string_config(source, "auth_token")
        return NatsSubstrate(
            url=url,
            auth_token=auth_token,
            lane_contracts=lane_contracts,
        )
    raise ValueError(f"Unsupported Deckr runtime substrate kind: {kind!r}")


def _supervised_auth_config(source: Mapping[str, object]) -> tuple[bool, str | None]:
    if "auth" not in source:
        return True, None
    value = source["auth"]
    if value is False:
        return False, None
    if isinstance(value, str) and value.strip().lower() == "token":
        return True, "token"
    raise ValueError('[deckr.runtime.substrate].auth must be false or "token"')


def _bool_config(source: Mapping[str, object], key: str, *, default: bool) -> bool:
    value = source.get(key, default)
    if not isinstance(value, bool):
        raise ValueError(f"[deckr.runtime.substrate].{key} must be a boolean")
    return value


def _int_config(source: Mapping[str, object], key: str, *, default: int) -> int:
    value = source.get(key, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"[deckr.runtime.substrate].{key} must be an integer")
    return value


def _float_config(source: Mapping[str, object], key: str, *, default: float) -> float:
    value = source.get(key, default)
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ValueError(f"[deckr.runtime.substrate].{key} must be a number")
    return float(value)


def _string_config(source: Mapping[str, object], key: str, *, default: str) -> str:
    value = source.get(key, default)
    if not isinstance(value, str):
        raise ValueError(f"[deckr.runtime.substrate].{key} must be a string")
    resolved = value.strip()
    if not resolved:
        raise ValueError(f"[deckr.runtime.substrate].{key} must not be empty")
    return resolved


def _optional_string_config(source: Mapping[str, object], key: str) -> str | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"[deckr.runtime.substrate].{key} must be a string")
    resolved = value.strip()
    if not resolved:
        raise ValueError(f"[deckr.runtime.substrate].{key} must not be empty")
    return resolved


def _absolute_path_config(source: Mapping[str, object], key: str) -> Path | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"[deckr.runtime.substrate].{key} must be a string")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f"[deckr.runtime.substrate].{key} must be an absolute path")
    return path


def _relative_path_config(
    document: ConfigDocument,
    source: Mapping[str, object],
    key: str,
) -> Path | None:
    value = source.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"[deckr.runtime.substrate].{key} must be a string")
    if not value.strip():
        raise ValueError(f"[deckr.runtime.substrate].{key} must not be empty")
    expanded = Path(value).expanduser()
    if expanded.is_absolute():
        return expanded
    return document.resolve_path(expanded)


@dataclass(frozen=True, slots=True)
class LauncherSpec:
    default_config_text: str | None = None
    load_document: DocumentLoader | None = None
    before_run: DocumentHook | None = None
    runner: DocumentRunner = run_configured_deckr
    require_components: bool = True


def default_config_document_text() -> str:
    return _DEFAULT_CONFIG_DOCUMENT_TEXT


def resolve_config_path(config_path: str | Path | None) -> Path | None:
    if config_path is None:
        return None
    path = config_path if isinstance(config_path, Path) else Path(config_path)
    return path.expanduser().resolve()


def load_launcher_document(
    config_path: str | Path | None,
    *,
    spec: LauncherSpec | None = None,
) -> ConfigDocument:
    resolved_spec = spec or LauncherSpec(
        default_config_text=default_config_document_text()
    )
    path = resolve_config_path(config_path)
    if resolved_spec.load_document is not None:
        return resolved_spec.load_document(path)
    return load_config_document(
        path,
        default_text=resolved_spec.default_config_text,
    )


def validate_component_configuration(document: ConfigDocument) -> None:
    if document.children("deckr.components.instances"):
        return
    components = document.namespace("deckr.components")
    if isinstance(components, Mapping) and components.get("instance_sources"):
        return
    raise ValueError(
        "Configuration does not define any component instances. "
        "Add [deckr.components.instances.<name>] or configure "
        "[[deckr.components.instance_sources]]."
    )


async def run_document(
    document: ConfigDocument,
    runner: DocumentRunner = run_configured_deckr,
) -> None:
    async with anyio.create_task_group() as tg:
        await add_signal_handler(tg)

        async def run_until_done() -> None:
            try:
                await runner(document)
            finally:
                tg.cancel_scope.cancel()

        tg.start_soon(run_until_done, name="deckr.launcher.runner")
        await anyio.sleep_forever()


def launch(
    config_path: str | Path | None,
    *,
    spec: LauncherSpec | None = None,
) -> None:
    resolved_spec = spec or LauncherSpec(
        default_config_text=default_config_document_text()
    )
    document = load_launcher_document(
        config_path,
        spec=resolved_spec,
    )
    if resolved_spec.require_components:
        validate_component_configuration(document)
    if resolved_spec.before_run is not None:
        resolved_spec.before_run(document)
    anyio.run(run_document, document, resolved_spec.runner)
