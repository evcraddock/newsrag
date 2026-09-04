from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.discovery import list_discovery_items
from newsrag.facts import (
    FactSource,
    extract_document_facts,
    extract_facts_from_sources,
)
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_deterministic_extractors_create_evidence_backed_fact_drafts() -> None:
    drafts = extract_facts_from_sources(
        [
            FactSource(
                document_id="document-a",
                source_unit_start_id="unit-a-4",
                source_unit_end_id="unit-a-4",
                passage_id="passage-a-4",
                text=(
                    "Council approved a $250,000 stormwater contract with ABC Construction. "
                    "Work must begin by June 1, 2026 and is 95% funded. "
                    "Resolution No. 2026-05 authorizes https://example.test/project."
                ),
            )
        ]
    )
    by_type_label = {(draft.item_type, draft.label): draft for draft in drafts}

    assert ("money", "$250,000") in by_type_label
    assert ("percentage", "95%") in by_type_label
    assert ("date", "June 1, 2026") in by_type_label
    assert ("url", "https://example.test/project") in by_type_label
    assert ("civic_identifier", "Resolution No. 2026-05") in by_type_label
    assert ("topic", "stormwater") in by_type_label
    assert ("topic", "contract") in by_type_label
    assert ("entity", "ABC Construction") in by_type_label
    assert any(draft.item_type == "deadline" for draft in drafts)
    assert any(draft.item_type == "action" and draft.label == "approved" for draft in drafts)

    money = by_type_label[("money", "$250,000")]
    assert money.value == {"amount_text": "$250,000"}
    assert money.confidence == 0.95
    assert money.evidence.document_id == "document-a"
    assert money.evidence.source_unit_start_id == "unit-a-4"
    assert money.evidence.source_unit_end_id == "unit-a-4"
    assert money.evidence.passage_id == "passage-a-4"
    assert money.evidence.validation_status == "validated"
    assert "$250,000 stormwater contract" in money.evidence.quote

    entity = by_type_label[("entity", "ABC Construction")]
    assert entity.confidence == 0.55


def test_false_positive_dates_and_entities_are_filtered_or_low_confidence() -> None:
    drafts = extract_facts_from_sources(
        [
            FactSource(
                document_id="document-a",
                source_unit_start_id="unit-a-1",
                source_unit_end_id="unit-a-1",
                text=(
                    "Page 1 of 2\nCity Manager Report\nInvalid date 2026-99-99. "
                    "Planning Commission reviewed the zoning request."
                ),
            )
        ]
    )

    labels_by_type = {(draft.item_type, draft.label) for draft in drafts}
    entity_confidences = {
        draft.label: draft.confidence for draft in drafts if draft.item_type == "entity"
    }

    assert ("date", "2026-99-99") not in labels_by_type
    assert ("entity", "City Manager Report") not in labels_by_type
    assert entity_confidences["Planning Commission"] == 0.55
    assert ("topic", "zoning") in labels_by_type


def test_extract_document_facts_persists_discovery_items_with_evidence(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_fact_document(data_dir)

    result = extract_document_facts(database_path, "document-a")
    second_result = extract_document_facts(database_path, "document-a")
    items = list_discovery_items(database_path, document_id="document-a")

    assert result.total > 0
    assert len(result.created) > 0
    assert second_result.created == ()
    assert second_result.skipped_existing == second_result.total
    assert len(items) == len(result.created)

    money_items = [item for item in items if item.item_type == "money"]
    assert len(money_items) == 1
    assert money_items[0].label == "$1.2 million"
    assert money_items[0].extractor == "deterministic-civic-facts"
    assert money_items[0].provider == "rules"
    assert money_items[0].model == "rules-v1"
    assert money_items[0].confidence == 0.95
    assert money_items[0].evidence[0].source_unit_start_id == "unit-a-1"
    assert money_items[0].evidence[0].source_unit_end_id == "unit-a-1"
    assert money_items[0].evidence[0].page_id == "page-a-1"
    assert money_items[0].evidence[0].page_start == 1
    assert "$1.2 million" in money_items[0].evidence[0].quote


def test_html_document_facts_use_heading_and_block_evidence(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_html_fact_document(data_dir)

    result = extract_document_facts(database_path, "document-html")
    items = list_discovery_items(database_path, document_id="document-html")

    assert result.total > 0
    assert {item.item_type for item in items} >= {"action", "money", "topic"}
    money = next(item for item in items if item.item_type == "money")
    evidence = money.evidence[0]
    assert evidence.source_unit_start_id == "unit-html-3"
    assert evidence.location_type == "html_block"
    assert evidence.location_label == "Council Update — Parks — block 3"
    assert evidence.page_id is None
    assert evidence.page_start is None


def test_discover_document_command_outputs_extraction_status(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    _seed_fact_document(data_dir)

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "discover", "document", "document-a"],
    )

    assert result.exit_code == 0, result.stdout
    assert "NewsRAG Discovery" in result.stdout
    assert "document_id: document-a" in result.stdout
    assert "created:" in result.stdout
    assert "money: $1.2 million" in result.stdout


def test_discover_document_command_reports_missing_document(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "discover", "document", "document-missing"],
    )

    assert result.exit_code == 1
    assert "Unknown document: document-missing" in result.stdout


def _seed_fact_document(data_dir: Path) -> Path:
    database_path = initialize_storage(data_dir).database
    page_one_text = (
        "Council awarded a $1.2 million sewer infrastructure contract "
        "to ABC Construction. Work must be completed by 2026-06-30."
    )
    page_two_text = "Ordinance 2026-10 sets a public hearing for July 1, 2026."
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-a', 'local_path', '/tmp/council.pdf', '/tmp/council.pdf')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path, acquired_at, state
            )
            VALUES(
                'artifact-a', 'source-a', 'application/pdf', 10, 'hash-a',
                '/tmp/council.pdf', CURRENT_TIMESTAMP, 'published'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, source_path, source_url, title, source_hash, normalized_path,
                metadata_json, artifact_id
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "document-a",
                "/tmp/council.pdf",
                "https://example.test/council.pdf",
                "Council Packet",
                "hash-a",
                "/tmp/council-ocr.pdf",
                '{"body": "City Council", "meeting_date": "2026-05-01"}',
                "artifact-a",
            ),
        )
        connection.executemany(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, ordinal, location_type, location_json,
                human_label, normalized_text, structure_json, extractor
            )
            VALUES(?, 'artifact-a', 'document-a', ?, 'page', ?, ?, ?, '{}', 'pymupdf')
            """,
            [
                ("unit-a-1", 1, '{"page_number": 1}', "p. 1", page_one_text),
                ("unit-a-2", 2, '{"page_number": 2}', "p. 2", page_two_text),
            ],
        )
        connection.executemany(
            """
            INSERT INTO pages(id, document_id, page_number, source_unit_id, text, extractor)
            VALUES(?, 'document-a', ?, ?, ?, 'pymupdf')
            """,
            [
                ("page-a-1", 1, "unit-a-1", page_one_text),
                ("page-a-2", 2, "unit-a-2", page_two_text),
            ],
        )
        connection.commit()
    return database_path


def _seed_html_fact_document(data_dir: Path) -> Path:
    database_path = initialize_storage(data_dir).database
    text = "Council approved a $2 million parks contract by 2026-07-01."
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
                '{"heading_path": ["Council Update", "Parks"]}', 'static-html'
            )
            """,
            (text,),
        )
        connection.commit()
    return database_path
