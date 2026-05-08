from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


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
)
DATABASE_FILENAME = "newsrag.sqlite3"
SCHEMA_VERSION = "1"
REQUIRED_TABLES = {
    "documents",
    "pages",
    "chunks",
    "chunks_fts",
    "jobs",
    "watches",
    "watch_files",
    "embedding_records",
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
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        source_path TEXT,
        source_url TEXT,
        title TEXT,
        source_hash TEXT,
        normalized_path TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pages (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        page_number INTEGER NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(document_id) REFERENCES documents(id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS chunks (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        page_start INTEGER NOT NULL,
        page_end INTEGER NOT NULL,
        text TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(document_id) REFERENCES documents(id)
    )
    """,
    """
    CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
        chunk_id UNINDEXED,
        text
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        status TEXT NOT NULL,
        payload_json TEXT NOT NULL DEFAULT '{}',
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
        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        database=data_dir / DATABASE_FILENAME,
    )


def initialize_storage(data_dir: Path) -> StoragePaths:
    """Create or update the NewsRAG storage layout for one data directory."""

    paths = build_storage_paths(data_dir)
    _validate_storage_target(paths.data_dir)
    paths.data_dir.mkdir(parents=True, exist_ok=True)

    for directory in _iter_directories(paths):
        directory.mkdir(parents=True, exist_ok=True)

    _initialize_database(paths.database)
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

    return StorageStatusReport(checks=tuple(checks))


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
    )


def _iter_directories(paths: StoragePaths) -> tuple[Path, ...]:
    return tuple(directory for _, directory in _directory_checks(paths))


def _initialize_database(database_path: Path) -> None:
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        for statement in SCHEMA_STATEMENTS:
            connection.execute(statement)
        _ensure_column(connection, "documents", "source_hash", "TEXT")
        _ensure_column(connection, "documents", "normalized_path", "TEXT")
        _ensure_column(connection, "documents", "metadata_json", "TEXT NOT NULL DEFAULT '{}'")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_documents_source_hash ON documents(source_hash)"
        )
        connection.execute(
            """
            INSERT INTO chunks_fts(chunk_id, text)
            SELECT chunks.id, chunks.text
            FROM chunks
            WHERE chunks.id NOT IN (SELECT chunk_id FROM chunks_fts)
            """
        )
        connection.execute(
            "INSERT OR IGNORE INTO metadata(key, value) VALUES(?, ?)",
            ("schema_version", SCHEMA_VERSION),
        )
        connection.commit()


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
