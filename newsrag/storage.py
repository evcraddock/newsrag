from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import lancedb  # type: ignore[import-untyped]
import pyarrow as pa

from newsrag.jobs import DONE, FAILED, PENDING, RUNNING
from newsrag.passages import build_passage_rows
from newsrag.sources import (
    PAGE_LOCATION_TYPE,
    PDF_MEDIA_TYPE,
    artifact_id_for_hash,
    build_source_identity,
    source_unit_id_for_page,
)


class StorageError(Exception):
    """Raised when the NewsRAG storage layout cannot be initialized or inspected."""


@dataclass(frozen=True)
class StoragePaths:
    """Resolved storage paths for one NewsRAG data directory."""

    data_dir: Path
    source_pdfs: Path
    downloaded_pdfs: Path
    ocr_pdfs: Path
    lancedb: Path
    logs: Path
    artifacts: Path
    source_artifacts: Path
    artifact_staging: Path
    database: Path


@dataclass(frozen=True)
class StorageCheck:
    """One storage health check result."""

    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class StorageStatusReport:
    """Structured storage status output."""

    checks: tuple[StorageCheck, ...]

    @property
    def has_errors(self) -> bool:
        return any(check.status == "error" for check in self.checks)

    @property
    def has_warnings(self) -> bool:
        return any(check.status == "warn" for check in self.checks)

    @property
    def summary(self) -> str:
        if self.has_errors:
            return "error"
        if self.has_warnings:
            return "warn"
        return "ok"


