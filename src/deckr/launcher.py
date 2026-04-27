from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TypeAlias

import anyio

from deckr.components import (
    configured_component_instance_specs,
    resolve_component_host_plan,
    start_components,
)
from deckr.core.config import ConfigDocument, load_config_document
from deckr.core.util.anyio import add_signal_handler
from deckr.runtime import Deckr

DocumentHook: TypeAlias = Callable[[ConfigDocument], None]
DocumentLoader: TypeAlias = Callable[[Path | None], ConfigDocument]
DocumentRunner: TypeAlias = Callable[[ConfigDocument], Awaitable[None]]

_DEFAULT_CONFIG_DOCUMENT_TEXT = """# Deckr configuration document
#
# Configure each component under its manifest-declared exact prefix.
#
# Examples:
#   [deckr.controller]
#   [deckr.plugin_hosts.python.instances.main]
#   [deckr.transports.websocket.instances.main]

[deckr]
"""


async def run_configured_deckr(document: ConfigDocument) -> None:
    plan = resolve_component_host_plan(document)
    async with Deckr(
        lane_contracts=plan.lane_contracts,
        lanes=plan.lane_names,
    ) as deckr, start_components(deckr, plan):
        await anyio.sleep_forever()


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


def config_env_from_environment(env: dict[str, str] | None = None) -> bool:
    source = os.environ if env is None else env
    value = source.get("DECKR_CONFIG_ENV")
    if value is None:
        return False
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"Invalid DECKR_CONFIG_ENV value: {value!r}")


def load_launcher_document(
    config_path: str | Path | None,
    *,
    spec: LauncherSpec | None = None,
    config_env: bool = False,
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
        expand_env=config_env,
    )


def validate_component_configuration(document: ConfigDocument) -> None:
    if configured_component_instance_specs(document):
        return
    raise ValueError(
        "Configuration does not define any component instances. "
        "Add a singleton [deckr.<component>] table or a multi-instance "
        "[deckr.<component>.instances.<name>] table."
    )


async def run_document(
    document: ConfigDocument,
    runner: DocumentRunner = run_configured_deckr,
) -> None:
    async with anyio.create_task_group() as tg:
        await add_signal_handler(tg)
        await runner(document)
        tg.cancel_scope.cancel()


def launch(
    config_path: str | Path | None,
    *,
    spec: LauncherSpec | None = None,
    config_env: bool | None = None,
) -> None:
    resolved_spec = spec or LauncherSpec(
        default_config_text=default_config_document_text()
    )
    expand_env = config_env_from_environment() if config_env is None else config_env
    document = load_launcher_document(
        config_path,
        spec=resolved_spec,
        config_env=expand_env,
    )
    if resolved_spec.require_components:
        validate_component_configuration(document)
    if resolved_spec.before_run is not None:
        resolved_spec.before_run(document)
    anyio.run(run_document, document, resolved_spec.runner)
