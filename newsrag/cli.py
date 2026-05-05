from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import typer

from newsrag.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    RuntimeSettings,
    load_config,
    resolve_data_dir,
)
from newsrag.doctor import format_report, run_doctor

CONFIG_PATH_OPTION = typer.Option(
    None,
    "--config-path",
    help="Override the path to the user-global config file.",
    dir_okay=False,
    resolve_path=False,
)
DATA_DIR_OPTION = typer.Option(
    None,
    "--data-dir",
    help="Override the active corpus data directory.",
    file_okay=False,
    resolve_path=False,
)

app = typer.Typer(help="Local-first evidence retrieval for city hall PDFs.")


@dataclass(frozen=True)
class CliState:
    """CLI state shared across commands."""

    config_path: Path
    data_dir: Path | None


@app.callback()
def main(
    ctx: typer.Context,
    config_path: Path | None = CONFIG_PATH_OPTION,
    data_dir: Path | None = DATA_DIR_OPTION,
) -> None:
    """Run NewsRAG commands."""

    ctx.obj = CliState(
        config_path=(config_path or DEFAULT_CONFIG_PATH).expanduser(),
        data_dir=data_dir,
    )


@app.command()
def doctor(ctx: typer.Context) -> None:
    """Validate the local NewsRAG environment and configuration."""

    state = _get_state(ctx)
    config_error: str | None = None

    try:
        config = load_config(state.config_path)
    except ConfigError as exc:
        config = AppConfig(source_path=state.config_path)
        config_error = str(exc)

    settings = RuntimeSettings(
        config_path=state.config_path,
        data_dir=resolve_data_dir(state.data_dir, config),
        config=config,
    )
    report = run_doctor(settings, config_error=config_error)
    typer.echo(format_report(report, settings=settings))

    if report.has_errors:
        raise typer.Exit(code=1)


def run() -> None:
    """Run the Typer application."""

    app()


def _get_state(ctx: typer.Context) -> CliState:
    state = ctx.obj
    if isinstance(state, CliState):
        return state
    return CliState(
        config_path=DEFAULT_CONFIG_PATH,
        data_dir=None,
    )
