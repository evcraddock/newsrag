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


def test_html_document_brief_uses_block_extent_and_citations(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_html_brief_document(data_dir)

    brief = generate_document_brief(database_path, "document-html")
    output = format_generated_brief(brief)

    assert brief.document.source_type == "html"
    assert brief.document.extent_type == "blocks"
    assert brief.document.extent_count == 1
    assert "source_type: html" in output
    assert "blocks: 1" in output
    assert "pages:" not in output
    assert "Council Update — Stormwater — block 3" in output


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
        _insert_pdf_source_model(
            connection,
            document_id="document-empty",
            artifact_id="artifact-empty",
            source_id="source-empty",
            content_hash="hash-empty",
            source_path="/tmp/empty.pdf",
            title="Empty",
            units=(("unit-empty", 1, "short"),),
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
        _insert_pdf_source_model(
            connection,
            document_id="document-a",
            artifact_id="artifact-a",
            source_id="source-a",
            content_hash="hash-a",
            source_path="/tmp/council.pdf",
            title="Council Packet",
            units=(
                (
                    "unit-a-1",
                    1,
                    "Council awarded a $250,000 stormwater contract to ABC Construction. Work must be completed by June 1, 2026.",
                ),
                (
                    "unit-a-2",
                    2,
                    "Resolution No. 2026-05 schedules a public hearing for July 1, 2026.",
                ),
            ),
            metadata_json='{"body": "City Council", "meeting_date": "2026-05-01"}',
        )
        connection.commit()
    return database_path


def _seed_html_brief_document(data_dir: Path) -> Path:
    database_path = initialize_storage(data_dir).database
    text = (
        "Council approved a $250,000 stormwater contract with ABC Construction. "
        "Work must be completed by June 1, 2026."
    )
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-html', 'local_path', '/tmp/update.html', '/tmp/update.html')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path, acquired_at, state
            )
            VALUES(
                'artifact-html', 'source-html', 'text/html', 10, 'hash-html',
                '/tmp/update.html', CURRENT_TIMESTAMP, 'published'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(id, source_path, title, source_hash, metadata_json, artifact_id)
            VALUES(
                'document-html', '/tmp/update.html', 'Council Update', 'hash-html',
                '{"body": "City Council"}', 'artifact-html'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, ordinal, location_type, location_json,
                human_label, normalized_text, structure_json, extractor
            )
            VALUES(
                'unit-html-3', 'artifact-html', 'document-html', 3, 'html_block',
                '{"block_number": 3}', 'block 3', ?,
                '{"heading_path": ["Council Update", "Stormwater"]}', 'static-html'
            )
            """,
            (text,),
        )
        connection.commit()
    return database_path


def _insert_pdf_source_model(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    artifact_id: str,
    source_id: str,
    content_hash: str,
    source_path: str,
    title: str,
    units: tuple[tuple[str, int, str], ...],
    metadata_json: str = "{}",
) -> None:
    connection.execute(
        """
        INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
        VALUES(?, 'local_path', ?, ?)
        """,
        (source_id, source_path, source_path),
    )
    connection.execute(
        """
        INSERT INTO source_artifacts(
            id, source_id, media_type, byte_size, content_hash, stored_path, acquired_at, state
        )
        VALUES(?, ?, 'application/pdf', 10, ?, ?, CURRENT_TIMESTAMP, 'published')
        """,
        (artifact_id, source_id, content_hash, source_path),
    )
    connection.execute(
        """
        INSERT INTO documents(id, source_path, title, source_hash, metadata_json, artifact_id)
        VALUES(?, ?, ?, ?, ?, ?)
        """,
        (document_id, source_path, title, content_hash, metadata_json, artifact_id),
    )
    connection.executemany(
        """
        INSERT INTO source_units(
            id, artifact_id, document_id, ordinal, location_type, location_json,
            human_label, normalized_text, structure_json, extractor
        )
        VALUES(?, ?, ?, ?, 'page', ?, ?, ?, '{}', 'pymupdf')
        """,
        [
            (
                unit_id,
                artifact_id,
                document_id,
                page_number,
                f'{{"page_number": {page_number}}}',
                f"p. {page_number}",
                text,
            )
            for unit_id, page_number, text in units
        ],
    )
    connection.executemany(
        """
        INSERT INTO pages(id, document_id, page_number, source_unit_id, text, extractor)
        VALUES(?, ?, ?, ?, ?, 'pymupdf')
        """,
        [
            (f"page-{document_id}-{page_number}", document_id, page_number, unit_id, text)
            for unit_id, page_number, text in units
        ],
    )
