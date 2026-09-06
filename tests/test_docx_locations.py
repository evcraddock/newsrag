from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from docx_fixtures import make_docx, paragraph
from markdown_it import MarkdownIt

from newsrag.adapters import AdapterInput, AdapterResult
from newsrag.briefs import format_generated_brief, generate_document_brief
from newsrag.discovery import (
    DiscoveryEvidenceDraft,
    create_discovery_item,
    create_document_profile,
)
from newsrag.documents import DocumentFilters, get_document_detail, list_document_summaries
from newsrag.docx_adapter import DocxSourceAdapter
from newsrag.facts import extract_document_facts
from newsrag.packets import format_source_packet, load_packet_source_provenance
from newsrag.search import (
    SearchFilters,
    merge_search_candidates,
    search_keyword_candidates,
)
from newsrag.source_locations import (
    SourceLocationError,
    format_docx_location_range,
    load_document_extent,
    resolve_source_range,
)
from newsrag.sources import DOCX_MEDIA_TYPE
from newsrag.storage import initialize_storage


def test_docx_ranges_validate_block_order_and_format_mixed_locations(tmp_path: Path) -> None:
    database_path = _seed_docx_corpus(tmp_path / ".newsrag")

    with sqlite3.connect(database_path) as connection:
        extent = load_document_extent(connection, "document-docx")
        paragraph = resolve_source_range(
            connection,
            document_id="document-docx",
            source_unit_start_id="unit-docx-2",
        )
        footnote = resolve_source_range(
            connection,
            document_id="document-docx",
            source_unit_start_id="unit-docx-3",
        )
        table = resolve_source_range(
            connection,
            document_id="document-docx",
            source_unit_start_id="unit-docx-4",
            source_unit_end_id="unit-docx-5",
        )
        mixed = resolve_source_range(
            connection,
            document_id="document-docx",
            source_unit_start_id="unit-docx-2",
            source_unit_end_id="unit-docx-5",
        )

    assert extent.source_type == "docx"
    assert extent.extent_type == "blocks"
    assert extent.extent_count == 6
    assert paragraph.location_label == "Council Update — paragraph 2"
    assert footnote.location_label == "Council Update — footnote ID 2, paragraph 1"
    assert table.location_label == "Council Update — table 1, rows 1–2"
    assert mixed.location_label == ("Council Update — paragraph 2 – table 1, row 2")
    assert mixed.text == "\n".join(_DOCX_TEXT[1:5])
    assert mixed.page_start is None
    assert mixed.page_end is None

    assert (
        format_docx_location_range(
            {
                "block_number": 7,
                "footnote_id": "2",
                "table_number": 3,
                "row_number": 1,
            },
            {
                "block_number": 8,
                "footnote_id": "2",
                "table_number": 3,
                "row_number": 2,
            },
        )
        == "footnote ID 2, table 3, rows 1–2"
    )


def test_docx_range_rejects_invalid_positive_numbers_structure_and_source_order(
    tmp_path: Path,
) -> None:
    database_path = _seed_docx_corpus(tmp_path / ".newsrag")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE source_units SET location_json = ? WHERE id = 'unit-docx-4'",
            ('{"block_number": 99, "table_number": 1, "row_number": 1}',),
        )
        with pytest.raises(SourceLocationError, match="does not match source order"):
            resolve_source_range(
                connection,
                document_id="document-docx",
                source_unit_start_id="unit-docx-2",
                source_unit_end_id="unit-docx-5",
            )
        connection.execute(
            "UPDATE source_units SET location_json = ? WHERE id = 'unit-docx-4'",
            ('{"block_number": 4, "table_number": 1, "row_number": 0}',),
        )
        with pytest.raises(SourceLocationError, match="row_number"):
            resolve_source_range(
                connection,
                document_id="document-docx",
                source_unit_start_id="unit-docx-4",
            )
        connection.execute(
            "UPDATE source_units SET location_json = ?, structure_json = ? "
            "WHERE id = 'unit-docx-4'",
            (
                '{"block_number": 4, "table_number": 1, "row_number": 1}',
                '{"kind": "table_row", "heading_path": "Council Update"}',
            ),
        )
        with pytest.raises(SourceLocationError, match="heading_path"):
            resolve_source_range(
                connection,
                document_id="document-docx",
                source_unit_start_id="unit-docx-4",
            )


@pytest.mark.parametrize(
    "location",
    [
        {"block_number": 0, "paragraph_number": 1},
        {"block_number": 1, "paragraph_number": 0},
        {"block_number": 1, "table_number": 0, "row_number": 1},
        {"block_number": 1, "table_number": 1, "row_number": 0},
        {"block_number": 1, "footnote_id": "", "paragraph_number": 1},
        {"block_number": 1, "paragraph_number": 1, "row_number": 1},
    ],
)
def test_docx_location_formatter_rejects_invalid_typed_locations(
    location: dict[str, object],
) -> None:
    with pytest.raises(SourceLocationError, match="source-unit|DOCX"):
        format_docx_location_range(location, location)


