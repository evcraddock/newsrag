from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.storage import REQUIRED_TABLES, StorageError, _existing_tables, initialize_storage

runner = CliRunner()


def test_initialize_storage_creates_layout_and_schema(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"

    paths = initialize_storage(data_dir)

    assert paths.data_dir == data_dir
    assert paths.source_pdfs.is_dir()
    assert paths.downloaded_pdfs.is_dir()
    assert paths.ocr_pdfs.is_dir()
    assert paths.lancedb.is_dir()
    assert paths.logs.is_dir()
    assert paths.artifacts.is_dir()
    assert paths.database.is_file()
    assert REQUIRED_TABLES.issubset(_existing_tables(paths.database))


def test_initialize_storage_is_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"

    first_paths = initialize_storage(data_dir)
    second_paths = initialize_storage(data_dir)

    assert first_paths == second_paths
    assert REQUIRED_TABLES == REQUIRED_TABLES.intersection(_existing_tables(second_paths.database))


def test_initialize_storage_backfills_passages_from_existing_chunks(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)

    with sqlite3.connect(paths.database) as connection:
        connection.execute(
            """
            INSERT INTO documents(id, source_path, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            (
                "document-1",
                "/tmp/report.pdf",
                "Report",
                "hash-1",
                "/tmp/report-ocr.pdf",
                "{}",
            ),
        )
        connection.execute(
            """
            INSERT INTO chunks(id, document_id, page_start, page_end, text)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                "chunk-1",
                "document-1",
                10,
                10,
                "City Manager's Report\nMay 1, 2026\nPage 10 of 16\n\n• Paperbacks & Playdates Book Club – A low-pressure book club.\n\n• Brown Bag Book Club – Bring your own lunch.",
            ),
        )
        connection.commit()

    initialize_storage(data_dir)

    with sqlite3.connect(paths.database) as connection:
        passage_rows = connection.execute(
            "SELECT id, text FROM passages ORDER BY ordinal ASC"
        ).fetchall()
        fts_rows = connection.execute(
            "SELECT passage_id, text FROM passages_fts ORDER BY passage_id ASC"
        ).fetchall()

    assert len(passage_rows) == 2
    assert "Paperbacks & Playdates Book Club" in passage_rows[0][1]
    assert "Brown Bag Book Club" in passage_rows[1][1]
    assert len(fts_rows) == 2


def test_initialize_storage_rejects_file_path(tmp_path: Path) -> None:
    data_dir = tmp_path / "not-a-directory"
    data_dir.write_text("x", encoding="utf-8")

    with pytest.raises(StorageError):
        initialize_storage(data_dir)


def test_initialize_storage_rejects_unwritable_parent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "nested" / ".newsrag"

    def fake_access(path: object, mode: int) -> bool:
        del mode
        candidate = Path(str(path))
        if candidate == tmp_path:
            return False
        return True

    monkeypatch.setattr("newsrag.storage.os.access", fake_access)

    with pytest.raises(StorageError):
        initialize_storage(data_dir)


def test_status_command_reports_storage_health(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"

    first_result = runner.invoke(app, ["--data-dir", str(data_dir), "status"])
    second_result = runner.invoke(app, ["--data-dir", str(data_dir), "status", "--initialize"])
    third_result = runner.invoke(app, ["--data-dir", str(data_dir), "status"])

    assert first_result.exit_code == 0
    assert "NewsRAG Status" in first_result.stdout
    assert "summary: warn" in first_result.stdout
    assert f"data_dir: {data_dir}" in first_result.stdout

    assert second_result.exit_code == 0
    assert "summary: ok" in second_result.stdout

    assert third_result.exit_code == 0
    assert "database: ok" in third_result.stdout
    assert "source_pdfs: ok" in third_result.stdout
    assert "summary: ok" in third_result.stdout
