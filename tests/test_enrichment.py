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
    name: str = "fake-llm"
    model: str = "fake-model"

    def enrich(self, request: EnrichmentRequest) -> str:
        assert request.document_id == "document-a"
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
                "page_start": 1,
                "quote": "Council awarded a $250,000 stormwater contract.",
            }
        ],
        "notable_actions": [
            {
                "label": "Awarded stormwater contract",
                "summary": "Council awarded the stormwater contract.",
                "evidence": {
                    "page_start": 1,
                    "quote": "Council awarded a $250,000 stormwater contract.",
                },
            }
        ],
        "story_leads": [
            {
                "label": "Follow the stormwater contract",
                "summary": "The contract amount and deadline may merit follow-up reporting.",
                "evidence": {
                    "page_start": 1,
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
        connection.execute(
            """
            INSERT INTO pages(id, document_id, page_number, text, extractor)
            VALUES(?, ?, ?, ?, ?)
            """,
            (
                "page-a-1",
                "document-a",
                1,
                "Council awarded a $250,000 stormwater contract. Work must begin by June 1, 2026.",
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
                "Council awarded a $250,000 stormwater contract.",
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
                "Council awarded a $250,000 stormwater contract.",
            ),
        )
        connection.commit()
    return database_path
