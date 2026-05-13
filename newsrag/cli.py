from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import typer

from newsrag import __version__
from newsrag.config import (
    DEFAULT_CONFIG_PATH,
    AppConfig,
    ConfigError,
    RuntimeSettings,
    apply_embedding_overrides,
    load_config,
    resolve_data_dir,
)

if TYPE_CHECKING:
    from newsrag.discovery_browse import DiscoveryBrowseFilters
    from newsrag.jobs import Job
    from newsrag.search import SearchFilters

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
PDF_EXTRACTOR_OPTION = typer.Option(
    "auto",
    "--pdf-extractor",
    help="PDF text extractor mode: auto, pymupdf, pdfplumber, or table.",
)
ENRICH_RESPONSE_JSON_OPTION = typer.Option(
    ...,
    "--response-json",
    help="Path to structured enrichment JSON produced by an enrichment provider.",
    dir_okay=False,
    resolve_path=False,
)
DISCOVERY_BODY_OPTION = typer.Option(None, help="Only browse items from this civic body.")
DISCOVERY_DOCUMENT_TYPE_OPTION = typer.Option(
    None,
    "--document-type",
    help="Only browse items from documents with this document type metadata.",
)
DISCOVERY_JURISDICTION_OPTION = typer.Option(
    None,
    help="Only browse items from this jurisdiction.",
)
DISCOVERY_SOURCE_URL_OPTION = typer.Option(
    None,
    "--source-url",
    help="Only browse items from documents with this source URL.",
)
DISCOVERY_SINCE_OPTION = typer.Option(
    None,
    "--since",
    help="Only browse items from documents with meeting dates on or after YYYY-MM-DD.",
)
DISCOVERY_UNTIL_OPTION = typer.Option(
    None,
    "--until",
    help="Only browse items from documents with meeting dates on or before YYYY-MM-DD.",
)
DISCOVERY_ITEM_TYPE_OPTION = typer.Option(
    None,
    "--item-type",
    help="Only browse timeline items with this discovery item type.",
)
DISCOVERY_MIN_CONFIDENCE_OPTION = typer.Option(
    None,
    "--min-confidence",
    help="Only browse items at or above this confidence threshold.",
)
DISCOVERY_LIMIT_OPTION = typer.Option(50, "--limit", help="Maximum items to show, up to 500.")
DISCOVERY_OFFSET_OPTION = typer.Option(0, "--offset", help="Number of matching items to skip.")

app = typer.Typer(
    help="Local-first evidence retrieval for city hall PDFs.",
    invoke_without_command=True,
    no_args_is_help=True,
)
daemon_app = typer.Typer(help="Run the NewsRAG background daemon.")
jobs_app = typer.Typer(help="Inspect durable NewsRAG jobs.")
watch_app = typer.Typer(help="Manage watched folders for automatic ingestion.")
documents_app = typer.Typer(help="Browse ingested document inventory.")
discover_app = typer.Typer(help="Extract and inspect discovery signals.")
enrich_app = typer.Typer(help="Run optional structured discovery enrichment.")
topics_app = typer.Typer(help="Browse corpus topics from discovery records.")
entities_app = typer.Typer(help="Browse corpus entities from discovery records.")
leads_app = typer.Typer(help="Browse story leads from discovery records.")
app.add_typer(daemon_app, name="daemon")
app.add_typer(jobs_app, name="jobs")
app.add_typer(watch_app, name="watch")
app.add_typer(documents_app, name="documents")
app.add_typer(discover_app, name="discover")
app.add_typer(enrich_app, name="enrich")
app.add_typer(topics_app, name="topics")
app.add_typer(entities_app, name="entities")
app.add_typer(leads_app, name="leads")


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
    version: bool = typer.Option(False, "--version", help="Show the NewsRAG version and exit."),
) -> None:
    """Run NewsRAG commands."""

    if version:
        typer.echo(f"newsrag {__version__}")
        raise typer.Exit

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

    from newsrag.doctor import format_report, run_doctor

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

    from newsrag.storage import (
        StorageError,
        format_status_report,
        get_storage_status,
        initialize_storage,
    )

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

    import asyncio

    from newsrag.daemon import DaemonConfig, run_daemon

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
    pdf_extractor: str = PDF_EXTRACTOR_OPTION,
) -> None:
    """Enqueue one local PDF or directory of PDFs for ingestion."""

    from newsrag.ingest import IngestError, enqueue_ingest_jobs, normalize_pdf_extractor_mode
    from newsrag.storage import initialize_storage

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
        extraction_mode = normalize_pdf_extractor_mode(pdf_extractor)
        jobs = enqueue_ingest_jobs(
            database_path,
            source_path=path,
            metadata=metadata,
            pdf_extractor=extraction_mode,
        )
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
    pdf_extractor: str = PDF_EXTRACTOR_OPTION,
) -> None:
    """Download one direct PDF URL and enqueue it for ingestion."""

    from newsrag.ingest import IngestError, enqueue_ingest_url_job, normalize_pdf_extractor_mode
    from newsrag.storage import initialize_storage

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
        extraction_mode = normalize_pdf_extractor_mode(pdf_extractor)
        job = enqueue_ingest_url_job(
            storage_paths.database,
            storage_paths=storage_paths,
            url=url,
            metadata=metadata,
            pdf_extractor=extraction_mode,
        )
    except IngestError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo("Enqueued 1 ingest job(s)")
    typer.echo(f"{job.id} {job.payload['path']}")


