from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from newsrag.briefs import BriefError, format_generated_brief, generate_document_brief
from newsrag.cli import app
from newsrag.discovery import list_document_briefs
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_generate_document_brief_persists_evidence_backed_summary(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_brief_document(data_dir)

    brief = generate_document_brief(database_path, "document-a")
    persisted_briefs = list_document_briefs(database_path, document_id="document-a")
    output = format_generated_brief(brief)

    assert brief.record.document_id == "document-a"
    assert brief.record.extractor == "deterministic-document-brief"
    assert brief.record.provider == "rules"
    assert brief.record.model == "rules-v1"
    assert brief.record.status == "validated"
    assert "Council Packet" in brief.record.summary
    assert "stormwater" in brief.record.summary
    assert "financial commitments" in brief.record.significance
    assert brief.record.open_questions
    assert persisted_briefs == [brief.record]
    assert len(brief.evidence_lines) > 0
    assert all(line.quote for line in brief.evidence_lines)
    assert any(line.label == "$250,000" for line in brief.evidence_lines)
    assert "p. 1" in output
    assert "Council awarded a $250,000 stormwater contract" in output


def test_documents_brief_command_outputs_citations(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    _seed_brief_document(data_dir)

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "brief", "document-a"],
    )

    assert result.exit_code == 0, result.stdout
    assert "NewsRAG Document Brief" in result.stdout
    assert "document_id: document-a" in result.stdout
    assert "Summary:" in result.stdout
    assert "Notable Evidence:" in result.stdout
    assert "money: $250,000" in result.stdout
    assert "p. 1" in result.stdout
    assert "Open Questions:" in result.stdout


def test_generate_document_brief_fails_for_missing_document(tmp_path: Path) -> None:
    database_path = initialize_storage(tmp_path / ".newsrag").database

    try:
        generate_document_brief(database_path, "document-missing")
    except BriefError as exc:
        assert "Unknown document: document-missing" in str(exc)
    else:  # pragma: no cover - defensive assertion branch
        raise AssertionError("expected BriefError")


def test_documents_brief_command_fails_for_low_text_document(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = initialize_storage(data_dir).database
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO documents(id, source_path, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?)
            """,
            ("document-empty", "/tmp/empty.pdf", "Empty", "hash-empty", "/tmp/empty-ocr.pdf", "{}"),
        )
        connection.execute(
            """
            INSERT INTO pages(id, document_id, page_number, text, extractor)
            VALUES(?, ?, ?, ?, ?)
            """,
            ("page-empty", "document-empty", 1, "short", "pymupdf"),
        )
        connection.commit()

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "documents", "brief", "document-empty"],
    )

    assert result.exit_code == 1
    assert "too little extracted text" in result.stdout


def _seed_brief_document(data_dir: Path) -> Path:
    database_path = initialize_storage(data_dir).database
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO documents(id, source_path, source_url, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "document-a",
                "/tmp/council.pdf",
                "https://example.test/council.pdf",
                "Council Packet",
                "hash-a",
                "/tmp/council-ocr.pdf",
                '{"body": "City Council", "meeting_date": "2026-05-01"}',
            ),
        )
        connection.executemany(
            """
            INSERT INTO pages(id, document_id, page_number, text, extractor)
            VALUES(?, ?, ?, ?, ?)
            """,
            [
                (
                    "page-a-1",
                    "document-a",
                    1,
                    (
                        "Council awarded a $250,000 stormwater contract to ABC Construction. "
                        "Work must be completed by June 1, 2026."
                    ),
                    "pymupdf",
                ),
                (
                    "page-a-2",
                    "document-a",
                    2,
                    "Resolution No. 2026-05 schedules a public hearing for July 1, 2026.",
                    "pymupdf",
                ),
            ],
        )
        connection.commit()
    return database_path
