from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.discovery import DiscoveryEvidenceDraft, create_discovery_item
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_topics_list_empty_corpus(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(app, ["--data-dir", str(data_dir), "topics", "list"])

    assert result.exit_code == 0
    assert "NewsRAG Topics" in result.stdout
    assert "topics: none" in result.stdout


def test_topics_list_supports_metadata_date_and_confidence_filters(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    _seed_discovery_browse_data(data_dir)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "topics",
            "list",
            "--body",
            "City Council",
            "--document-type",
            "agenda_packet",
            "--jurisdiction",
            "Example City",
            "--source-url",
            "https://example.test/council.pdf",
            "--since",
            "2026-05-01",
            "--until",
            "2026-05-31",
            "--min-confidence",
            "0.55",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "NewsRAG Topics" in result.stdout
    assert "topic-council-contract | topic | contract" in result.stdout
    assert "meeting_date=2026-05-12" in result.stdout
    assert "citation=document-council p.2" in result.stdout
    assert "topic-planning-zoning" not in result.stdout


def test_entities_list_shows_extracted_entities(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    _seed_discovery_browse_data(data_dir)

    result = runner.invoke(app, ["--data-dir", str(data_dir), "entities", "list"])

    assert result.exit_code == 0, result.stdout
    assert "NewsRAG Entities" in result.stdout
    assert "entity-planning-board | entity | Planning Board" in result.stdout
    assert "entity-council-vendor | entity | Acme Construction LLC" in result.stdout


def test_timeline_supports_item_type_filter_and_citation_output(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    _seed_discovery_browse_data(data_dir)

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "timeline",
            "--item-type",
            "deadline",
            "--min-confidence",
            "0.80",
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "NewsRAG Timeline" in result.stdout
    assert "deadline-council | deadline | date=June 1, 2026 | June 1, 2026" in result.stdout
    assert "citation=document-council p.2" in result.stdout
    assert "action-council" not in result.stdout


def test_timeline_rejects_invalid_filters(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    invalid_date_result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "timeline", "--since", "05-01-2026"],
    )
    invalid_confidence_result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "timeline", "--min-confidence", "1.5"],
    )
    invalid_item_type_result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "timeline", "--item-type", "topic"],
    )

    assert invalid_date_result.exit_code == 1
    assert "Invalid --since date" in invalid_date_result.stdout
    assert invalid_confidence_result.exit_code == 1
    assert "--min-confidence must be between 0 and 1" in invalid_confidence_result.stdout
    assert invalid_item_type_result.exit_code == 1
    assert "--item-type must be one of" in invalid_item_type_result.stdout


def test_leads_list_and_show_include_supporting_evidence(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    _seed_discovery_browse_data(data_dir)

    list_result = runner.invoke(app, ["--data-dir", str(data_dir), "leads", "list"])
    show_result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "leads", "show", "lead-council-contract"],
    )

    assert list_result.exit_code == 0, list_result.stdout
    assert "NewsRAG Story Leads" in list_result.stdout
    assert "lead-council-contract | story_lead | Follow stormwater contract" in list_result.stdout
    assert "citation=document-council p.2" in list_result.stdout

    assert show_result.exit_code == 0, show_result.stdout
    assert "NewsRAG Story Lead" in show_result.stdout
    assert "id: lead-council-contract" in show_result.stdout
    assert "label: Follow stormwater contract" in show_result.stdout
    assert "source: https://example.test/council.pdf" in show_result.stdout
    assert "evidence:" in show_result.stdout
    assert "document-council p.2" in show_result.stdout
    assert 'quote: "Council awarded a $250,000 stormwater contract."' in show_result.stdout


