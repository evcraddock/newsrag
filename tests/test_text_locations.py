from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsrag.briefs import format_generated_brief, generate_document_brief
from newsrag.cli import app
from newsrag.discovery import (
    DiscoveryEvidenceDraft,
    create_discovery_item,
    create_document_profile,
)
from newsrag.discovery_browse import (
    TOPIC_ITEM_TYPES,
    DiscoveryBrowseFilters,
    format_browse_detail,
    format_topics_list,
    get_browse_item,
    list_browse_items,
)
from newsrag.documents import DocumentFilters, get_document_detail, list_document_summaries
from newsrag.enrichment import EnrichmentRequest, enrich_document, format_enrichment_result
from newsrag.facts import extract_document_facts, format_fact_extraction_result
from newsrag.packets import PacketSourceProvenance, format_source_packet
from newsrag.search import (
    SearchFilters,
    merge_search_candidates,
    search_keyword_candidates,
)
from newsrag.source_locations import (
    SourceLocationError,
    load_document_extent,
    resolve_source_range,
)
from newsrag.storage import initialize_storage

runner = CliRunner()


@dataclass(frozen=True)
class _TextEnrichmentProvider:
    name: str = "test-provider"
    model: str = "test-model"

    def enrich(self, request: EnrichmentRequest) -> str:
        assert {context.location_label for context in request.evidence_contexts} >= {
            "line 1",
            "line 2",
            "line 3",
        }
        return json.dumps(
            {
                "summary": "Council approved a stormwater contract.",
                "summary_evidence": [
                    {
                        "passage_id": "passage-text-1",
                        "quote": "Council approved a $250,000 stormwater contract",
                    }
                ],
                "notable_actions": [],
                "story_leads": [],
                "open_questions": [],
            }
        )


def test_text_extent_and_range_include_blank_physical_lines(tmp_path: Path) -> None:
    database_path = _seed_text_corpus(tmp_path / ".newsrag")

    with sqlite3.connect(database_path) as connection:
        extent = load_document_extent(connection, "document-text")
        resolved = resolve_source_range(
            connection,
            document_id="document-text",
            source_unit_start_id="unit-text-1",
            source_unit_end_id="unit-text-3",
        )

    assert extent.source_type == "text"
    assert extent.extent_type == "lines"
    assert extent.extent_count == 3
    assert extent.text_length == len(_TEXT_LINE_ONE) + len(_TEXT_LINE_THREE)
    assert resolved.location_type == "text_line"
    assert resolved.location_label == "lines 1–3"
    assert resolved.text == f"{_TEXT_LINE_ONE}\n\n{_TEXT_LINE_THREE}"
    assert resolved.page_id is None
    assert resolved.page_start is None
    assert resolved.page_end is None


def test_text_range_rejects_missing_physical_line(tmp_path: Path) -> None:
    database_path = _seed_text_corpus(tmp_path / ".newsrag")
    with sqlite3.connect(database_path) as connection:
        connection.execute("DELETE FROM source_units WHERE id = 'unit-text-2'")
        with pytest.raises(SourceLocationError, match="range is incomplete"):
            resolve_source_range(
                connection,
                document_id="document-text",
                source_unit_start_id="unit-text-1",
                source_unit_end_id="unit-text-3",
            )


