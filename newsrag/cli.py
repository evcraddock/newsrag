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
    apply_embedding_overrides,
    load_config,
    resolve_data_dir,
)
from newsrag.daemon import DaemonConfig, run_daemon
from newsrag.doctor import format_report, run_doctor
from newsrag.ingest import IngestError, enqueue_ingest_jobs, enqueue_ingest_url_job
from newsrag.jobs import list_jobs
from newsrag.manifests import ManifestError, load_manifest
from newsrag.packets import PacketError, format_source_packet, write_source_packet
from newsrag.search import SearchError, SearchFilters, build_search_engine, format_search_results
from newsrag.storage import (
    StorageError,
    format_status_report,
    get_storage_status,
    initialize_storage,
)
from newsrag.watches import add_watch, list_watches

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
PACKET_OUT_OPTION = typer.Option(
    ...,
    "--out",
    help="Write the Markdown source packet to this path.",
    dir_okay=False,
    resolve_path=False,
)

app = typer.Typer(help="Local-first evidence retrieval for city hall PDFs.")
daemon_app = typer.Typer(help="Run the NewsRAG background daemon.")
jobs_app = typer.Typer(help="Inspect durable NewsRAG jobs.")
watch_app = typer.Typer(help="Manage watched folders for automatic ingestion.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(jobs_app, name="jobs")
app.add_typer(watch_app, name="watch")


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
def doctor(
    ctx: typer.Context,
    embedding_provider: str | None = typer.Option(
        None,
        "--embedding-provider",
        help="Override the embedding provider for this doctor run.",
    ),
    embedding_base_url: str | None = typer.Option(
        None,
        "--embedding-base-url",
        help="Override the embedding provider base URL for this doctor run.",
    ),
    embedding_model: str | None = typer.Option(
        None,
        "--embedding-model",
        help="Override the embedding model for this doctor run.",
    ),
    embedding_api_key_env: str | None = typer.Option(
        None,
        "--embedding-api-key-env",
        help="Override the embedding API key environment variable for this doctor run.",
    ),
) -> None:
    """Validate the local NewsRAG environment and configuration."""

    settings, config_error = _resolve_runtime_settings(
        ctx,
        embedding_provider=embedding_provider,
        embedding_base_url=embedding_base_url,
        embedding_model=embedding_model,
        embedding_api_key_env=embedding_api_key_env,
    )
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
                embedding_config=settings.config.embedding,
                poll_interval=poll_interval,
                max_loops=max_loops,
            )
        )
    )


@app.command("ingest")
def ingest_command(
    ctx: typer.Context,
    path: Path,
    title: str | None = typer.Option(None, help="Document title override for the ingested PDFs."),
    body: str | None = typer.Option(None, help="Civic body metadata for the ingested PDFs."),
    document_type: str | None = typer.Option(
        None,
        "--document-type",
        help="Document type metadata for the ingested PDFs.",
    ),
    meeting_date: str | None = typer.Option(
        None,
        "--meeting-date",
        help="Meeting date metadata for the ingested PDFs.",
    ),
    jurisdiction: str | None = typer.Option(
        None,
        help="Jurisdiction metadata for the ingested PDFs.",
    ),
) -> None:
    """Enqueue one local PDF or directory of PDFs for ingestion."""

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    metadata = _build_document_metadata_options(
        title=title,
        body=body,
        document_type=document_type,
        meeting_date=meeting_date,
        jurisdiction=jurisdiction,
    )

    try:
        jobs = enqueue_ingest_jobs(database_path, source_path=path, metadata=metadata)
    except IngestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(f"Enqueued {len(jobs)} ingest job(s)")
    for job in jobs:
        typer.echo(f"{job.id} {job.payload['path']}")