def test_docx_inventory_search_discovery_brief_and_packet_keep_typed_inert_provenance(
    tmp_path: Path,
) -> None:
    database_path = _seed_docx_corpus(tmp_path / ".newsrag", include_pdf=True)

    candidates = search_keyword_candidates(database_path, "stormwater", limit=10)
    mixed_results = merge_search_candidates(
        candidates,
        [],
        database_path=database_path,
        limit=10,
        keyword_weight=0.6,
        vector_weight=0.4,
    )
    docx_results = merge_search_candidates(
        candidates,
        [],
        database_path=database_path,
        limit=10,
        keyword_weight=0.6,
        vector_weight=0.4,
        filters=SearchFilters(source_type="docx"),
    )
    inventory = list_document_summaries(
        database_path,
        filters=DocumentFilters(source_type="docx"),
    )
    detail = get_document_detail(database_path, "document-docx")

    assert {result.source_type for result in mixed_results} == {"docx", "pdf"}
    assert [result.passage_id for result in docx_results] == ["passage-docx-2"]
    result = docx_results[0]
    assert result.citation == (
        "Approved <b>Minutes</b> — 2026-08-01 — Council Update — paragraph 2"
    )
    assert result.location_label == "paragraph 2"
    assert "<script>alert('x')</script>" in result.text
    assert [document.id for document in inventory.documents] == ["document-docx"]
    assert inventory.documents[0].extent_label == "blocks"
    assert inventory.documents[0].extent_count == 6
    assert inventory.documents[0].page_count is None
    assert detail.source_type == "docx"
    assert detail.extent_label == "blocks"
    assert detail.extent_count == 6
    assert detail.page_count is None

    profile = create_document_profile(
        database_path,
        document_id="document-docx",
        text_length=sum(len(text) for text in _DOCX_TEXT),
        extractor="docx-xml",
    )
    facts = extract_document_facts(database_path, "document-docx")
    mixed_item = create_discovery_item(
        database_path,
        document_id="document-docx",
        item_type="topic",
        label="mixed DOCX evidence",
        extractor="test",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id="document-docx",
                source_unit_start_id="unit-docx-2",
                source_unit_end_id="unit-docx-5",
                quote="stormwater contract by June 1, 2026. [footnote ID 2] "
                "Funding note requires audit. Fund Amount",
                validation_status="validated",
            ),
        ),
        item_id="discovery-docx-mixed",
    )
    brief = generate_document_brief(database_path, "document-docx")
    brief_output = format_generated_brief(brief)
    provenance = load_packet_source_provenance(database_path, docx_results)
    packet = format_source_packet(
        query="stormwater",
        results=docx_results,
        source_provenance=provenance,
    )

    assert profile.source_type == "docx"
    assert profile.extent_type == "blocks"
    assert profile.extent_count == 6
    assert facts.created
    assert all(item.evidence[0].location_type == "docx_block" for item in facts.created)
    assert mixed_item.evidence[0].location_label == (
        "Council Update — paragraph 2 – table 1, row 2"
    )
    assert brief.document.source_type == "docx"
    assert brief.document.extent_type == "blocks"
    assert "blocks: 6" in brief_output
    assert "paragraph 2" in brief_output
    assert "source type: docx" in packet
    assert "paragraph 2" in packet
    assert "page 2" not in packet
    assert provenance["document-docx"].artifact_hash == "hash-docx"

    assert "<script" not in packet.casefold()
    assert "<script" not in brief_output.casefold()
    assert "&lt;script&gt;" in packet
    assert "&lt;script&gt;" in brief_output
    assert "<b>" not in packet.casefold()
    assert "<b>" not in brief_output.casefold()
    assert result.text == _DOCX_TEXT[1]
    assert "<script" not in MarkdownIt().render(packet).casefold()
    assert "<script" not in MarkdownIt().render(brief_output).casefold()


_DOCX_TEXT = (
    "Council Update",
    "Docxsignal Council approved a $250,000 <script>alert('x')</script> stormwater "
    "contract by June 1, 2026. [footnote ID 2]",
    "Funding note requires audit.",
    "Fund\tAmount",
    "Road repairs\t$250,000",
    "Later reference remains [footnote ID 2]",
)


def _extract_docx(tmp_path: Path) -> AdapterResult:
    styles = (
        '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="Heading 1"/></w:style>'
    )
    body = (
        paragraph("Council Update", '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>')
        + paragraph(
            "Docxsignal Council approved a $250,000 <script>alert('x')</script> stormwater "
            "contract by June 1, 2026.",
            runs='<w:r><w:footnoteReference w:id="2"/></w:r>',
        )
        + "<w:tbl><w:tr><w:tc>"
        + paragraph("Fund")
        + "</w:tc><w:tc>"
        + paragraph("Amount")
        + "</w:tc></w:tr><w:tr><w:tc>"
        + paragraph("Road repairs")
        + "</w:tc><w:tc>"
        + paragraph("$250,000")
        + "</w:tc></w:tr></w:tbl>"
        + paragraph(
            "Later reference remains",
            runs='<w:r><w:footnoteReference w:id="2"/></w:r>',
        )
    )
    footnotes = (
        '<w:footnote w:id="2">' + paragraph("Funding note requires audit.") + "</w:footnote>"
    )
    source_path = make_docx(
        tmp_path / "approved-minutes.docx",
        body,
        styles=styles,
        footnotes=footnotes,
    )
    return DocxSourceAdapter().extract(
        AdapterInput(
            artifact_path=source_path,
            content_hash="hash-docx",
            media_type=DOCX_MEDIA_TYPE,
            work_dir=tmp_path,
        )
    )


