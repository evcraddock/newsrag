from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from newsrag.discovery import (
    DiscoveryError,
    DiscoveryEvidenceDraft,
    create_discovery_item,
    create_document_brief,
    create_document_profile,
    get_document_profile,
    list_discovery_items,
    list_document_briefs,
)
from newsrag.storage import REQUIRED_TABLES, _existing_tables, initialize_storage


def test_initialize_storage_creates_discovery_tables_idempotently(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"

    first_paths = initialize_storage(data_dir)
    second_paths = initialize_storage(data_dir)

    expected_tables = {
        "document_profiles",
        "document_briefs",
        "document_briefs_fts",
        "discovery_items",
        "discovery_items_fts",
        "discovery_evidence",
    }
    existing_tables = _existing_tables(second_paths.database)

    assert first_paths == second_paths
    assert expected_tables.issubset(existing_tables)
    assert expected_tables.issubset(REQUIRED_TABLES)


def test_document_profile_can_be_created_and_replaced(tmp_path: Path) -> None:
    database_path = _seed_discovery_document(tmp_path)

    first_profile = create_document_profile(
        database_path,
        document_id="document-a",
        page_count=2,
        text_length=42,
        extraction_quality={"empty_pages": 0},
        extractor="deterministic",
        provider="rules",
        model="rules-v1",
        profile_id="profile-a",
    )
    second_profile = create_document_profile(
        database_path,
        document_id="document-a",
        page_count=3,
        text_length=84,
        extraction_quality={"empty_pages": 1},
        extractor="deterministic",
        provider="rules",
        model="rules-v2",
        profile_id="profile-b",
    )
    loaded_profile = get_document_profile(database_path, "document-a")

    assert first_profile.id == "profile-a"
    assert second_profile.id == "profile-a"
    assert loaded_profile.page_count == 3
    assert loaded_profile.text_length == 84
    assert loaded_profile.extraction_quality == {"empty_pages": 1}
    assert loaded_profile.extractor == "deterministic"
    assert loaded_profile.provider == "rules"
    assert loaded_profile.model == "rules-v2"


def test_document_brief_persists_provider_identity_and_fts(tmp_path: Path) -> None:
    database_path = _seed_discovery_document(tmp_path)

    brief = create_document_brief(
        database_path,
        document_id="document-a",
        summary="Stormwater funding moved forward.",
        significance="Potential infrastructure story.",
        open_questions=("What contract funds the project?",),
        extractor="local-llm",
        provider="ollama",
        model="llama3.1",
        status="validated",
        brief_id="brief-a",
    )
    briefs = list_document_briefs(database_path, document_id="document-a")

    with sqlite3.connect(database_path) as connection:
        fts_rows = connection.execute(
            "SELECT brief_id, summary, significance FROM document_briefs_fts"
        ).fetchall()

    assert brief.id == "brief-a"
    assert brief.extractor == "local-llm"
    assert brief.provider == "ollama"
    assert brief.model == "llama3.1"
    assert brief.status == "validated"
    assert brief.open_questions == ("What contract funds the project?",)
    assert briefs == [brief]
    assert fts_rows == [
        ("brief-a", "Stormwater funding moved forward.", "Potential infrastructure story.")
    ]


def test_discovery_item_persists_evidence_references_and_identity(
    tmp_path: Path,
) -> None:
    database_path = _seed_discovery_document(tmp_path)

    item = create_discovery_item(
        database_path,
        document_id="document-a",
        item_type="money",
        label="$250,000 stormwater contract",
        value={"amount": 250000, "currency": "USD"},
        summary="The packet references a stormwater contract amount.",
        confidence=0.92,
        extractor="deterministic-money",
        provider="rules",
        model="rules-v1",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id="document-a",
                page_id="page-a-1",
                passage_id="passage-a-1",
                page_start=1,
                page_end=1,
                quote="Approve a $250,000 stormwater contract.",
                validation_status="validated",
            ),
        ),
        item_id="discovery-a",
    )
    listed_items = list_discovery_items(database_path, document_id="document-a", item_type="money")

    with sqlite3.connect(database_path) as connection:
        fts_rows = connection.execute(
            "SELECT item_id, label, summary FROM discovery_items_fts"
        ).fetchall()

    assert item.id == "discovery-a"
    assert item.item_type == "money"
    assert item.value == {"amount": 250000, "currency": "USD"}
    assert item.confidence == 0.92
    assert item.extractor == "deterministic-money"
    assert item.provider == "rules"
    assert item.model == "rules-v1"
    assert len(item.evidence) == 1
    assert item.evidence[0].document_id == "document-a"
    assert item.evidence[0].page_id == "page-a-1"
    assert item.evidence[0].passage_id == "passage-a-1"
    assert item.evidence[0].page_start == 1
    assert item.evidence[0].page_end == 1
    assert item.evidence[0].quote == "Approve a $250,000 stormwater contract."
    assert item.evidence[0].validation_status == "validated"
    assert listed_items == [item]
    assert fts_rows == [
        (
            "discovery-a",
            "$250,000 stormwater contract",
            "The packet references a stormwater contract amount.",
        )
    ]


