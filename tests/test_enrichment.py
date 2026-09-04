from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.discovery import list_discovery_items, list_document_briefs
from newsrag.enrichment import (
    EnrichmentError,
    EnrichmentRequest,
    enrich_document,
)
from newsrag.storage import initialize_storage

runner = CliRunner()


@dataclass(frozen=True)
class FakeEnrichmentProvider:
    response: str
    expected_document_id: str = "document-a"
    name: str = "fake-llm"
    model: str = "fake-model"

    def enrich(self, request: EnrichmentRequest) -> str:
        assert request.document_id == self.expected_document_id
        assert request.evidence_contexts
        return self.response


def test_structured_enrichment_persists_brief_items_and_evidence(tmp_path: Path) -> None:
    database_path = _seed_enrichment_document(tmp_path / ".newsrag")
    response = _valid_enrichment_response()

    result = enrich_document(
        database_path,
        "document-a",
        provider=FakeEnrichmentProvider(json.dumps(response)),
    )
    briefs = list_document_briefs(database_path, document_id="document-a")
    items = list_discovery_items(database_path, document_id="document-a")

    assert result.brief.summary == response["summary"]
    assert result.brief.extractor == "structured-llm-enrichment"
    assert result.brief.provider == "fake-llm"
    assert result.brief.model == "fake-model"
    assert result.brief.status == "validated"
    assert result.brief.open_questions == (
        "Which vendor received the contract?",
        "What funding source pays for the work?",
    )
    assert briefs == [result.brief]

    items_by_type = {item.item_type: item for item in items}
    assert set(items_by_type) == {"summary", "action", "story_lead"}
    assert items_by_type["summary"].summary == response["summary"]
    assert (
        items_by_type["summary"].evidence[0].quote
        == "Council awarded a $250,000 stormwater contract."
    )
    assert items_by_type["action"].label == "Awarded stormwater contract"
    assert items_by_type["action"].evidence[0].page_start == 1
    assert items_by_type["story_lead"].label == "Follow the stormwater contract"
    assert (
        items_by_type["story_lead"].evidence[0].quote
        == "Council awarded a $250,000 stormwater contract."
    )


def test_html_enrichment_creates_story_lead_with_block_evidence(tmp_path: Path) -> None:
    database_path = _seed_html_enrichment_document(tmp_path / ".newsrag")
    response = _valid_enrichment_response()
    summary_evidence = cast(list[dict[str, Any]], response["summary_evidence"])
    summary_evidence[0]["passage_id"] = "passage-html-3"
    for collection_name in ("notable_actions", "story_leads"):
        claims = cast(list[dict[str, Any]], response[collection_name])
        evidence = cast(dict[str, Any], claims[0]["evidence"])
        evidence["source_unit_start_id"] = "unit-html-3"

    result = enrich_document(
        database_path,
        "document-html",
        provider=FakeEnrichmentProvider(
            json.dumps(response),
            expected_document_id="document-html",
        ),
    )

    story_lead = next(item for item in result.items if item.item_type == "story_lead")
    story_evidence = story_lead.evidence[0]
    assert story_evidence.location_type == "html_block"
    assert story_evidence.location_label == "Council Update — Stormwater — block 3"
    assert story_evidence.page_start is None
    assert story_evidence.page_id is None


def test_structured_enrichment_rejects_malformed_json(tmp_path: Path) -> None:
    database_path = _seed_enrichment_document(tmp_path / ".newsrag")

    with pytest.raises(EnrichmentError, match="malformed JSON"):
        enrich_document(
            database_path,
            "document-a",
            provider=FakeEnrichmentProvider("not json"),
        )


def test_structured_enrichment_rejects_invalid_schema(tmp_path: Path) -> None:
    database_path = _seed_enrichment_document(tmp_path / ".newsrag")
    response = _valid_enrichment_response()
    del response["summary_evidence"]

    with pytest.raises(EnrichmentError, match="summary_evidence"):
        enrich_document(
            database_path,
            "document-a",
            provider=FakeEnrichmentProvider(json.dumps(response)),
        )


