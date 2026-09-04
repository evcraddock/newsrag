from __future__ import annotations

import re
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


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


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


def test_documents_list_filters_on_or_after_ingestion_date(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_document_inventory(data_dir)

    page = list_document_summaries(
        database_path,
        filters=DocumentFilters(ingested_since="2026-04-02"),
    )

    assert [document.id for document in page.documents] == ["document-c", "document-b"]


def test_documents_list_filters_on_or_before_entire_ingestion_day(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_document_inventory(data_dir)

    page = list_document_summaries(
        database_path,
        filters=DocumentFilters(ingested_until="2026-04-02"),
    )

    assert [document.id for document in page.documents] == ["document-b", "document-a"]
    assert page.documents[0].created_at == "2026-04-02T23:59:59+00:00"


def test_documents_list_filters_within_ingestion_date_range(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_document_inventory(data_dir)

    page = list_document_summaries(
        database_path,
        filters=DocumentFilters(
            ingested_since="2026-04-02",
            ingested_until="2026-04-02",
        ),
    )

    assert [document.id for document in page.documents] == ["document-b"]


def test_documents_list_combines_ingestion_and_existing_filters(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_document_inventory(data_dir)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "documents",
            "list",
            "--ingested-since",
            "2026-04-03",
            "--body",
            "Planning Commission",
            "--query",
            "zoning",
        ],
    )
    page = list_document_summaries(
        database_path,
        filters=DocumentFilters(
            body="Planning Commission",
            query="zoning",
            ingested_since="2026-04-03",
        ),
    )

    assert result.exit_code == 0, result.stdout
    assert "document-c | Zoning Packet" in result.stdout
    assert [document.id for document in page.documents] == ["document-c"]


def test_documents_list_ingestion_filters_can_return_empty_results(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    _seed_document_inventory(data_dir)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "documents",
            "list",
            "--ingested-since",
            "2030-01-01",
        ],
    )

    assert result.exit_code == 0
    assert "documents: none" in result.stdout


def test_documents_list_rejects_invalid_and_reversed_ingestion_dates(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    invalid_result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "documents",
            "list",
            "--ingested-since",
            "04-01-2026",
        ],
    )
    reversed_result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "documents",
            "list",
            "--ingested-since",
            "2026-04-03",
            "--ingested-until",
            "2026-04-02",
        ],
    )

    assert invalid_result.exit_code == 1
    assert "Invalid --ingested-since date: expected YYYY-MM-DD" in invalid_result.stdout
    assert reversed_result.exit_code == 1
    assert (
        "Invalid ingestion date range: --ingested-since must be on or before --ingested-until"
    ) in reversed_result.stdout


def test_documents_list_help_distinguishes_ingestion_and_meeting_dates() -> None:
    result = runner.invoke(app, ["documents", "list", "--help"])
    output = _strip_ansi(result.stdout)

    assert result.exit_code == 0
    assert "--ingested-since" in output
    assert "--ingested-until" in output
    assert "Only list documents ingested on or after" in output
    assert "YYYY-MM-DD (UTC)." in output
    assert "Only list documents with meeting dates on" in output


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


def test_mixed_source_inventory_shows_typed_extents_and_filters(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_mixed_document_inventory(data_dir)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "documents",
            "list",
            "--source-type",
            "html",
            "--body",
            "City Council",
        ],
    )
    page = list_document_summaries(
        database_path,
        filters=DocumentFilters(source_type="html", body="City Council"),
    )
    detail_result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "show", "document-html"],
    )

    assert result.exit_code == 0, result.stdout
    assert "document-html | Web Notice" in result.stdout
    assert "source_type=html" in result.stdout
    assert "blocks=3" in result.stdout
    assert "pages=" not in result.stdout
    assert [document.id for document in page.documents] == ["document-html"]
    assert page.documents[0].source_type == "html"
    assert page.documents[0].extent_label == "blocks"
    assert page.documents[0].extent_count == 3
    assert page.documents[0].page_count is None
    assert detail_result.exit_code == 0, detail_result.stdout
    assert "source_type: html" in detail_result.stdout
    assert "blocks: 3" in detail_result.stdout
    assert "pages:" not in detail_result.stdout


def test_mixed_source_inventory_preserves_pdf_page_extents(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_mixed_document_inventory(data_dir)

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "list", "--source-type", "pdf"],
    )
    detail = get_document_detail(database_path, "document-a")

    assert result.exit_code == 0, result.stdout
    assert "document-a | Stormwater Report" in result.stdout
    assert "source_type=pdf" in result.stdout
    assert "pages=2" in result.stdout
    assert "document-html" not in result.stdout
    assert detail.source_type == "pdf"
    assert detail.extent_label == "pages"
    assert detail.extent_count == 2
    assert detail.page_count == 2


def test_documents_list_rejects_unsupported_source_type(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "list", "--source-type", "docx"],
    )

    assert result.exit_code == 1
    assert "Unsupported --source-type 'docx'; expected one of: html, pdf" in result.stdout


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
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES(?, 'local_path', ?, ?)
            """,
            [
                ("source-a", "/tmp/stormwater.pdf", "/tmp/stormwater.pdf"),
                ("source-b", "/tmp/budget.pdf", "/tmp/budget.pdf"),
                ("source-c", "/tmp/zoning.pdf", "/tmp/zoning.pdf"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path, acquired_at, state
            )
            VALUES(?, ?, 'application/pdf', 10, ?, ?, CURRENT_TIMESTAMP, 'published')
            """,
            [
                ("artifact-a", "source-a", "hash-a", "/tmp/stormwater.pdf"),
                ("artifact-b", "source-b", "hash-b", "/tmp/budget.pdf"),
                ("artifact-c", "source-c", "hash-c", "/tmp/zoning.pdf"),
            ],
        )
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
                artifact_id,
                created_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    "artifact-a",
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
                    "artifact-b",
                    "2026-04-02T23:59:59+00:00",
                ),
                (
                    "document-c",
                    "/tmp/zoning.pdf",
                    None,
                    "Zoning Packet",
                    "hash-c",
                    "/tmp/zoning-ocr.pdf",
                    '{"body": "Planning Commission", "document_type": "agenda_packet", "jurisdiction": "Example City", "meeting_date": "2026-05-01", "source_filename": "zoning.pdf"}',
                    "artifact-c",
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


def _seed_mixed_document_inventory(data_dir: Path) -> Path:
    database_path = _seed_document_inventory(data_dir)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-html', 'local_path', '/tmp/notice.html', '/tmp/notice.html')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path, acquired_at, state
            )
            VALUES(
                'artifact-html', 'source-html', 'application/xhtml+xml', 20, 'hash-html',
                '/tmp/notice.html', CURRENT_TIMESTAMP, 'published'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, source_path, title, source_hash, metadata_json, artifact_id, created_at
            )
            VALUES(
                'document-html', '/tmp/notice.html', 'Web Notice', 'hash-html',
                '{"body": "City Council", "document_type": "notice", "jurisdiction": "Example City"}',
                'artifact-html', '2026-04-04T00:00:00+00:00'
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, ordinal, location_type, location_json,
                human_label, normalized_text, structure_json, extractor
            )
            VALUES(?, 'artifact-html', 'document-html', ?, 'html_block', ?, ?, ?, '{}', 'static-html')
            """,
            [
                ("html-unit-1", 1, '{"block_number": 1}', "block 1", "Web Notice"),
                ("html-unit-2", 2, '{"block_number": 2}', "block 2", "Budget"),
                ("html-unit-3", 3, '{"block_number": 3}', "block 3", "Public update"),
            ],
        )
        connection.commit()
    return database_path