def test_mixed_search_and_inventory_use_text_types_and_line_citations(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    database_path = _seed_text_corpus(data_dir, include_pdf=True)

    candidates = search_keyword_candidates(database_path, "stormwater", limit=10)
    all_results = merge_search_candidates(
        candidates,
        [],
        database_path=database_path,
        limit=10,
        keyword_weight=0.6,
        vector_weight=0.4,
    )
    text_results = merge_search_candidates(
        candidates,
        [],
        database_path=database_path,
        limit=10,
        keyword_weight=0.6,
        vector_weight=0.4,
        filters=SearchFilters(source_type="text"),
    )
    inventory = list_document_summaries(database_path)
    text_inventory = list_document_summaries(
        database_path,
        filters=DocumentFilters(source_type="text", query="notes"),
    )
    detail = get_document_detail(database_path, "document-text")

    assert {result.source_type for result in all_results} == {"pdf", "text"}
    assert [result.passage_id for result in text_results] == ["passage-text-1"]
    assert text_results[0].citation == "Council Notes — 2026-06-01 — line 1"
    assert {document.source_type for document in inventory.documents} == {"pdf", "text"}
    assert [document.id for document in text_inventory.documents] == ["document-text"]
    assert text_inventory.documents[0].extent_label == "lines"
    assert text_inventory.documents[0].extent_count == 3
    assert text_inventory.documents[0].page_count is None
    assert detail.source_type == "text"
    assert detail.extent_label == "lines"
    assert detail.extent_count == 3
    assert detail.page_count is None


def test_text_discovery_profile_facts_and_browse_preserve_line_ranges(tmp_path: Path) -> None:
    database_path = _seed_text_corpus(tmp_path / ".newsrag")

    profile = create_document_profile(
        database_path,
        document_id="document-text",
        text_length=len(_TEXT_LINE_ONE) + len(_TEXT_LINE_THREE),
        extractor="deterministic",
    )
    range_item = create_discovery_item(
        database_path,
        document_id="document-text",
        item_type="topic",
        label="stormwater schedule",
        extractor="deterministic",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id="document-text",
                source_unit_start_id="unit-text-1",
                source_unit_end_id="unit-text-3",
                quote="stormwater contract with ABC Construction. Work must be completed",
                validation_status="validated",
            ),
        ),
        item_id="discovery-text-range",
    )
    fact_result = extract_document_facts(database_path, "document-text")
    fact_output = format_fact_extraction_result(fact_result)
    browse_page = list_browse_items(
        database_path,
        item_types=TOPIC_ITEM_TYPES,
        filters=DiscoveryBrowseFilters(),
    )
    browse_output = format_topics_list(browse_page)
    detail_output = format_browse_detail(get_browse_item(database_path, range_item.id))

    assert profile.source_type == "text"
    assert profile.extent_type == "lines"
    assert profile.extent_count == 3
    assert range_item.evidence[0].location_type == "text_line"
    assert range_item.evidence[0].location_label == "lines 1–3"
    assert range_item.evidence[0].page_id is None
    assert range_item.evidence[0].page_start is None
    assert "line 1" in fact_output
    assert "p." not in fact_output
    assert "document-text lines 1–3" in browse_output
    assert "document-text lines 1–3" in detail_output


def test_text_brief_enrichment_and_packet_format_without_invented_pages(tmp_path: Path) -> None:
    database_path = _seed_text_corpus(tmp_path / ".newsrag")
    extract_document_facts(database_path, "document-text")

    brief = generate_document_brief(database_path, "document-text")
    brief_output = format_generated_brief(brief)
    enrichment = enrich_document(
        database_path,
        "document-text",
        provider=_TextEnrichmentProvider(),
    )
    enrichment_output = format_enrichment_result(enrichment)

    candidates = search_keyword_candidates(database_path, "stormwater", limit=5)
    results = merge_search_candidates(
        candidates,
        [],
        database_path=database_path,
        limit=5,
        keyword_weight=0.6,
        vector_weight=0.4,
    )
    packet = format_source_packet(
        query="stormwater",
        results=results,
        source_provenance={
            "document-text": PacketSourceProvenance(
                document_id="document-text",
                source_type="text",
                source_kind="local_path",
                submitted_reference="/tmp/council-notes.txt",
                resolved_reference="/tmp/council-notes.txt",
                retrieved_at=None,
                artifact_hash="hash-text",
            )
        },
    )

    assert brief.document.source_type == "text"
    assert brief.document.extent_type == "lines"
    assert "lines: 3" in brief_output
    assert "pages:" not in brief_output
    assert "line 1" in brief_output
    assert "line 1" in enrichment_output
    assert "p." not in enrichment_output
    assert "**Council Notes — 2026-06-01 — line 1**" in packet
    text_source_line = next(
        line for line in packet.splitlines() if "artifact SHA-256: hash-text" in line
    )
    assert "line 1" in text_source_line
    assert "page " not in text_source_line
    assert "source type: text" in text_source_line