def test_discovery_item_validates_confidence_and_evidence_pages(tmp_path: Path) -> None:
    database_path = _seed_discovery_document(tmp_path)

    with pytest.raises(DiscoveryError, match="confidence must be between 0 and 1"):
        create_discovery_item(
            database_path,
            document_id="document-a",
            item_type="topic",
            label="Stormwater",
            confidence=1.5,
            extractor="deterministic",
        )

    with pytest.raises(DiscoveryError, match="evidence.page_end"):
        create_discovery_item(
            database_path,
            document_id="document-a",
            item_type="topic",
            label="Stormwater",
            extractor="deterministic",
            evidence=(
                DiscoveryEvidenceDraft(
                    document_id="document-a",
                    page_start=2,
                    page_end=1,
                    quote="Stormwater",
                    validation_status="validated",
                ),
            ),
        )

    with pytest.raises(DiscoveryError, match="evidence.document_id must match"):
        create_discovery_item(
            database_path,
            document_id="document-a",
            item_type="topic",
            label="Stormwater",
            extractor="deterministic",
            evidence=(
                DiscoveryEvidenceDraft(
                    document_id="document-other",
                    page_start=1,
                    page_end=1,
                    quote="Stormwater",
                    validation_status="validated",
                ),
            ),
        )

    with pytest.raises(DiscoveryError, match="evidence.quote must be non-empty"):
        create_discovery_item(
            database_path,
            document_id="document-a",
            item_type="topic",
            label="Stormwater",
            extractor="deterministic",
            evidence=(
                DiscoveryEvidenceDraft(
                    document_id="document-a",
                    page_start=1,
                    page_end=1,
                    quote="",
                    validation_status="validated",
                ),
            ),
        )


def _seed_discovery_document(tmp_path: Path) -> Path:
    database_path = initialize_storage(tmp_path / ".newsrag").database
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO documents(id, source_path, source_url, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "document-a",
                "/tmp/stormwater.pdf",
                "https://example.test/stormwater.pdf",
                "Stormwater Packet",
                "hash-a",
                "/tmp/stormwater-ocr.pdf",
                '{"body": "Planning Commission", "meeting_date": "2026-05-01"}',
            ),
        )
        connection.execute(
            """
            INSERT INTO pages(id, document_id, page_number, text, extractor)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                "page-a-1",
                "document-a",
                1,
                "Approve a $250,000 stormwater contract.",
                "pymupdf",
            ),
        )
        connection.execute(
            """
            INSERT INTO chunks(id, document_id, page_start, page_end, text)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                "chunk-a-1",
                "document-a",
                1,
                1,
                "Approve a $250,000 stormwater contract.",
            ),
        )
        connection.execute(
            """
            INSERT INTO passages(id, chunk_id, document_id, page_start, page_end, ordinal, text)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "passage-a-1",
                "chunk-a-1",
                "document-a",
                1,
                1,
                1,
                "Approve a $250,000 stormwater contract.",
            ),
        )
        connection.commit()
    return database_path