def test_leads_show_missing_id_fails_clearly(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(app, ["--data-dir", str(data_dir), "leads", "show", "missing"])

    assert result.exit_code == 1
    assert "Unknown discovery item: missing" in result.stdout


def _seed_discovery_browse_data(data_dir: Path) -> Path:
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
                    "document-council",
                    "/tmp/council.pdf",
                    "https://example.test/council.pdf",
                    "Council Packet",
                    "hash-council",
                    "/tmp/council-ocr.pdf",
                    '{"body": "City Council", "document_type": "agenda_packet", "jurisdiction": "Example City", "meeting_date": "2026-05-12"}',
                    "2026-05-12T00:00:00+00:00",
                ),
                (
                    "document-planning",
                    "/tmp/planning.pdf",
                    "https://example.test/planning.pdf",
                    "Planning Packet",
                    "hash-planning",
                    "/tmp/planning-ocr.pdf",
                    '{"body": "Planning Board", "document_type": "staff_report", "jurisdiction": "Example City", "meeting_date": "2026-04-10"}',
                    "2026-04-10T00:00:00+00:00",
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO pages(id, document_id, page_number, text, extractor)
            VALUES(?, ?, ?, ?, ?)
            """,
            [
                (
                    "page-council-2",
                    "document-council",
                    2,
                    "Council awarded a $250,000 stormwater contract. Work must begin by June 1, 2026.",
                    "pymupdf",
                ),
                (
                    "page-planning-1",
                    "document-planning",
                    1,
                    "Planning Board reviewed the zoning request.",
                    "pymupdf",
                ),
            ],
        )
        connection.commit()

    _create_item(
        database_path,
        document_id="document-council",
        item_id="topic-council-contract",
        item_type="topic",
        label="contract",
        summary="Topic candidate from keyword 'contract'.",
        confidence=0.60,
        page_id="page-council-2",
        page=2,
        quote="Council awarded a $250,000 stormwater contract.",
    )
    _create_item(
        database_path,
        document_id="document-planning",
        item_id="topic-planning-zoning",
        item_type="topic",
        label="zoning",
        summary="Topic candidate from keyword 'zoning'.",
        confidence=0.60,
        page_id="page-planning-1",
        page=1,
        quote="Planning Board reviewed the zoning request.",
    )
    _create_item(
        database_path,
        document_id="document-council",
        item_id="entity-council-vendor",
        item_type="entity",
        label="Acme Construction LLC",
        summary="Entity candidate from capitalized phrase.",
        confidence=0.55,
        page_id="page-council-2",
        page=2,
        quote="Council awarded a $250,000 stormwater contract.",
    )
    _create_item(
        database_path,
        document_id="document-planning",
        item_id="entity-planning-board",
        item_type="entity",
        label="Planning Board",
        summary="Entity candidate from capitalized phrase.",
        confidence=0.55,
        page_id="page-planning-1",
        page=1,
        quote="Planning Board reviewed the zoning request.",
    )
    _create_item(
        database_path,
        document_id="document-council",
        item_id="deadline-council",
        item_type="deadline",
        label="June 1, 2026",
        summary="Deadline-related language appears in document text.",
        confidence=0.82,
        page_id="page-council-2",
        page=2,
        quote="Work must begin by June 1, 2026.",
        value={"deadline": "June 1, 2026"},
    )
    _create_item(
        database_path,
        document_id="document-council",
        item_id="action-council",
        item_type="action",
        label="awarded",
        summary="Action or vote language appears in document text.",
        confidence=0.75,
        page_id="page-council-2",
        page=2,
        quote="Council awarded a $250,000 stormwater contract.",
    )
    _create_item(
        database_path,
        document_id="document-council",
        item_id="lead-council-contract",
        item_type="story_lead",
        label="Follow stormwater contract",
        summary="The vendor and funding source may merit follow-up reporting.",
        confidence=0.70,
        page_id="page-council-2",
        page=2,
        quote="Council awarded a $250,000 stormwater contract.",
    )
    return database_path


def _create_item(
    database_path: Path,
    *,
    document_id: str,
    item_id: str,
    item_type: str,
    label: str,
    summary: str,
    confidence: float,
    page_id: str,
    page: int,
    quote: str,
    value: dict[str, object] | None = None,
) -> None:
    create_discovery_item(
        database_path,
        document_id=document_id,
        item_id=item_id,
        item_type=item_type,
        label=label,
        value=value or {},
        summary=summary,
        confidence=confidence,
        extractor="test-extractor",
        provider="rules",
        model="rules-v1",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id=document_id,
                page_id=page_id,
                page_start=page,
                page_end=page,
                quote=quote,
                validation_status="validated",
            ),
        ),
    )