@app.command("ingest-url")
def ingest_url_command(
    ctx: typer.Context,
    url: str,
    title: str | None = typer.Option(None, help="Document title override for the downloaded PDF."),
    body: str | None = typer.Option(None, help="Civic body metadata for the downloaded PDF."),
    document_type: str | None = typer.Option(
        None,
        "--document-type",
        help="Document type metadata for the downloaded PDF.",
    ),
    meeting_date: str | None = typer.Option(
        None,
        "--meeting-date",
        help="Meeting date metadata for the downloaded PDF.",
    ),
    jurisdiction: str | None = typer.Option(
        None,
        help="Jurisdiction metadata for the downloaded PDF.",
    ),
) -> None:
    """Download one direct PDF URL and enqueue it for ingestion."""

    settings, _ = _resolve_runtime_settings(ctx)
    storage_paths = initialize_storage(settings.data_dir)
    metadata = _build_document_metadata_options(
        title=title,
        body=body,
        document_type=document_type,
        meeting_date=meeting_date,
        jurisdiction=jurisdiction,
    )

    try:
        job = enqueue_ingest_url_job(
            storage_paths.database,
            storage_paths=storage_paths,
            url=url,
            metadata=metadata,
        )
    except IngestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo("Enqueued 1 ingest job(s)")
    typer.echo(f"{job.id} {job.payload['path']}")


@app.command("ingest-manifest")
def ingest_manifest_command(ctx: typer.Context, path: Path) -> None:
    """Load a YAML manifest and enqueue one URL-ingest job per document."""

    settings, _ = _resolve_runtime_settings(ctx)
    storage_paths = initialize_storage(settings.data_dir)

    try:
        manifest = load_manifest(path)
    except ManifestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    jobs = []
    try:
        for document in manifest.documents:
            jobs.append(
                enqueue_ingest_url_job(
                    storage_paths.database,
                    storage_paths=storage_paths,
                    url=document.url,
                    metadata=document.metadata,
                )
            )
    except IngestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(f"Enqueued {len(jobs)} ingest job(s)")
    for job in jobs:
        typer.echo(f"{job.id} {job.payload['path']}")


@app.command("search")
def search_command(
    ctx: typer.Context,
    query: str,
    body: str | None = typer.Option(None, help="Only search documents from this civic body."),
    document_type: str | None = typer.Option(
        None,
        "--document-type",
        help="Only search documents with this document type metadata.",
    ),
    jurisdiction: str | None = typer.Option(
        None,
        help="Only search documents from this jurisdiction.",
    ),
    source_url: str | None = typer.Option(
        None,
        "--source-url",
        help="Only search documents with this source URL.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only search documents with meeting dates on or after YYYY-MM-DD.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Only search documents with meeting dates on or before YYYY-MM-DD.",
    ),
) -> None:
    """Search indexed evidence passages with hybrid keyword/vector retrieval."""

    settings, _ = _resolve_runtime_settings(ctx)
    storage_paths = initialize_storage(settings.data_dir)
    filters = _build_search_filters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
    )

    try:
        engine = build_search_engine(
            database_path=storage_paths.database,
            lancedb_path=storage_paths.lancedb,
            embedding_config=settings.config.embedding,
        )
        results = engine.search(query, filters=filters)
    except SearchError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_search_results(results, query=query, filters=filters))


