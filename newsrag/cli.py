from __future__ import annotations

import asyncio
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
from newsrag.daemon import DaemonConfig, run_daemon
from newsrag.doctor import format_report, run_doctor
from newsrag.jobs import list_jobs
from newsrag.storage import (
    StorageError,
    format_status_report,
    get_storage_status,
    initialize_storage,
)

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
daemon_app = typer.Typer(help="Run the NewsRAG background daemon.")
jobs_app = typer.Typer(help="Inspect durable NewsRAG jobs.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(jobs_app, name="jobs")


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

    settings, config_error = _resolve_runtime_settings(ctx)
    report = run_doctor(settings, config_error=config_error)
    typer.echo(format_report(report, settings=settings))

    if report.has_errors:
        raise typer.Exit(code=1)


@app.command()
def status(
    ctx: typer.Context,
    initialize: bool = typer.Option(
        False,
        "--initialize",
        help="Create the storage layout before reporting status.",
    ),
) -> None:
    """Report storage layout health for the active data directory."""

    settings, _ = _resolve_runtime_settings(ctx)

    if initialize:
        try:
            initialize_storage(settings.data_dir)
        except StorageError as exc:
            typer.echo(
                format_status_report(
                    get_storage_status(settings.data_dir), data_dir=settings.data_dir
                )
            )
            raise typer.Exit(code=1) from exc

    report = get_storage_status(settings.data_dir)
    typer.echo(format_status_report(report, data_dir=settings.data_dir))

    if report.has_errors:
        raise typer.Exit(code=1)


@daemon_app.command("run")
def daemon_run(
    ctx: typer.Context,
    poll_interval: float = typer.Option(0.5, help="Seconds to wait between queue polls."),
    max_loops: int | None = typer.Option(None, hidden=True),
) -> None:
    """Run the foreground NewsRAG daemon loop."""

    settings, _ = _resolve_runtime_settings(ctx)
    typer.echo(f"NewsRAG daemon running for {settings.data_dir}")
    asyncio.run(
        run_daemon(
            DaemonConfig(
                data_dir=settings.data_dir,
                poll_interval=poll_interval,
                max_loops=max_loops,
            )
        )
    )


@jobs_app.command("list")
def jobs_list_command(ctx: typer.Context) -> None:
    """List durable NewsRAG jobs."""

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    jobs = list_jobs(database_path)

    typer.echo("NewsRAG Jobs")
    typer.echo(f"data_dir: {settings.data_dir}")
    if not jobs:
        typer.echo("jobs: none")
        return

    for job in jobs:
        line = f"{job.id} {job.status} {job.kind}"
        if job.error is not None:
            line = f"{line} error={job.error}"
        typer.echo(line)


def run() -> None:
    """Run the Typer application."""

    app()


def _resolve_runtime_settings(ctx: typer.Context) -> tuple[RuntimeSettings, str | None]:
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
    return settings, config_error


def _get_state(ctx: typer.Context) -> CliState:
    state = ctx.obj
    if isinstance(state, CliState):
        return state
    return CliState(
        config_path=DEFAULT_CONFIG_PATH,
        data_dir=None,
    )