@app.command("ingest-manifest")
def ingest_manifest_command(ctx: typer.Context, path: Path) -> None:
    """Load a YAML manifest and enqueue one URL-ingest job per document."""

    from newsrag.ingest import IngestError, enqueue_ingest_url_job
    from newsrag.manifests import ManifestError, load_manifest
    from newsrag.storage import initialize_storage

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

    from newsrag.search import SearchError, build_search_engine, format_search_results
    from newsrag.storage import initialize_storage

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

    from newsrag.packets import PacketError, format_source_packet, write_source_packet
    from newsrag.search import SearchError, build_search_engine
    from newsrag.storage import initialize_storage

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


@documents_app.command("list")
def documents_list_command(
    ctx: typer.Context,
    body: str | None = typer.Option(None, help="Only list documents from this civic body."),
    document_type: str | None = typer.Option(
        None,
        "--document-type",
        help="Only list documents with this document type metadata.",
    ),
    jurisdiction: str | None = typer.Option(
        None,
        help="Only list documents from this jurisdiction.",
    ),
    source_url: str | None = typer.Option(
        None,
        "--source-url",
        help="Only list documents with this source URL.",
    ),
    since: str | None = typer.Option(
        None,
        "--since",
        help="Only list documents with meeting dates on or after YYYY-MM-DD.",
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help="Only list documents with meeting dates on or before YYYY-MM-DD.",
    ),
    query: str | None = typer.Option(
        None,
        "--query",
        "-q",
        help="Search document IDs, titles, source paths, source URLs, and filenames.",
    ),
    limit: int = typer.Option(50, "--limit", help="Maximum documents to show, up to 500."),
    offset: int = typer.Option(0, "--offset", help="Number of matching documents to skip."),
) -> None:
    """List ingested documents with bounded, filterable output."""

    from newsrag.documents import (
        DocumentError,
        DocumentFilters,
        format_document_list,
        list_document_summaries,
    )
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    filters = DocumentFilters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
        query=query,
    )
    try:
        page = list_document_summaries(
            database_path,
            filters=filters,
            limit=limit,
            offset=offset,
        )
    except DocumentError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_document_list(page))


@documents_app.command("show")
def documents_show_command(ctx: typer.Context, document_id: str) -> None:
    """Show details for one ingested document."""

    from newsrag.documents import DocumentError, format_document_detail, get_document_detail
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    try:
        document = get_document_detail(database_path, document_id)
    except DocumentError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_document_detail(document))


@discover_app.command("document")
def discover_document_command(
    ctx: typer.Context,
    document_id: str,
    persist: bool = typer.Option(
        True,
        "--persist/--no-persist",
        help="Persist newly extracted deterministic discovery items.",
    ),
) -> None:
    """Extract deterministic discovery signals for one document."""

    from newsrag.facts import (
        FactExtractionError,
        extract_document_facts,
        format_fact_extraction_result,
    )
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    try:
        result = extract_document_facts(database_path, document_id, persist=persist)
    except FactExtractionError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_fact_extraction_result(result))


@documents_app.command("brief")
def documents_brief_command(ctx: typer.Context, document_id: str) -> None:
    """Generate and show an evidence-backed brief for one document."""

    from newsrag.briefs import BriefError, format_generated_brief, generate_document_brief
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    try:
        brief = generate_document_brief(database_path, document_id)
    except BriefError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_generated_brief(brief))


@enrich_app.command("document")
def enrich_document_command(
    ctx: typer.Context,
    document_id: str,
    response_json: Path = ENRICH_RESPONSE_JSON_OPTION,
) -> None:
    """Run structured enrichment for one document from provider JSON."""

    from newsrag.enrichment import (
        EnrichmentError,
        JsonFileEnrichmentProvider,
        enrich_document,
        format_enrichment_result,
    )
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    try:
        result = enrich_document(
            database_path,
            document_id,
            provider=JsonFileEnrichmentProvider(response_json),
        )
    except EnrichmentError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_enrichment_result(result))


