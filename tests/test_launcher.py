from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import pytest
from click.testing import CliRunner

from deckr import cli as cli_mod
from deckr.core.config import ConfigDocument
from deckr.launcher import (
    LauncherSpec,
    launch,
    load_launcher_document,
    run_configured_deckr,
    run_document,
    validate_component_configuration,
)


def test_load_launcher_document_passes_path_to_default_loader(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    document = ConfigDocument(raw={"deckr": {}}, source_path=None, base_dir=tmp_path)

    def fake_load_config_document(path, *, default_text):
        captured["path"] = path
        captured["default_text"] = default_text
        return document

    monkeypatch.setattr("deckr.launcher.load_config_document", fake_load_config_document)

    loaded = load_launcher_document(
        tmp_path / "deckr.toml",
        spec=LauncherSpec(default_config_text="[deckr]\n"),
    )

    assert loaded is document
    assert captured["path"] == (tmp_path / "deckr.toml").resolve()
    assert captured["default_text"] == "[deckr]\n"


def test_validate_component_configuration_rejects_empty_document(
) -> None:
    with pytest.raises(ValueError, match="does not define any component instances"):
        validate_component_configuration(
            ConfigDocument(raw={"deckr": {}}, source_path=None, base_dir=Path.cwd())
        )


def test_launch_runs_before_run_hook_and_anyio(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    document = ConfigDocument(raw={"deckr": {}}, source_path=None, base_dir=tmp_path)
    captured: dict[str, object] = {}

    def fake_loader(path: Path | None) -> ConfigDocument:
        captured["path"] = path
        return document

    def fake_before_run(loaded: ConfigDocument) -> None:
        captured["document"] = loaded

    async def fake_runner(loaded: ConfigDocument) -> None:
        captured["runner_document"] = loaded

    def fake_validate(loaded: ConfigDocument) -> None:
        captured["validated"] = loaded

    def fake_anyio_run(fn, loaded: ConfigDocument, runner) -> None:
        captured["fn"] = fn
        captured["anyio_document"] = loaded
        captured["anyio_runner"] = runner

    monkeypatch.setattr("deckr.launcher.validate_component_configuration", fake_validate)
    monkeypatch.setattr("deckr.launcher.anyio.run", fake_anyio_run)

    launch(
        tmp_path / "deckr.toml",
        spec=LauncherSpec(
            load_document=fake_loader,
            before_run=fake_before_run,
            runner=fake_runner,
        ),
    )

    assert captured["path"] == (tmp_path / "deckr.toml").resolve()
    assert captured["document"] is document
    assert captured["validated"] is document
    assert captured["fn"] is run_document
    assert captured["anyio_document"] is document
    assert captured["anyio_runner"] is fake_runner


def test_run_document_uses_signal_handler_and_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_add_signal_handler(tg) -> None:
        captured["task_group"] = tg

    async def fake_runner(document: ConfigDocument) -> None:
        captured["document"] = document

    monkeypatch.setattr("deckr.launcher.add_signal_handler", fake_add_signal_handler)

    document = ConfigDocument(raw={"deckr": {}}, source_path=None, base_dir=Path.cwd())
    anyio.run(run_document, document, fake_runner)

    assert captured["document"] is document
    assert "task_group" in captured


def test_run_configured_deckr_uses_public_runtime_and_component_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Done(Exception):
        pass

    captured: dict[str, object] = {}
    plan = type(
        "Plan",
        (),
        {
            "lane_contracts": "contracts",
            "lane_names": ("actions",),
        },
    )()

    class FakeDeckr:
        def __init__(self, *, lane_contracts, lanes, message_bus):
            captured["lane_contracts"] = lane_contracts
            captured["lanes"] = lanes
            captured["message_bus"] = message_bus

        async def __aenter__(self):
            captured["deckr_entered"] = True
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            captured["deckr_exited"] = True

    @asynccontextmanager
    async def fake_start_components(deckr, resolved_plan):
        captured["start_components_deckr"] = deckr
        captured["start_components_plan"] = resolved_plan
        yield object()

    async def fake_sleep_forever() -> None:
        raise Done()

    document = ConfigDocument(raw={"deckr": {}}, source_path=None, base_dir=Path.cwd())
    monkeypatch.setattr("deckr.launcher.resolve_component_host_plan", lambda doc: plan)
    monkeypatch.setattr("deckr.launcher.Deckr", FakeDeckr)
    monkeypatch.setattr("deckr.launcher.start_components", fake_start_components)
    monkeypatch.setattr("deckr.launcher.anyio.sleep_forever", fake_sleep_forever)

    with pytest.raises(Done):
        anyio.run(run_configured_deckr, document)

    assert captured["lane_contracts"] == "contracts"
    assert captured["lanes"] == ("actions",)
    assert captured["message_bus"] is not None
    assert captured["start_components_plan"] is plan
    assert captured["deckr_entered"] is True
    assert captured["deckr_exited"] is True


def test_cli_prints_default_config() -> None:
    runner = CliRunner()
    command = cli_mod.build_cli(
        spec=LauncherSpec(default_config_text="[deckr.components.instances.main]\n")
    )

    result = runner.invoke(command, ["--print-default-config"])

    assert result.exit_code == 0
    assert result.output == "[deckr.components.instances.main]\n\n"


def test_cli_loads_logging_config_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    captured: dict[str, object] = {}
    log_config = tmp_path / "logging.ini"
    log_config.write_text("[loggers]\nkeys=root\n")

    def fake_file_config(path: str, *, disable_existing_loggers: bool) -> None:
        captured["path"] = path
        captured["disable_existing_loggers"] = disable_existing_loggers

    def fake_launch(config_path, *, spec=None) -> None:
        captured["launched"] = True

    monkeypatch.setattr(cli_mod.logging.config, "fileConfig", fake_file_config)
    monkeypatch.setattr(
        cli_mod,
        "configure_process_logging",
        lambda level: captured.setdefault("default_level", level),
    )
    monkeypatch.setattr(cli_mod, "launch", fake_launch)

    runner = CliRunner()
    command = cli_mod.build_cli(spec=LauncherSpec(default_config_text="[deckr]\n"))

    result = runner.invoke(command, ["--log-config", str(log_config)])

    assert result.exit_code == 0
    assert captured["path"] == str(log_config.resolve())
    assert captured["disable_existing_loggers"] is False
    assert "default_level" not in captured
    assert captured["launched"] is True


def test_cli_reports_leaf_exception_from_exception_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_launch(config_path, *, spec: LauncherSpec | None = None) -> None:
        raise ExceptionGroup(
            "unhandled errors in a TaskGroup",
            [ValueError("Controller ID is required.")],
        )

    monkeypatch.setattr(cli_mod, "launch", fake_launch)
    monkeypatch.setattr(cli_mod, "configure_process_logging", lambda level: None)

    runner = CliRunner()
    command = cli_mod.build_cli(spec=LauncherSpec(default_config_text="[deckr]\n"))

    result = runner.invoke(command, [])

    assert result.exit_code != 0
    assert "Controller ID is required." in result.output
    assert "TaskGroup" not in result.output


def test_cli_reports_multiple_leaf_exceptions_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_launch(config_path, *, spec: LauncherSpec | None = None) -> None:
        raise ExceptionGroup(
            "outer",
            [
                ExceptionGroup(
                    "inner",
                    [ValueError("first problem"), RuntimeError("second problem")],
                ),
                ValueError("first problem"),
            ],
        )

    monkeypatch.setattr(cli_mod, "launch", fake_launch)
    monkeypatch.setattr(cli_mod, "configure_process_logging", lambda level: None)

    runner = CliRunner()
    command = cli_mod.build_cli(spec=LauncherSpec(default_config_text="[deckr]\n"))

    result = runner.invoke(command, [])

    assert result.exit_code != 0
    assert "Multiple errors occurred:" in result.output
    assert result.output.count("first problem") == 1
    assert result.output.count("second problem") == 1
