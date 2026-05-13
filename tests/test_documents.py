from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.documents import (
    DocumentFilters,
    get_document_detail,
    list_document_summaries,
)
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_documents_list_empty_corpus(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(app, ["--data-dir", str(data_dir), "documents", "list"])

    assert result.exit_code == 0
    assert "NewsRAG Documents" in result.stdout
    assert "documents: none" in result.stdout


def test_documents_list_shows_bounded_recent_documents(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_document_inventory(data_dir)

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "list", "--limit", "2"],
    )
    page = list_document_summaries(database_path, limit=2)

    assert result.exit_code == 0, result.stdout
    assert "showing 2 of 3 document(s); limit=2 offset=0" in result.stdout
    assert "more: use --limit/--offset or filters to narrow results" in result.stdout
    assert "document-c | Zoning Packet" in result.stdout
    assert "document-b | Budget Packet" in result.stdout
    assert "document-a | Stormwater Report" not in result.stdout
    assert [document.id for document in page.documents] == ["document-c", "document-b"]
    assert page.total == 3


def test_documents_list_supports_metadata_date_query_and_pagination_filters(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_document_inventory(data_dir)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "documents",
            "list",
            "--body",
            "City Council",
            "--document-type",
            "agenda_packet",
            "--jurisdiction",
            "Example City",
            "--since",
            "2026-04-01",
            "--until",
            "2026-04-30",
            "--query",
            "budget",
            "--limit",
            "1",
            "--offset",
            "0",
        ],
    )
    page = list_document_summaries(
        database_path,
        filters=DocumentFilters(
            body="City Council",
            document_type="agenda_packet",
            jurisdiction="Example City",
            since="2026-04-01",
            until="2026-04-30",
            query="budget",
        ),
        limit=1,
    )

    assert result.exit_code == 0, result.stdout
    assert "document-b | Budget Packet" in result.stdout
    assert "document-a | Stormwater Report" not in result.stdout
    assert [document.id for document in page.documents] == ["document-b"]
    assert page.total == 1


def test_documents_list_rejects_invalid_limit_and_date(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    limit_result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "list", "--limit", "501"],
    )
    date_result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "list", "--since", "04-01-2026"],
    )

    assert limit_result.exit_code == 1
    assert "--limit must be between 1 and 500" in limit_result.stdout
    assert date_result.exit_code == 1
    assert "Invalid --since date" in date_result.stdout


def test_documents_show_outputs_detail_and_page_count(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_document_inventory(data_dir)

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "show", "document-a"],
    )
    detail = get_document_detail(database_path, "document-a")

    assert result.exit_code == 0, result.stdout
    assert "NewsRAG Document" in result.stdout
    assert "id: document-a" in result.stdout
    assert "title: Stormwater Report" in result.stdout
    assert "pages: 2" in result.stdout
    assert "source_url: https://example.test/stormwater.pdf" in result.stdout
    assert "source_path: /tmp/stormwater.pdf" in result.stdout
    assert "source_hash: hash-a" in result.stdout
    assert "normalized_path: /tmp/stormwater-ocr.pdf" in result.stdout
    assert "body: Planning Commission" in result.stdout
    assert "document_type: staff_report" in result.stdout
    assert detail.page_count == 2


def test_documents_show_missing_id_fails_clearly(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "show", "document-missing"],
    )

    assert result.exit_code == 1
    assert "Unknown document: document-missing" in result.stdout


def _seed_document_inventory(data_dir: Path) -> Path:
    database_path = initialize_storage(data_dir).database
    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO documents(
                id,
                source_path,
                source_url,
                title,
                source_hash,
                normalized_path,
                metadata_json,
                created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "document-a",
                    "/tmp/stormwater.pdf",
                    "https://example.test/stormwater.pdf",
                    "Stormwater Report",
                    "hash-a",
                    "/tmp/stormwater-ocr.pdf",
                    '{"body": "Planning Commission", "document_type": "staff_report", "jurisdiction": "Example City", "meeting_date": "2026-03-15", "source_filename": "stormwater.pdf"}',
                    "2026-04-01T00:00:00+00:00",
                ),
                (
                    "document-b",
                    "/tmp/budget.pdf",
                    "https://example.test/budget.pdf",
                    "Budget Packet",
                    "hash-b",
                    "/tmp/budget-ocr.pdf",
                    '{"body": "City Council", "document_type": "agenda_packet", "jurisdiction": "Example City", "meeting_date": "2026-04-20", "source_filename": "budget.pdf"}',
                    "2026-04-02T00:00:00+00:00",
                ),
                (
                    "document-c",
                    "/tmp/zoning.pdf",
                    None,
                    "Zoning Packet",
                    "hash-c",
                    "/tmp/zoning-ocr.pdf",
                    '{"body": "Planning Commission", "document_type": "agenda_packet", "jurisdiction": "Example City", "meeting_date": "2026-05-01", "source_filename": "zoning.pdf"}',
                    "2026-04-03T00:00:00+00:00",
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO pages(id, document_id, page_number, text, extractor)
            VALUES(?, ?, ?, ?, ?)
            """,
            [
                ("page-a-1", "document-a", 1, "Stormwater page one", "pymupdf"),
                ("page-a-2", "document-a", 2, "Stormwater page two", "pymupdf"),
                ("page-b-1", "document-b", 1, "Budget page", "pymupdf"),
            ],
        )
        connection.commit()
    return database_path