@topics_app.command("list")
def topics_list_command(
    ctx: typer.Context,
    body: str | None = DISCOVERY_BODY_OPTION,
    document_type: str | None = DISCOVERY_DOCUMENT_TYPE_OPTION,
    jurisdiction: str | None = DISCOVERY_JURISDICTION_OPTION,
    source_url: str | None = DISCOVERY_SOURCE_URL_OPTION,
    since: str | None = DISCOVERY_SINCE_OPTION,
    until: str | None = DISCOVERY_UNTIL_OPTION,
    min_confidence: float | None = DISCOVERY_MIN_CONFIDENCE_OPTION,
    limit: int = DISCOVERY_LIMIT_OPTION,
    offset: int = DISCOVERY_OFFSET_OPTION,
) -> None:
    """List corpus topics from discovery records."""

    from newsrag.discovery_browse import (
        TOPIC_ITEM_TYPES,
        DiscoveryBrowseError,
        format_topics_list,
        list_browse_items,
    )
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    filters = _build_discovery_browse_filters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
        min_confidence=min_confidence,
    )
    try:
        page = list_browse_items(
            database_path,
            item_types=TOPIC_ITEM_TYPES,
            filters=filters,
            limit=limit,
            offset=offset,
        )
    except DiscoveryBrowseError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_topics_list(page))


@topics_app.command("show")
def topics_show_command(ctx: typer.Context, item_id: str) -> None:
    """Show one topic discovery record with citations."""

    from newsrag.discovery_browse import DiscoveryBrowseError, format_browse_detail, get_browse_item
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    try:
        item = get_browse_item(database_path, item_id)
    except DiscoveryBrowseError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_browse_detail(item, title="NewsRAG Topic"))


@entities_app.command("list")
def entities_list_command(
    ctx: typer.Context,
    body: str | None = DISCOVERY_BODY_OPTION,
    document_type: str | None = DISCOVERY_DOCUMENT_TYPE_OPTION,
    jurisdiction: str | None = DISCOVERY_JURISDICTION_OPTION,
    source_url: str | None = DISCOVERY_SOURCE_URL_OPTION,
    since: str | None = DISCOVERY_SINCE_OPTION,
    until: str | None = DISCOVERY_UNTIL_OPTION,
    min_confidence: float | None = DISCOVERY_MIN_CONFIDENCE_OPTION,
    limit: int = DISCOVERY_LIMIT_OPTION,
    offset: int = DISCOVERY_OFFSET_OPTION,
) -> None:
    """List corpus entities from discovery records."""

    from newsrag.discovery_browse import (
        ENTITY_ITEM_TYPES,
        DiscoveryBrowseError,
        format_entities_list,
        list_browse_items,
    )
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    filters = _build_discovery_browse_filters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
        min_confidence=min_confidence,
    )
    try:
        page = list_browse_items(
            database_path,
            item_types=ENTITY_ITEM_TYPES,
            filters=filters,
            limit=limit,
            offset=offset,
        )
    except DiscoveryBrowseError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_entities_list(page))


@entities_app.command("show")
def entities_show_command(ctx: typer.Context, item_id: str) -> None:
    """Show one entity discovery record with citations."""

    from newsrag.discovery_browse import DiscoveryBrowseError, format_browse_detail, get_browse_item
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    try:
        item = get_browse_item(database_path, item_id)
    except DiscoveryBrowseError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_browse_detail(item, title="NewsRAG Entity"))


@app.command("timeline")
def timeline_command(
    ctx: typer.Context,
    body: str | None = DISCOVERY_BODY_OPTION,
    document_type: str | None = DISCOVERY_DOCUMENT_TYPE_OPTION,
    jurisdiction: str | None = DISCOVERY_JURISDICTION_OPTION,
    source_url: str | None = DISCOVERY_SOURCE_URL_OPTION,
    since: str | None = DISCOVERY_SINCE_OPTION,
    until: str | None = DISCOVERY_UNTIL_OPTION,
    item_type: str | None = DISCOVERY_ITEM_TYPE_OPTION,
    min_confidence: float | None = DISCOVERY_MIN_CONFIDENCE_OPTION,
    limit: int = DISCOVERY_LIMIT_OPTION,
    offset: int = DISCOVERY_OFFSET_OPTION,
) -> None:
    """Show dated evidence-backed discovery items across the corpus."""

    from newsrag.discovery_browse import (
        TIMELINE_ITEM_TYPES,
        DiscoveryBrowseError,
        format_timeline,
        list_browse_items,
    )
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    filters = _build_discovery_browse_filters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
        item_type=item_type,
        min_confidence=min_confidence,
    )
    try:
        page = list_browse_items(
            database_path,
            item_types=TIMELINE_ITEM_TYPES,
            filters=filters,
            limit=limit,
            offset=offset,
            order_by="timeline",
        )
    except DiscoveryBrowseError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_timeline(page))


