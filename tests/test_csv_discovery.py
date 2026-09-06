from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_text_ingestion as support
from test_enrichment import FakeEnrichmentProvider

from newsrag.discovery import DiscoveryError, DiscoveryEvidenceDraft, create_discovery_item
from newsrag.documents import format_document_detail, get_document_detail
from newsrag.enrichment import EnrichmentError, enrich_document
from newsrag.facts import extract_document_facts
from newsrag.tabular import TableRegion


def test_csv_facts_are_exact_values_not_inferred_money_dates_or_totals(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "expenses.csv"
    source.write_text("Date,Amount\n2026-01-01,001200\n2026-02-01,300\n")
    job = corpus.ingest(str(source))
    assert job.status == "done" and job.result is not None, job.error
    document_id = str(job.result["document_id"])
    facts = extract_document_facts(corpus.paths.database, document_id)
    assert facts.total == 2
    assert {item.item_type for item in facts.created} == {"table_values"}
    assert all(item.evidence[0].table_region is not None for item in facts.created)
    assert all(item.evidence[0].table_context for item in facts.created)
    assert extract_document_facts(corpus.paths.database, document_id).skipped_existing == 2
    inventory = get_document_detail(corpus.paths.database, document_id)
    assert (
        inventory.source_type == "csv"
        and inventory.extent_label == "rows"
        and inventory.extent_count == 3
    )
    assert inventory.table_descriptors[0]["column_end"] == 2
    assert "sheets: 1" in format_document_detail(inventory)
    assert "pages:" not in format_document_detail(inventory)


def test_csv_discovery_explicit_selector_rejects_other_column(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "expenses.csv"
    source.write_text("Name,Amount\nRoads,001200\n")
    job = corpus.ingest(str(source))
    assert job.status == "done" and job.result is not None, job.error
    document_id = str(job.result["document_id"])
    unit = str(corpus.rows("SELECT id FROM source_units WHERE ordinal=2")[0][0])
    region = TableRegion("sheet-1", 1, "cells", 2, 2, 2, 2)
    with pytest.raises(DiscoveryError):
        create_discovery_item(
            corpus.paths.database,
            document_id=document_id,
            item_type="table_value",
            label="Amount",
            extractor="test",
            evidence=(
                DiscoveryEvidenceDraft(
                    document_id, unit, unit, "Roads", "validated", table_region=region
                ),
            ),
        )
    assert corpus.rows("SELECT * FROM discovery_items") == []
    item = create_discovery_item(
        corpus.paths.database,
        document_id=document_id,
        item_type="table_value",
        label="B2",
        extractor="test",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id, unit, unit, "001200", "validated", table_region=region
            ),
        ),
    )
    assert item.evidence[0].table_region == region


def test_csv_enrichment_rejects_context_substrings_and_preserves_selectors(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "expenses.csv"
    source.write_text("Name,Amount\nRoads,001200\nParks,5\n")
    job = corpus.ingest(str(source))
    assert job.status == "done" and job.result is not None, job.error
    document_id = str(job.result["document_id"])
    passage = str(corpus.rows("SELECT passage_id FROM table_passages ORDER BY focus_text")[0][0])
    response = {
        "summary": 'B2=string:"001200"',
        "summary_evidence": [{"passage_id": passage, "quote": "Parks"}],
        "notable_actions": [],
        "story_leads": [],
        "open_questions": [],
    }
    with pytest.raises(EnrichmentError, match="Unsupported quote"):
        enrich_document(
            corpus.paths.database,
            document_id,
            provider=FakeEnrichmentProvider(json.dumps(response), document_id),
        )
    assert corpus.rows("SELECT * FROM document_briefs") == []
    response["summary_evidence"] = [{"passage_id": passage, "quote": 'B2=string:"001200"'}]
    enriched = enrich_document(
        corpus.paths.database,
        document_id,
        provider=FakeEnrichmentProvider(json.dumps(response), document_id),
    )
    assert enriched.items[0].evidence[0].table_region is not None