DIRECTORY_NAMES: tuple[tuple[str, str], ...] = (
    ("source_pdfs", "source-pdfs"),
    ("downloaded_pdfs", "downloaded-pdfs"),
    ("ocr_pdfs", "ocr-pdfs"),
    ("lancedb", "lancedb"),
    ("logs", "logs"),
    ("artifacts", "artifacts"),
    ("source_artifacts", "artifacts/sources"),
    ("artifact_staging", "artifacts/staging"),
)
DATABASE_FILENAME = "newsrag.sqlite3"
SCHEMA_VERSION = "5"
REQUIRED_TABLES = {
    "sources",
    "source_artifacts",
    "source_units",
    "documents",
    "pages",
    "chunks",
    "chunks_fts",
    "passages",
    "passages_fts",
    "jobs",
    "watches",
    "watch_files",
    "embedding_records",
    "document_profiles",
    "document_briefs",
    "document_briefs_fts",
    "discovery_items",
    "discovery_items_fts",
    "discovery_evidence",
    "metadata",
}
SCHEMA_STATEMENTS = (
    """
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS sources (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        submitted_reference TEXT NOT NULL,
        normalized_reference TEXT NOT NULL,
        resolved_reference TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(kind, normalized_reference)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_artifacts (
        id TEXT PRIMARY KEY,
        source_id TEXT NOT NULL,
        media_type TEXT NOT NULL,
        byte_size INTEGER,
        content_hash TEXT NOT NULL UNIQUE,
        stored_path TEXT NOT NULL,
        acquired_at TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'processing',
        reported_media_type TEXT,
        provenance_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(source_id) REFERENCES sources(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        source_path TEXT,
        source_url TEXT,
        title TEXT,
        source_hash TEXT,
        normalized_path TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        artifact_id TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(artifact_id) REFERENCES source_artifacts(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pages (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        source_unit_id TEXT,
        text TEXT NOT NULL DEFAULT '',
        extractor TEXT NOT NULL DEFAULT 'unknown',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(document_id) REFERENCES documents(id),
        FOREIGN KEY(source_unit_id) REFERENCES source_units(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS source_units (
        id TEXT PRIMARY KEY,
        artifact_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL,
        location_type TEXT NOT NULL,
        location_json TEXT NOT NULL DEFAULT '{}',
        human_label TEXT NOT NULL,
        normalized_text TEXT NOT NULL DEFAULT '',
        structure_json TEXT NOT NULL DEFAULT '{}',
        extractor TEXT NOT NULL,
        extractor_version TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(artifact_id) REFERENCES source_artifacts(id),
        FOREIGN KEY(document_id) REFERENCES documents(id),
        UNIQUE(document_id, ordinal)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        page_start INTEGER NOT NULL,
        page_end INTEGER NOT NULL,
        source_unit_start_id TEXT,
        source_unit_end_id TEXT,
        text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(document_id) REFERENCES documents(id),
        FOREIGN KEY(source_unit_start_id) REFERENCES source_units(id),
        FOREIGN KEY(source_unit_end_id) REFERENCES source_units(id)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        chunk_id UNINDEXED,
        text
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS passages (
        id TEXT PRIMARY KEY,
        chunk_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        page_start INTEGER NOT NULL,
        page_end INTEGER NOT NULL,
        source_unit_start_id TEXT,
        source_unit_end_id TEXT,
        ordinal INTEGER NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(chunk_id) REFERENCES chunks(id),
        FOREIGN KEY(document_id) REFERENCES documents(id),
        FOREIGN KEY(source_unit_start_id) REFERENCES source_units(id),
        FOREIGN KEY(source_unit_end_id) REFERENCES source_units(id)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS passages_fts USING fts5(
        passage_id UNINDEXED,
        text,
        tokenize='porter unicode61'
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
        result_json TEXT,
        error TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watches (
        id TEXT PRIMARY KEY,
        path TEXT NOT NULL UNIQUE,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS watch_files (
        path TEXT PRIMARY KEY,
        watch_id TEXT NOT NULL,
        content_signature TEXT NOT NULL,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(watch_id) REFERENCES watches(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS embedding_records (
        id TEXT PRIMARY KEY,
        source_kind TEXT NOT NULL,
        source_key TEXT NOT NULL,
        provider TEXT NOT NULL,
        model TEXT NOT NULL,
        version TEXT NOT NULL,
        dimensions INTEGER NOT NULL,
        source_unit_start_id TEXT,
        source_unit_end_id TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(source_unit_start_id) REFERENCES source_units(id),
        FOREIGN KEY(source_unit_end_id) REFERENCES source_units(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_profiles (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL UNIQUE,
        source_type TEXT NOT NULL,
        extent_type TEXT NOT NULL,
        extent_count INTEGER NOT NULL,
        text_length INTEGER NOT NULL,
        extraction_quality_json TEXT NOT NULL DEFAULT '{}',
        extractor TEXT NOT NULL,
        provider TEXT,
        model TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(document_id) REFERENCES documents(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS document_briefs (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '',
        significance TEXT NOT NULL DEFAULT '',
        open_questions_json TEXT NOT NULL DEFAULT '[]',
        extractor TEXT NOT NULL,
        provider TEXT,
        model TEXT,
        status TEXT NOT NULL DEFAULT 'draft',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(document_id) REFERENCES documents(id)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS document_briefs_fts USING fts5(
        brief_id UNINDEXED,
        summary,
        significance
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS discovery_items (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        item_type TEXT NOT NULL,
        label TEXT NOT NULL,
        value_json TEXT NOT NULL DEFAULT '{}',
        summary TEXT NOT NULL DEFAULT '',
        confidence REAL,
        extractor TEXT NOT NULL,
        provider TEXT,
        model TEXT,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(document_id) REFERENCES documents(id)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS discovery_items_fts USING fts5(
        item_id UNINDEXED,
        label,
        summary
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS discovery_evidence (
        id TEXT PRIMARY KEY,
        item_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        source_unit_start_id TEXT NOT NULL,
        source_unit_end_id TEXT NOT NULL,
        location_type TEXT NOT NULL,
        location_label TEXT NOT NULL,
        page_id TEXT,
        passage_id TEXT,
        page_start INTEGER,
        page_end INTEGER,
        quote TEXT NOT NULL DEFAULT '',
        validation_status TEXT NOT NULL,
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(item_id) REFERENCES discovery_items(id),
        FOREIGN KEY(document_id) REFERENCES documents(id),
        FOREIGN KEY(source_unit_start_id) REFERENCES source_units(id),
        FOREIGN KEY(source_unit_end_id) REFERENCES source_units(id),
        FOREIGN KEY(page_id) REFERENCES pages(id),
        FOREIGN KEY(passage_id) REFERENCES passages(id)
    )
    """,
)