def test_old_text_discovery_citations_remain_generation_aware(tmp_path: Path) -> None:
    database_path = _seed_text_corpus(tmp_path / ".newsrag")
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO processing_generations(id, document_id, fingerprint, configuration_json)
            VALUES('generation-old', 'document-text', 'old', '{}')
            """
        )
        connection.execute(
            """
            UPDATE source_units
            SET processing_generation_id = 'generation-old'
            WHERE document_id = 'document-text'
            """
        )
        connection.execute(
            """
            UPDATE chunks
            SET processing_generation_id = 'generation-old'
            WHERE document_id = 'document-text'
            """
        )
        connection.execute(
            """
            UPDATE passages
            SET processing_generation_id = 'generation-old'
            WHERE document_id = 'document-text'
            """
        )
        connection.execute(
            """
            UPDATE documents
            SET current_processing_generation_id = 'generation-old'
            WHERE id = 'document-text'
            """
        )

    old_item = create_discovery_item(
        database_path,
        document_id="document-text",
        item_type="topic",
        label="old schedule",
        extractor="deterministic",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id="document-text",
                source_unit_start_id="unit-text-3",
                source_unit_end_id="unit-text-3",
                quote="Work must be completed",
                validation_status="validated",
            ),
        ),
        item_id="discovery-text-old",
    )

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO processing_generations(id, document_id, fingerprint, configuration_json)
            VALUES('generation-new', 'document-text', 'new', '{}')
            """
        )
        connection.execute(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, processing_generation_id, ordinal,
                location_type, location_json, human_label, normalized_text,
                structure_json, extractor
            )
            VALUES(
                'unit-text-new-1', 'artifact-text', 'document-text', 'generation-new', 1,
                'text_line', '{"line_start": 1, "line_end": 1}', 'line 1',
                'Replacement current text.', '{}', 'plain-text'
            )
            """
        )
        connection.execute(
            """
            UPDATE documents
            SET current_processing_generation_id = 'generation-new'
            WHERE id = 'document-text'
            """
        )
        resolved_old = resolve_source_range(
            connection,
            document_id="document-text",
            source_unit_start_id="unit-text-3",
            processing_generation_id="generation-old",
        )
        with pytest.raises(SourceLocationError, match="processing generation"):
            resolve_source_range(
                connection,
                document_id="document-text",
                source_unit_start_id="unit-text-3",
                processing_generation_id="generation-new",
            )

    current_page = list_browse_items(
        database_path,
        item_types=TOPIC_ITEM_TYPES,
        include_history=False,
    )
    historical_page = list_browse_items(
        database_path,
        item_types=TOPIC_ITEM_TYPES,
        include_history=True,
    )
    old_detail = get_browse_item(database_path, old_item.id)

    assert resolved_old.location_label == "line 3"
    assert current_page.items == ()
    assert [item.item.id for item in historical_page.items] == [old_item.id]
    assert old_detail.processing_generation_id == "generation-old"
    assert not old_detail.is_current_processing_generation
    assert "document-text line 3" in format_browse_detail(old_detail)


def test_cli_source_type_help_lists_text() -> None:
    ingest_help = runner.invoke(app, ["ingest", "--help"]).stdout
    search_help = runner.invoke(app, ["search", "--help"]).stdout
    inventory_help = runner.invoke(app, ["documents", "list", "--help"]).stdout

    assert "csv, docx, html, markdown, pdf, text" in " ".join(ingest_help.replace("│", "").split())
    for help_text in (search_help, inventory_help):
        assert "csv, docx, html, markdown, pdf, or text" in " ".join(
            help_text.replace("│", "").split()
        )


_TEXT_LINE_ONE = "Council approved a $250,000 stormwater contract with ABC Construction."
_TEXT_LINE_THREE = "Work must be completed by June 1, 2026 and is 95% funded."


def _seed_text_corpus(data_dir: Path, *, include_pdf: bool = False) -> Path:
    database_path = initialize_storage(data_dir).database
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES(
                'source-text', 'local_path', '/tmp/council-notes.txt',
                '/tmp/council-notes.txt'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path,
                acquired_at, state
            )
            VALUES(
                'artifact-text', 'source-text', 'text/plain; charset=utf-8', 128, 'hash-text',
                '/tmp/council-notes.txt', CURRENT_TIMESTAMP, 'published'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, source_path, title, source_hash, metadata_json, artifact_id
            )
            VALUES(
                'document-text', '/tmp/council-notes.txt', 'Council Notes', 'hash-text',
                '{"body": "City Council", "meeting_date": "2026-06-01"}',
                'artifact-text'
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, ordinal, location_type, location_json,
                human_label, normalized_text, structure_json, extractor
            )
            VALUES(
                ?, 'artifact-text', 'document-text', ?, 'text_line', ?, ?, ?, '{}',
                'plain-text'
            )
            """,
            [
                (
                    "unit-text-1",
                    1,
                    '{"line_start": 1, "line_end": 1}',
                    "line 1",
                    _TEXT_LINE_ONE,
                ),
                (
                    "unit-text-2",
                    2,
                    '{"line_start": 2, "line_end": 2}',
                    "line 2",
                    "",
                ),
                (
                    "unit-text-3",
                    3,
                    '{"line_start": 3, "line_end": 3}',
                    "line 3",
                    _TEXT_LINE_THREE,
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO chunks(
                id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, text
            )
            VALUES(?, 'document-text', ?, ?, ?, ?, ?)
            """,
            [
                (
                    "chunk-text-1",
                    1,
                    1,
                    "unit-text-1",
                    "unit-text-1",
                    _TEXT_LINE_ONE,
                ),
                (
                    "chunk-text-3",
                    3,
                    3,
                    "unit-text-3",
                    "unit-text-3",
                    _TEXT_LINE_THREE,
                ),
            ],
        )
        connection.executemany(
            """
            INSERT INTO passages(
                id, chunk_id, document_id, page_start, page_end,
                source_unit_start_id, source_unit_end_id, ordinal, text
            )
            VALUES(?, ?, 'document-text', ?, ?, ?, ?, 1, ?)
            """,
            [
                (
                    "passage-text-1",
                    "chunk-text-1",
                    1,
                    1,
                    "unit-text-1",
                    "unit-text-1",
                    _TEXT_LINE_ONE,
                ),
                (
                    "passage-text-3",
                    "chunk-text-3",
                    3,
                    3,
                    "unit-text-3",
                    "unit-text-3",
                    _TEXT_LINE_THREE,
                ),
            ],
        )
        connection.executemany(
            "INSERT INTO passages_fts(passage_id, text) VALUES(?, ?)",
            [
                ("passage-text-1", _TEXT_LINE_ONE),
                ("passage-text-3", _TEXT_LINE_THREE),
            ],
        )
        _publish_revision(connection, "source-text", "document-text")
        if include_pdf:
            _add_pdf_document(connection)
        connection.commit()
    return database_path


def _add_pdf_document(connection: sqlite3.Connection) -> None:
    text = "The stormwater report describes a separate drainage project."
    connection.execute(
        """
        INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
        VALUES('source-pdf', 'local_path', '/tmp/report.pdf', '/tmp/report.pdf')
        """
    )
    connection.execute(
        """
        INSERT INTO source_artifacts(
            id, source_id, media_type, byte_size, content_hash, stored_path,
            acquired_at, state
        )
        VALUES(
            'artifact-pdf', 'source-pdf', 'application/pdf', 64, 'hash-pdf',
            '/tmp/report.pdf', CURRENT_TIMESTAMP, 'published'
        )
        """
    )
    connection.execute(
        """
        INSERT INTO documents(id, source_path, title, source_hash, metadata_json, artifact_id)
        VALUES(
            'document-pdf', '/tmp/report.pdf', 'Drainage Report', 'hash-pdf', '{}',
            'artifact-pdf'
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
            'unit-pdf-1', 'artifact-pdf', 'document-pdf', 1, 'page',
            '{"page_number": 1}', 'p. 1', ?, '{}', 'pymupdf'
        )
        """,
        (text,),
    )
    connection.execute(
        """
        INSERT INTO pages(id, document_id, page_number, source_unit_id, text, extractor)
        VALUES('page-pdf-1', 'document-pdf', 1, 'unit-pdf-1', ?, 'pymupdf')
        """,
        (text,),
    )
    connection.execute(
        """
        INSERT INTO chunks(
            id, document_id, page_start, page_end, source_unit_start_id,
            source_unit_end_id, text
        )
        VALUES('chunk-pdf-1', 'document-pdf', 1, 1, 'unit-pdf-1', 'unit-pdf-1', ?)
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
            'passage-pdf-1', 'chunk-pdf-1', 'document-pdf', 1, 1,
            'unit-pdf-1', 'unit-pdf-1', 1, ?
        )
        """,
        (text,),
    )
    connection.execute(
        "INSERT INTO passages_fts(passage_id, text) VALUES('passage-pdf-1', ?)",
        (text,),
    )
    _publish_revision(connection, "source-pdf", "document-pdf")


def _publish_revision(
    connection: sqlite3.Connection,
    source_id: str,
    document_id: str,
) -> None:
    revision_id = f"revision-{document_id}"
    connection.execute(
        """
        INSERT INTO source_revisions(
            id, source_id, document_id, revision_number, published_at
        )
        VALUES(?, ?, ?, 1, CURRENT_TIMESTAMP)
        """,
        (revision_id, source_id, document_id),
    )
    connection.execute(
        """
        UPDATE sources
        SET current_revision_id = ?, publication_generation = 1
        WHERE id = ?
        """,
        (revision_id, source_id),
    )