def test_structured_enrichment_rejects_unsupported_quotes(tmp_path: Path) -> None:
    database_path = _seed_enrichment_document(tmp_path / ".newsrag")
    response = _valid_enrichment_response()
    story_leads = cast(list[dict[str, Any]], response["story_leads"])
    evidence = cast(dict[str, Any], story_leads[0]["evidence"])
    evidence["quote"] = "This quote is not in the document."

    with pytest.raises(EnrichmentError, match="Unsupported quote"):
        enrich_document(
            database_path,
            "document-a",
            provider=FakeEnrichmentProvider(json.dumps(response)),
        )


def test_enrich_document_command_uses_json_file_provider(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    _seed_enrichment_document(data_dir)
    response_path = tmp_path / "response.json"
    response_path.write_text(json.dumps(_valid_enrichment_response()), encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "enrich",
            "document",
            "document-a",
            "--response-json",
            str(response_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert "NewsRAG Enrichment" in result.stdout
    assert "document_id: document-a" in result.stdout
    assert "items_created: 3" in result.stdout
    assert "story_lead: Follow the stormwater contract" in result.stdout


def _valid_enrichment_response() -> dict[str, object]:
    return {
        "summary": "The packet centers on a stormwater contract award backed by council action.",
        "summary_evidence": [
            {
                "passage_id": "passage-a-1",
                "quote": "Council awarded a $250,000 stormwater contract.",
            }
        ],
        "notable_actions": [
            {
                "label": "Awarded stormwater contract",
                "summary": "Council awarded the stormwater contract.",
                "evidence": {
                    "source_unit_start_id": "unit-a-1",
                    "quote": "Council awarded a $250,000 stormwater contract.",
                },
            }
        ],
        "story_leads": [
            {
                "label": "Follow the stormwater contract",
                "summary": "The contract amount and deadline may merit follow-up reporting.",
                "evidence": {
                    "source_unit_start_id": "unit-a-1",
                    "quote": "Council awarded a $250,000 stormwater contract.",
                },
            }
        ],
        "open_questions": [
            "Which vendor received the contract?",
            "What funding source pays for the work?",
        ],
    }


def _seed_enrichment_document(data_dir: Path) -> Path:
    database_path = initialize_storage(data_dir).database
    text = "Council awarded a $250,000 stormwater contract. Work must begin by June 1, 2026."
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
        connection.execute(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, ordinal, location_type, location_json,
                human_label, normalized_text, structure_json, extractor
            )
            VALUES(
                'unit-a-1', 'artifact-a', 'document-a', 1, 'page',
                '{"page_number": 1}', 'p. 1', ?, '{}', 'pymupdf'
            )
            """,
            (text,),
        )
        connection.execute(
            """
            INSERT INTO pages(id, document_id, page_number, source_unit_id, text, extractor)
            VALUES('page-a-1', 'document-a', 1, 'unit-a-1', ?, 'pymupdf')
            """,
            (text,),
        )
        connection.execute(
            """
            INSERT INTO chunks(
                id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, text
            )
            VALUES(
                'chunk-a-1', 'document-a', 1, 1, 'unit-a-1', 'unit-a-1',
                'Council awarded a $250,000 stormwater contract.'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO passages(
                id, chunk_id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, ordinal, text
            )
            VALUES(
                'passage-a-1', 'chunk-a-1', 'document-a', 1, 1,
                'unit-a-1', 'unit-a-1', 1,
                'Council awarded a $250,000 stormwater contract.'
            )
            """
        )
        connection.commit()
    return database_path


def _seed_html_enrichment_document(data_dir: Path) -> Path:
    database_path = initialize_storage(data_dir).database
    text = "Council awarded a $250,000 stormwater contract. Work must begin by June 1, 2026."
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
        connection.execute(
            """
            INSERT INTO chunks(
                id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, text
            )
            VALUES(
                'chunk-html-3', 'document-html', 3, 3, 'unit-html-3', 'unit-html-3', ?
            )
            """,
            (text,),
        )
        connection.execute(
            """
            INSERT INTO passages(
                id, chunk_id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, ordinal, text
            )
            VALUES(
                'passage-html-3', 'chunk-html-3', 'document-html', 3, 3,
                'unit-html-3', 'unit-html-3', 1, ?
            )
            """,
            (text,),
        )
        connection.commit()
    return database_path