def _seed_docx_corpus(data_dir: Path, *, include_pdf: bool = False) -> Path:
    database_path = initialize_storage(data_dir).database
    extracted = _extract_docx(data_dir.parent)
    assert tuple(unit.normalized_text for unit in extracted.units) == _DOCX_TEXT

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-docx', 'local_path', '/tmp/approved-minutes.docx',
                   '/tmp/approved-minutes.docx')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path,
                acquired_at, state
            )
            VALUES(
                'artifact-docx', 'source-docx', ?, 512, 'hash-docx',
                '/tmp/approved-minutes.docx', CURRENT_TIMESTAMP, 'published'
            )
            """,
            (DOCX_MEDIA_TYPE,),
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, source_path, title, source_hash, metadata_json, artifact_id
            )
            VALUES(
                'document-docx', '/tmp/approved-minutes.docx', 'Approved <b>Minutes</b>',
                'hash-docx',
                '{"body": "City Council", "meeting_date": "2026-08-01"}',
                'artifact-docx'
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, ordinal, location_type, location_json,
                human_label, normalized_text, structure_json, extractor
            )
            VALUES(?, 'artifact-docx', 'document-docx', ?, ?, ?, ?, ?, ?, 'docx-xml')
            """,
            [
                (
                    f"unit-docx-{unit.ordinal}",
                    unit.ordinal,
                    unit.location_type,
                    json.dumps(unit.location, sort_keys=True),
                    unit.human_label,
                    unit.normalized_text,
                    json.dumps(unit.structure, sort_keys=True),
                )
                for unit in extracted.units
            ],
        )
        searchable_units = [unit for unit in extracted.units if len(unit.normalized_text) >= 40]
        connection.executemany(
            """
            INSERT INTO chunks(
                id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, text
            )
            VALUES(?, 'document-docx', ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"chunk-docx-{unit.ordinal}",
                    unit.ordinal,
                    unit.ordinal,
                    f"unit-docx-{unit.ordinal}",
                    f"unit-docx-{unit.ordinal}",
                    unit.normalized_text,
                )
                for unit in searchable_units
            ],
        )
        connection.executemany(
            """
            INSERT INTO passages(
                id, chunk_id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, ordinal, text
            )
            VALUES(?, ?, 'document-docx', ?, ?, ?, ?, 1, ?)
            """,
            [
                (
                    f"passage-docx-{unit.ordinal}",
                    f"chunk-docx-{unit.ordinal}",
                    unit.ordinal,
                    unit.ordinal,
                    f"unit-docx-{unit.ordinal}",
                    f"unit-docx-{unit.ordinal}",
                    unit.normalized_text,
                )
                for unit in searchable_units
            ],
        )
        connection.executemany(
            "INSERT INTO passages_fts(passage_id, text) VALUES(?, ?)",
            [(f"passage-docx-{unit.ordinal}", unit.normalized_text) for unit in searchable_units],
        )
        _publish_revision(connection, "source-docx", "document-docx")
        if include_pdf:
            _add_pdf_source(connection)
        connection.commit()
    return database_path


def _add_pdf_source(connection: sqlite3.Connection) -> None:
    text = "A separate PDF stormwater report provides enough searchable supporting text."
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
        VALUES('artifact-pdf', 'source-pdf', 'application/pdf', 64, 'hash-pdf',
               '/tmp/report.pdf', CURRENT_TIMESTAMP, 'published')
        """
    )
    connection.execute(
        """
        INSERT INTO documents(id, source_path, title, source_hash, metadata_json, artifact_id)
        VALUES('document-pdf', '/tmp/report.pdf', 'PDF Report', 'hash-pdf', '{}',
               'artifact-pdf')
        """
    )
    connection.execute(
        """
        INSERT INTO source_units(
            id, artifact_id, document_id, ordinal, location_type, location_json,
            human_label, normalized_text, structure_json, extractor
        )
        VALUES('unit-pdf-1', 'artifact-pdf', 'document-pdf', 1, 'page',
               '{"page_number": 1}', 'p. 1', ?, '{}', 'pymupdf')
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
        VALUES('passage-pdf-1', 'chunk-pdf-1', 'document-pdf', 1, 1,
               'unit-pdf-1', 'unit-pdf-1', 1, ?)
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