@app.command("packet")
def packet_command(
    ctx: typer.Context,
    query: str,
    out: Path = PACKET_OUT_OPTION,
    overwrite: bool = typer.Option(
        False,
        "--overwrite",
        help="Replace the output file if it already exists.",
    ),
    body: str | None = typer.Option(None, help="Only search documents from this civic body."),
    document_type: str | None = typer.Option(
        None,
        "--document-type",
        help="Only search documents with this document type metadata.",
    ),
    jurisdiction: str | None = typer.Option(
        None,
        help="Only search documents from this jurisdiction.",
    ),
    source_url: str | None = typer.Option(
        None,
        "--source-url",
        help="Only search documents with this source URL.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only search documents with meeting dates on or after YYYY-MM-DD.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Only search documents with meeting dates on or before YYYY-MM-DD.",
    ),
) -> None:
    """Generate an extractive Markdown source packet from retrieved evidence."""

    settings, _ = _resolve_runtime_settings(ctx)
    storage_paths = initialize_storage(settings.data_dir)
    filters = _build_search_filters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
    )

    try:
        engine = build_search_engine(
            database_path=storage_paths.database,
            lancedb_path=storage_paths.lancedb,
            embedding_config=settings.config.embedding,
        )
        results = engine.search(query, filters=filters)
        write_source_packet(
            out,
            format_source_packet(query=query, results=results, filters=filters),
            overwrite=overwrite,
        )
    except (SearchError, PacketError) as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(f"Wrote source packet to {out}")


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


@watch_app.command("add")
def watch_add_command(
    ctx: typer.Context,
    path: Path,
    body: str | None = typer.Option(None, help="Default civic body for files in this watch."),
    document_type: str | None = typer.Option(
        None,
        "--document-type",
        help="Default document type for files in this watch.",
    ),
    meeting_date_default: str | None = typer.Option(
        None,
        "--meeting-date-default",
        help="Default meeting date metadata for files in this watch.",
    ),
    jurisdiction: str | None = typer.Option(
        None,
        help="Default jurisdiction metadata for files in this watch.",
    ),
) -> None:
    """Register one watched folder."""

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    watch = add_watch(
        database_path,
        path=path,
        metadata={
            key: value
            for key, value in {
                "body": body,
                "document_type": document_type,
                "meeting_date_default": meeting_date_default,
                "jurisdiction": jurisdiction,
            }.items()
            if value is not None
        },
    )
    typer.echo(f"Added watch {watch.id} {watch.path}")


@watch_app.command("list")
def watch_list_command(ctx: typer.Context) -> None:
    """List watched folders."""

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    watches = list_watches(database_path)

    typer.echo("NewsRAG Watches")
    typer.echo(f"data_dir: {settings.data_dir}")
    if not watches:
        typer.echo("watches: none")
        return

    for watch in watches:
        typer.echo(f"{watch.id} {watch.path} metadata={watch.metadata}")


def run() -> None:
    """Run the Typer application."""

    app()


def _build_search_filters(
    *,
    body: str | None,
    document_type: str | None,
    jurisdiction: str | None,
    source_url: str | None,
    since: str | None,
    until: str | None,
) -> SearchFilters:
    return SearchFilters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
    )


def _build_document_metadata_options(
    *,
    title: str | None,
    body: str | None,
    document_type: str | None,
    meeting_date: str | None,
    jurisdiction: str | None,
) -> dict[str, str]:
    return {
        key: value
        for key, value in {
            "title": title,
            "body": body,
            "document_type": document_type,
            "meeting_date": meeting_date,
            "jurisdiction": jurisdiction,
        }.items()
        if value is not None
    }


def _resolve_runtime_settings(
    ctx: typer.Context,
    *,
    embedding_provider: str | None = None,
    embedding_base_url: str | None = None,
    embedding_model: str | None = None,
    embedding_api_key_env: str | None = None,
) -> tuple[RuntimeSettings, str | None]:
    state = _get_state(ctx)
    config_error: str | None = None

    try:
        config = load_config(state.config_path)
    except ConfigError as exc:
        config = AppConfig(source_path=state.config_path)
        config_error = str(exc)

    if any(
        override is not None
        for override in (
            embedding_provider,
            embedding_base_url,
            embedding_model,
            embedding_api_key_env,
        )
    ):
        config = AppConfig(
            source_path=config.source_path,
            data_dir=config.data_dir,
            embedding=apply_embedding_overrides(
                config.embedding,
                provider=embedding_provider,
                base_url=embedding_base_url,
                model=embedding_model,
                api_key_env=embedding_api_key_env,
            ),
            daemon=config.daemon,
        )

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