@leads_app.command("list")
def leads_list_command(
    ctx: typer.Context,
    body: str | None = DISCOVERY_BODY_OPTION,
    document_type: str | None = DISCOVERY_DOCUMENT_TYPE_OPTION,
    jurisdiction: str | None = DISCOVERY_JURISDICTION_OPTION,
    source_url: str | None = DISCOVERY_SOURCE_URL_OPTION,
    since: str | None = DISCOVERY_SINCE_OPTION,
    until: str | None = DISCOVERY_UNTIL_OPTION,
    min_confidence: float | None = DISCOVERY_MIN_CONFIDENCE_OPTION,
    limit: int = DISCOVERY_LIMIT_OPTION,
    offset: int = DISCOVERY_OFFSET_OPTION,
) -> None:
    """List possible story leads from discovery records."""

    from newsrag.discovery_browse import (
        LEAD_ITEM_TYPES,
        DiscoveryBrowseError,
        format_leads_list,
        list_browse_items,
    )
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    filters = _build_discovery_browse_filters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
        min_confidence=min_confidence,
    )
    try:
        page = list_browse_items(
            database_path,
            item_types=LEAD_ITEM_TYPES,
            filters=filters,
            limit=limit,
            offset=offset,
            order_by="created",
        )
    except DiscoveryBrowseError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_leads_list(page))


@leads_app.command("show")
def leads_show_command(ctx: typer.Context, item_id: str) -> None:
    """Show one story lead with supporting evidence."""

    from newsrag.discovery_browse import DiscoveryBrowseError, format_browse_detail, get_browse_item
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    try:
        item = get_browse_item(database_path, item_id)
    except DiscoveryBrowseError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(format_browse_detail(item, title="NewsRAG Story Lead"))


@jobs_app.command("list")
def jobs_list_command(ctx: typer.Context) -> None:
    """List durable NewsRAG jobs."""

    from newsrag.jobs import list_jobs
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    jobs = list_jobs(database_path)

    typer.echo("NewsRAG Jobs")
    typer.echo(f"data_dir: {settings.data_dir}")
    if not jobs:
        typer.echo("jobs: none")
        return

    for job in jobs:
        typer.echo(_format_job_line(job))


@jobs_app.command("retry")
def jobs_retry_command(ctx: typer.Context, job_id: str) -> None:
    """Retry one failed durable NewsRAG job."""

    from newsrag.jobs import JobRetryError, retry_failed_job
    from newsrag.storage import initialize_storage

    settings, _ = _resolve_runtime_settings(ctx)
    database_path = initialize_storage(settings.data_dir).database
    try:
        job = retry_failed_job(database_path, job_id)
    except JobRetryError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc

    typer.echo(f"Retried {job.id}; status={job.status}")


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

    from newsrag.storage import initialize_storage
    from newsrag.watches import add_watch

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

    from newsrag.storage import initialize_storage
    from newsrag.watches import list_watches

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


def _format_job_line(job: Job) -> str:
    from newsrag.jobs import FAILED

    parts = [job.id, job.status, job.kind, f"updated_at={job.updated_at}"]
    source_path = job.payload.get("path")
    if isinstance(source_path, str) and source_path.strip():
        parts.append(f"path={source_path}")
    if job.status == FAILED and job.error is not None:
        parts.append(f"failed_at={job.updated_at}")
        parts.append(f"error={job.error}")
    elif job.error is not None:
        parts.append(f"error={job.error}")
    return " ".join(parts)


def _build_search_filters(
    *,
    body: str | None,
    document_type: str | None,
    jurisdiction: str | None,
    source_url: str | None,
    since: str | None,
    until: str | None,
) -> SearchFilters:
    from newsrag.search import SearchFilters

    return SearchFilters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
    )


def _build_discovery_browse_filters(
    *,
    body: str | None,
    document_type: str | None,
    jurisdiction: str | None,
    source_url: str | None,
    since: str | None,
    until: str | None,
    item_type: str | None = None,
    min_confidence: float | None = None,
) -> DiscoveryBrowseFilters:
    from newsrag.discovery_browse import DiscoveryBrowseFilters

    return DiscoveryBrowseFilters(
        body=body,
        document_type=document_type,
        jurisdiction=jurisdiction,
        source_url=source_url,
        since=since,
        until=until,
        item_type=item_type,
        min_confidence=min_confidence,
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