def build_storage_paths(data_dir: Path) -> StoragePaths:
    """Build the expected storage paths for one data directory."""

    directory_paths = {name: data_dir / relative_path for name, relative_path in DIRECTORY_NAMES}
    return StoragePaths(
        data_dir=data_dir,
        source_pdfs=directory_paths["source_pdfs"],
        downloaded_pdfs=directory_paths["downloaded_pdfs"],
        ocr_pdfs=directory_paths["ocr_pdfs"],
        lancedb=directory_paths["lancedb"],
        logs=directory_paths["logs"],
        artifacts=directory_paths["artifacts"],
        source_artifacts=directory_paths["source_artifacts"],
        artifact_staging=directory_paths["artifact_staging"],
        database=data_dir / DATABASE_FILENAME,
    )


def initialize_storage(data_dir: Path) -> StoragePaths:
    """Create or update the NewsRAG storage layout for one data directory."""

    paths = build_storage_paths(data_dir)
    _validate_storage_target(paths.data_dir)
    paths.data_dir.mkdir(parents=True, exist_ok=True)

    for directory in _iter_directories(paths):
        directory.mkdir(parents=True, exist_ok=True)

    vector_migration_required, removed_document_ids = _initialize_database(paths.database)
    if removed_document_ids:
        _delete_document_vectors(paths.lancedb, removed_document_ids)
    if vector_migration_required:
        _delete_orphan_document_vectors(paths.database, paths.lancedb)
        _backfill_vector_source_unit_ranges(paths.database, paths.lancedb)
    _set_schema_version(paths.database)
    return paths


def get_storage_status(data_dir: Path) -> StorageStatusReport:
    """Inspect the current storage layout without mutating it."""

    checks: list[StorageCheck] = []
    paths = build_storage_paths(data_dir)

    if paths.data_dir.exists() and not paths.data_dir.is_dir():
        return StorageStatusReport(
            checks=(
                StorageCheck(
                    "data_dir",
                    "error",
                    f"{paths.data_dir} exists but is not a directory",
                ),
            )
        )

    target = paths.data_dir if paths.data_dir.exists() else _nearest_existing_parent(paths.data_dir)
    if target is None:
        checks.append(
            StorageCheck("data_dir", "error", f"cannot resolve parent for {paths.data_dir}")
        )
        return StorageStatusReport(checks=tuple(checks))

    if not os.access(target, os.W_OK | os.X_OK):
        checks.append(StorageCheck("data_dir", "error", f"{target} is not writable"))
        return StorageStatusReport(checks=tuple(checks))

    if paths.data_dir.exists():
        checks.append(StorageCheck("data_dir", "ok", f"{paths.data_dir} exists"))
    else:
        checks.append(
            StorageCheck(
                "data_dir",
                "warn",
                f"{paths.data_dir} does not exist yet; parent {target} is writable",
            )
        )

    for name, directory in _directory_checks(paths):
        if directory.exists() and directory.is_dir():
            checks.append(StorageCheck(name, "ok", f"present at {directory}"))
        elif directory.exists():
            checks.append(StorageCheck(name, "error", f"{directory} exists but is not a directory"))
        else:
            checks.append(StorageCheck(name, "warn", f"missing directory {directory}"))

    if not paths.database.exists():
        checks.append(StorageCheck("database", "warn", f"missing database {paths.database}"))
        return StorageStatusReport(checks=tuple(checks))

    if not paths.database.is_file():
        checks.append(
            StorageCheck("database", "error", f"{paths.database} exists but is not a file")
        )
        return StorageStatusReport(checks=tuple(checks))

    missing_tables = REQUIRED_TABLES.difference(_existing_tables(paths.database))
    if missing_tables:
        table_list = ", ".join(sorted(missing_tables))
        checks.append(StorageCheck("database", "warn", f"missing tables: {table_list}"))
    else:
        checks.append(StorageCheck("database", "ok", f"schema ready at {paths.database}"))
        checks.append(_job_queue_check(paths.database))
        checks.append(_watcher_health_check(paths.database))

    return StorageStatusReport(checks=tuple(checks))


