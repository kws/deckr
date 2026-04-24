from __future__ import annotations

from typing import Any

from deckr.launcher import LauncherSpec, default_config_document_text, launch


def _require_click() -> Any:
    try:
        import click
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "The `deckr` CLI requires the optional `cli` extra. "
            "Install `deckr[cli]`."
        ) from exc
    return click


def build_cli(*, spec: LauncherSpec | None = None):
    click = _require_click()
    resolved_spec = spec or LauncherSpec(
        default_config_text=default_config_document_text()
    )

    @click.command(context_settings={"help_option_names": ["-h", "--help"]})
    @click.option(
        "--config",
        "config_path",
        type=click.Path(
            dir_okay=False,
            path_type=str,
            resolve_path=True,
        ),
        default=None,
        metavar="PATH",
        help="Load configuration from PATH instead of auto-loading ./deckr.toml.",
    )
    @click.option(
        "--print-default-config",
        is_flag=True,
        help="Print the built-in default deckr.toml document and exit.",
    )
    def command(config_path: str | None, print_default_config: bool) -> None:
        if print_default_config:
            click.echo(resolved_spec.default_config_text or "")
            return
        try:
            launch(config_path, spec=resolved_spec)
        except Exception as exc:
            raise click.ClickException(str(exc)) from exc

    return command


def main(argv: list[str] | None = None) -> None:
    click = _require_click()
    command = build_cli()
    try:
        command.main(args=argv, prog_name="deckr", standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(exc.exit_code) from exc
    except click.exceptions.Exit as exc:
        raise SystemExit(exc.exit_code) from exc