def _job_queue_check(database_path: Path) -> StorageCheck:
    counts = _job_status_counts(database_path)
    detail = (
        f"pending={counts[PENDING]} running={counts[RUNNING]} "
        f"failed={counts[FAILED]} done={counts[DONE]}"
    )
    if counts[FAILED] > 0:
        return StorageCheck("jobs", "warn", detail)
    return StorageCheck("jobs", "ok", detail)


def _job_status_counts(database_path: Path) -> dict[str, int]:
    counts = {PENDING: 0, RUNNING: 0, FAILED: 0, DONE: 0}
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT status, COUNT(*) FROM jobs GROUP BY status ORDER BY status ASC"
        ).fetchall()

    for status, count in rows:
        counts[str(status)] = int(count)
    return counts


def _watcher_health_check(database_path: Path) -> StorageCheck:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute("SELECT path FROM watches ORDER BY path ASC").fetchall()

    if not rows:
        return StorageCheck("watcher", "ok", "no watched folders configured")

    problems: list[str] = []
    for (raw_path,) in rows:
        path = Path(str(raw_path))
        if not path.exists():
            problems.append(
                f"missing watched folder {path}; recreate it or remove/re-add the watch"
            )
        elif not path.is_dir():
            problems.append(f"watched path {path} is not a directory; remove/re-add the watch")
        elif not os.access(path, os.R_OK | os.X_OK):
            problems.append(f"watched folder {path} is not readable; fix permissions")

    if problems:
        return StorageCheck("watcher", "warn", "; ".join(problems))
    return StorageCheck("watcher", "ok", f"{len(rows)} watched folder(s) ready")


def format_status_report(report: StorageStatusReport, *, data_dir: Path) -> str:
    """Format storage status for terminal output."""

    lines = [
        "NewsRAG Status",
        f"data_dir: {data_dir}",
    ]

    for check in report.checks:
        lines.append(f"{check.name}: {check.status} - {check.detail}")

    lines.append(f"summary: {report.summary}")
    return "\n".join(lines)


def _directory_checks(paths: StoragePaths) -> tuple[tuple[str, Path], ...]:
    return (
        ("source_pdfs", paths.source_pdfs),
        ("downloaded_pdfs", paths.downloaded_pdfs),
        ("ocr_pdfs", paths.ocr_pdfs),
        ("lancedb", paths.lancedb),
        ("logs", paths.logs),
        ("artifacts", paths.artifacts),
        ("source_artifacts", paths.source_artifacts),
        ("artifact_staging", paths.artifact_staging),
    )


def _iter_directories(paths: StoragePaths) -> tuple[Path, ...]:
    return tuple(directory for _, directory in _directory_checks(paths))


def _initialize_database(database_path: Path) -> tuple[bool, tuple[str, ...]]:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        previous_schema_version = _read_schema_version(connection)
        if previous_schema_version in {"1", "2", "3", "4"}:
            _reset_derived_discovery_schema(connection)
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        _ensure_column(connection, "documents", "source_hash", "TEXT")
        _ensure_column(connection, "documents", "normalized_path", "TEXT")
        _ensure_column(connection, "documents", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(
            connection,
            "documents",
            "artifact_id",
            "TEXT REFERENCES source_artifacts(id)",
        )
        _ensure_column(
            connection,
            "source_artifacts",
            "state",
            "TEXT NOT NULL DEFAULT 'processing'",
        )
        _ensure_column(connection, "jobs", "result_json", "TEXT")
        _ensure_column(connection, "source_artifacts", "reported_media_type", "TEXT")
        _ensure_column(
            connection,
            "source_artifacts",
            "provenance_json",
            "TEXT NOT NULL DEFAULT '{}'",
        )
        _ensure_column(connection, "pages", "extractor", "TEXT NOT NULL DEFAULT 'unknown'")
        _ensure_column(
            connection,
            "pages",
            "source_unit_id",
            "TEXT REFERENCES source_units(id)",
        )
        for table_name in ("chunks", "passages", "embedding_records"):
            _ensure_column(
                connection,
                table_name,
                "source_unit_start_id",
                "TEXT REFERENCES source_units(id)",
            )
            _ensure_column(
                connection,
                table_name,
                "source_unit_end_id",
                "TEXT REFERENCES source_units(id)",
            )
        _ensure_column(connection, "document_profiles", "provider", "TEXT")
        _ensure_column(connection, "document_briefs", "provider", "TEXT")
        _ensure_column(connection, "discovery_items", "provider", "TEXT")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_source_hash ON documents(source_hash)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_artifact_id ON documents(artifact_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_source_units_document_ordinal "
            "ON source_units(document_id, ordinal)"
        )
        connection.execute(
            """
            INSERT INTO chunks_fts(chunk_id, text)
            SELECT chunks.id, chunks.text
            FROM chunks
            WHERE chunks.id NOT IN (SELECT chunk_id FROM chunks_fts)
            """
        )
        _backfill_passages(connection)
        _backfill_pdf_source_model(connection)
        removed_document_ids = _consolidate_duplicate_documents(connection)
        connection.execute(
            """
            UPDATE source_artifacts
            SET state = 'published'
            WHERE id IN (
                SELECT artifact_id
                FROM documents
                WHERE artifact_id IS NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_documents_unique_artifact
            ON documents(artifact_id)
            WHERE artifact_id IS NOT NULL
            """
        )
        connection.execute(
            """
            INSERT INTO passages_fts(passage_id, text)
            SELECT passages.id, passages.text
            FROM passages
            WHERE passages.id NOT IN (SELECT passage_id FROM passages_fts)
            """
        )
        connection.commit()

    migration_required = previous_schema_version != SCHEMA_VERSION
    return migration_required, removed_document_ids


def _read_schema_version(connection: sqlite3.Connection) -> str | None:
    metadata_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'metadata'"
    ).fetchone()
    if metadata_exists is None:
        return None
    row = connection.execute("SELECT value FROM metadata WHERE key = 'schema_version'").fetchone()
    if row is None:
        return None
    return str(row[0])


def _reset_derived_discovery_schema(connection: sqlite3.Connection) -> None:
    """Reset regenerable discovery records when adopting a new discovery schema."""

    for table_name in (
        "discovery_evidence",
        "discovery_items_fts",
        "discovery_items",
        "document_briefs_fts",
        "document_briefs",
        "document_profiles",
    ):
        connection.execute(f"DROP TABLE IF EXISTS {table_name}")


def _set_schema_version(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO metadata(key, value) VALUES(?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
            """,
            ("schema_version", SCHEMA_VERSION),
        )
        connection.commit()


def _backfill_vector_source_unit_ranges(database_path: Path, lancedb_path: Path) -> None:
    database = lancedb.connect(lancedb_path)
    table_specs = (
        ("chunk_embeddings", "chunk_id", "chunks"),
        ("passage_embeddings", "passage_id", "passages"),
    )
    for table_name, key_column, sqlite_table in table_specs:
        try:
            table = database.open_table(table_name)
        except ValueError:
            continue

        missing_fields = [
            pa.field(column_name, pa.string())
            for column_name in ("source_unit_start_id", "source_unit_end_id")
            if column_name not in table.schema.names
        ]
        if missing_fields:
            table.add_columns(missing_fields)

        with sqlite3.connect(database_path) as connection:
            rows = connection.execute(
                f"""
                SELECT id, source_unit_start_id, source_unit_end_id
                FROM {sqlite_table}
                WHERE source_unit_start_id IS NOT NULL
                    AND source_unit_end_id IS NOT NULL
                ORDER BY id ASC
                """
            ).fetchall()

        for source_key, source_unit_start_id, source_unit_end_id in rows:
            escaped_key = str(source_key).replace("'", "''")
            table.update(
                where=(
                    f"{key_column} = '{escaped_key}' AND "
                    "(source_unit_start_id IS NULL OR source_unit_end_id IS NULL)"
                ),
                values={
                    "source_unit_start_id": str(source_unit_start_id),
                    "source_unit_end_id": str(source_unit_end_id),
                },
            )


def _backfill_pdf_source_model(connection: sqlite3.Connection) -> None:
    document_rows = connection.execute(
        """
        SELECT
            id,
            source_path,
            source_url,
            source_hash,
            metadata_json,
            created_at
        FROM documents
        WHERE artifact_id IS NULL
            AND source_hash IS NOT NULL
            AND (source_path IS NOT NULL OR source_url IS NOT NULL)
        ORDER BY created_at ASC, id ASC
        """
    ).fetchall()

    for row in document_rows:
        document_id = str(row[0])
        raw_source_path = str(row[1]) if row[1] is not None else ""
        source_url = str(row[2]) if row[2] is not None else None
        content_hash = str(row[3])
        metadata = _load_metadata_json(row[4])
        stored_path = _artifact_stored_path(metadata, raw_source_path)
        if stored_path is None:
            continue

        source_identity = build_source_identity(
            source_path=Path(raw_source_path),
            source_url=source_url,
        )
        artifact_id = artifact_id_for_hash(content_hash)
        acquired_at = _metadata_text(metadata, "retrieved_at") or str(row[5])
        byte_size = _artifact_byte_size(metadata, stored_path)

        connection.execute(
            """
            INSERT OR IGNORE INTO sources(
                id,
                kind,
                submitted_reference,
                normalized_reference,
                resolved_reference
            )
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                source_identity.id,
                source_identity.kind,
                source_identity.submitted_reference,
                source_identity.normalized_reference,
                source_identity.resolved_reference,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO source_artifacts(
                id,
                source_id,
                media_type,
                byte_size,
                content_hash,
                stored_path,
                acquired_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                artifact_id,
                source_identity.id,
                PDF_MEDIA_TYPE,
                byte_size,
                content_hash,
                stored_path,
                acquired_at,
            ),
        )
        connection.execute(
            "UPDATE documents SET artifact_id = ? WHERE id = ?",
            (artifact_id, document_id),
        )

    page_rows = connection.execute(
        """
        SELECT
            pages.id,
            pages.document_id,
            pages.page_number,
            pages.text,
            pages.extractor,
            documents.artifact_id
        FROM pages
        JOIN documents ON documents.id = pages.document_id
        WHERE pages.source_unit_id IS NULL
            AND documents.artifact_id IS NOT NULL
        ORDER BY pages.document_id ASC, pages.page_number ASC, pages.id ASC
        """
    ).fetchall()
    for row in page_rows:
        page_id = str(row[0])
        document_id = str(row[1])
        page_number = int(row[2])
        unit_id = source_unit_id_for_page(page_id)
        connection.execute(
            """
            INSERT OR IGNORE INTO source_units(
                id,
                artifact_id,
                document_id,
                ordinal,
                location_type,
                location_json,
                human_label,
                normalized_text,
                structure_json,
                extractor
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                unit_id,
                str(row[5]),
                document_id,
                page_number,
                PAGE_LOCATION_TYPE,
                json.dumps({"page_number": page_number}, sort_keys=True),
                f"p. {page_number}",
                str(row[3]),
                str(row[4]),
            ),
        )
        connection.execute(
            "UPDATE pages SET source_unit_id = ? WHERE id = ?",
            (unit_id, page_id),
        )

    for table_name in ("chunks", "passages"):
        connection.execute(
            f"""
            UPDATE {table_name}
            SET source_unit_start_id = COALESCE(
                    source_unit_start_id,
                    (
                        SELECT pages.source_unit_id
                        FROM pages
                        WHERE pages.document_id = {table_name}.document_id
                            AND pages.page_number = {table_name}.page_start
                    )
                ),
                source_unit_end_id = COALESCE(
                    source_unit_end_id,
                    (
                        SELECT pages.source_unit_id
                        FROM pages
                        WHERE pages.document_id = {table_name}.document_id
                            AND pages.page_number = {table_name}.page_end
                    )
                )
            WHERE document_id IN (
                    SELECT documents.id
                    FROM documents
                    JOIN source_artifacts
                        ON source_artifacts.id = documents.artifact_id
                    WHERE source_artifacts.media_type = ?
                )
                AND (source_unit_start_id IS NULL OR source_unit_end_id IS NULL)
            """,
            (PDF_MEDIA_TYPE,),
        )

    for source_kind, table_name in (("chunk", "chunks"), ("passage", "passages")):
        connection.execute(
            f"""
            UPDATE embedding_records
            SET source_unit_start_id = COALESCE(
                    source_unit_start_id,
                    (
                        SELECT {table_name}.source_unit_start_id
                        FROM {table_name}
                        WHERE {table_name}.id = embedding_records.source_key
                    )
                ),
                source_unit_end_id = COALESCE(
                    source_unit_end_id,
                    (
                        SELECT {table_name}.source_unit_end_id
                        FROM {table_name}
                        WHERE {table_name}.id = embedding_records.source_key
                    )
                )
            WHERE source_kind = ?
                AND (source_unit_start_id IS NULL OR source_unit_end_id IS NULL)
            """,
            (source_kind,),
        )


def _consolidate_duplicate_documents(connection: sqlite3.Connection) -> tuple[str, ...]:
    rows = connection.execute(
        """
        SELECT
            documents.id,
            COALESCE(source_artifacts.content_hash, documents.source_hash) AS content_hash
        FROM documents
        LEFT JOIN source_artifacts ON source_artifacts.id = documents.artifact_id
        WHERE COALESCE(source_artifacts.content_hash, documents.source_hash) IS NOT NULL
        ORDER BY documents.created_at ASC, documents.id ASC
        """
    ).fetchall()
    seen_hashes: set[str] = set()
    duplicate_ids: list[str] = []
    for document_id, content_hash in rows:
        normalized_hash = str(content_hash)
        if normalized_hash in seen_hashes:
            duplicate_ids.append(str(document_id))
        else:
            seen_hashes.add(normalized_hash)

    for document_id in duplicate_ids:
        _delete_duplicate_document(connection, document_id)

    connection.execute(
        """
        DELETE FROM sources
        WHERE NOT EXISTS (
            SELECT 1
            FROM source_artifacts
            WHERE source_artifacts.source_id = sources.id
        )
        """
    )
    return tuple(duplicate_ids)


def _delete_duplicate_document(connection: sqlite3.Connection, document_id: str) -> None:
    connection.execute(
        """
        DELETE FROM discovery_evidence
        WHERE document_id = ?
            OR item_id IN (SELECT id FROM discovery_items WHERE document_id = ?)
        """,
        (document_id, document_id),
    )
    connection.execute(
        """
        DELETE FROM discovery_items_fts
        WHERE item_id IN (SELECT id FROM discovery_items WHERE document_id = ?)
        """,
        (document_id,),
    )
    connection.execute("DELETE FROM discovery_items WHERE document_id = ?", (document_id,))
    connection.execute(
        """
        DELETE FROM document_briefs_fts
        WHERE brief_id IN (SELECT id FROM document_briefs WHERE document_id = ?)
        """,
        (document_id,),
    )
    connection.execute("DELETE FROM document_briefs WHERE document_id = ?", (document_id,))
    connection.execute("DELETE FROM document_profiles WHERE document_id = ?", (document_id,))
    connection.execute(
        """
        DELETE FROM embedding_records
        WHERE (source_kind = 'chunk' AND source_key IN (
                SELECT id FROM chunks WHERE document_id = ?
            ))
            OR (source_kind = 'passage' AND source_key IN (
                SELECT id FROM passages WHERE document_id = ?
            ))
        """,
        (document_id, document_id),
    )
    connection.execute(
        "DELETE FROM passages_fts WHERE passage_id IN "
        "(SELECT id FROM passages WHERE document_id = ?)",
        (document_id,),
    )
    connection.execute("DELETE FROM passages WHERE document_id = ?", (document_id,))
    connection.execute(
        "DELETE FROM chunks_fts WHERE chunk_id IN (SELECT id FROM chunks WHERE document_id = ?)",
        (document_id,),
    )
    connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))
    connection.execute("DELETE FROM pages WHERE document_id = ?", (document_id,))
    connection.execute("DELETE FROM source_units WHERE document_id = ?", (document_id,))
    connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def _delete_document_vectors(lancedb_path: Path, document_ids: tuple[str, ...]) -> None:
    database = lancedb.connect(lancedb_path)
    for table_name in ("chunk_embeddings", "passage_embeddings"):
        try:
            table = database.open_table(table_name)
        except ValueError:
            continue
        for document_id in document_ids:
            escaped_document_id = document_id.replace("'", "''")
            table.delete(f"document_id = '{escaped_document_id}'")


def _delete_orphan_document_vectors(database_path: Path, lancedb_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        document_ids = {
            str(row[0]) for row in connection.execute("SELECT id FROM documents").fetchall()
        }

    database = lancedb.connect(lancedb_path)
    for table_name in ("chunk_embeddings", "passage_embeddings"):
        try:
            table = database.open_table(table_name)
        except ValueError:
            continue
        orphan_ids = {
            str(row["document_id"])
            for row in table.to_arrow().to_pylist()
            if str(row["document_id"]) not in document_ids
        }
        for document_id in orphan_ids:
            escaped_document_id = document_id.replace("'", "''")
            table.delete(f"document_id = '{escaped_document_id}'")


def _load_metadata_json(raw_metadata: object) -> dict[str, object]:
    if not isinstance(raw_metadata, str):
        return {}
    try:
        metadata = json.loads(raw_metadata)
    except ValueError:
        return {}
    if not isinstance(metadata, dict):
        return {}
    return metadata


def _metadata_text(metadata: dict[str, object], key: str) -> str | None:
    value = metadata.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _artifact_stored_path(metadata: dict[str, object], source_path: str) -> str | None:
    stored_path = _metadata_text(metadata, "stored_source_path")
    if stored_path is not None:
        return stored_path
    if source_path:
        return source_path
    return None


def _artifact_byte_size(metadata: dict[str, object], stored_path: str) -> int | None:
    stored_size = metadata.get("source_size_bytes")
    if isinstance(stored_size, int) and not isinstance(stored_size, bool) and stored_size >= 0:
        return stored_size
    try:
        return Path(stored_path).stat().st_size
    except OSError:
        return None


def _backfill_passages(connection: sqlite3.Connection) -> None:
    rows = connection.execute(
        """
        SELECT id, document_id, page_start, page_end, text
        FROM chunks
        WHERE id NOT IN (SELECT DISTINCT chunk_id FROM passages)
        ORDER BY id ASC
        """
    ).fetchall()
    passage_rows = []
    for row in rows:
        passage_rows.extend(
            build_passage_rows(
                chunk_id=str(row[0]),
                document_id=str(row[1]),
                page_start=int(row[2]),
                page_end=int(row[3]),
                text=str(row[4]),
            )
        )
    if not passage_rows:
        return

    connection.executemany(
        """
        INSERT INTO passages(id, chunk_id, document_id, page_start, page_end, ordinal, text)
        VALUES(?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row.id,
                row.chunk_id,
                row.document_id,
                row.page_start,
                row.page_end,
                row.ordinal,
                row.text,
            )
            for row in passage_rows
        ],
    )


def _ensure_column(
    connection: sqlite3.Connection,
    table_name: str,
    column_name: str,
    column_definition: str,
) -> None:
    existing_columns = {
        str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    }
    if column_name in existing_columns:
        return
    connection.execute(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {column_definition}")


def _existing_tables(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        cursor = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
        rows = cursor.fetchall()
    return {row[0] for row in rows}


def _validate_storage_target(data_dir: Path) -> None:
    if data_dir.exists() and not data_dir.is_dir():
        raise StorageError(f"{data_dir} exists but is not a directory")

    target = data_dir if data_dir.exists() else _nearest_existing_parent(data_dir)
    if target is None:
        raise StorageError(f"cannot resolve writable parent for {data_dir}")
    if not os.access(target, os.W_OK | os.X_OK):
        raise StorageError(f"{target} is not writable")


def _nearest_existing_parent(path: Path) -> Path | None:
    current = path
    while not current.exists():
        parent = current.parent
        if parent == current:
            return None
        current = parent
    return current
