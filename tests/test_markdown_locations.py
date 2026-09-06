from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from markdown_it import MarkdownIt
from typer.testing import CliRunner

from newsrag.adapters import AdapterInput
from newsrag.briefs import (
    BriefDocumentContext,
    BriefEvidenceLine,
    GeneratedBrief,
    format_generated_brief,
    generate_document_brief,
)
from newsrag.cli import app
from newsrag.discovery import (
    DiscoveryEvidenceDraft,
    DocumentBriefRecord,
    create_discovery_item,
    create_document_profile,
)
from newsrag.documents import DocumentFilters, get_document_detail, list_document_summaries
from newsrag.markdown_adapter import MarkdownSourceAdapter
from newsrag.packets import PacketSourceProvenance, format_source_packet
from newsrag.search import (
    SearchFilters,
    SearchResult,
    format_citation,
    merge_search_candidates,
    search_keyword_candidates,
)
from newsrag.source_locations import load_document_extent, resolve_source_range
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_markdown_locations_use_heading_paths_and_original_line_extents(tmp_path: Path) -> None:
    database_path = _seed_markdown_corpus(tmp_path / ".newsrag")

    with sqlite3.connect(database_path) as connection:
        extent = load_document_extent(connection, "document-markdown")
        full_range = resolve_source_range(
            connection,
            document_id="document-markdown",
            source_unit_start_id="unit-markdown-1",
            source_unit_end_id="unit-markdown-6",
        )
        budget_range = resolve_source_range(
            connection,
            document_id="document-markdown",
            source_unit_start_id="unit-markdown-4",
            source_unit_end_id="unit-markdown-6",
        )

    assert extent.source_type == "markdown"
    assert extent.extent_type == "lines"
    assert extent.extent_count == 8
    assert extent.text_length == sum(len(text) for text in _MARKDOWN_UNIT_TEXTS)
    assert full_range.location_type == "markdown_block"
    assert full_range.location_label == "Council Update — lines 1–8"
    assert full_range.text == "\n".join(_MARKDOWN_UNIT_TEXTS)
    assert "Intro.\n\n## Budget" in full_range.text
    assert budget_range.location_label == "Council Update — Budget — lines 4–8"
    assert budget_range.text == "\n".join(_MARKDOWN_UNIT_TEXTS[3:])


def test_markdown_search_inventory_discovery_brief_and_packet_are_typed_and_safe(
    tmp_path: Path,
) -> None:
    database_path = _seed_markdown_corpus(tmp_path / ".newsrag")
    candidates = search_keyword_candidates(database_path, "alert", limit=5)
    results = merge_search_candidates(
        candidates,
        [],
        database_path=database_path,
        limit=5,
        keyword_weight=0.6,
        vector_weight=0.4,
        filters=SearchFilters(source_type="markdown"),
    )
    inventory = list_document_summaries(
        database_path,
        filters=DocumentFilters(source_type="markdown"),
    )
    detail = get_document_detail(database_path, "document-markdown")

    assert len(results) == 1
    assert results[0].source_type == "markdown"
    assert results[0].citation == ("Council Markdown — Council Update — Budget — lines 6–8")
    assert [document.id for document in inventory.documents] == ["document-markdown"]
    assert inventory.documents[0].extent_label == "lines"
    assert inventory.documents[0].extent_count == 8
    assert detail.source_type == "markdown"
    assert detail.extent_label == "lines"
    assert detail.extent_count == 8

    profile = create_document_profile(
        database_path,
        document_id="document-markdown",
        text_length=sum(len(text) for text in _MARKDOWN_UNIT_TEXTS),
        extractor="markdown-it",
    )
    item = create_discovery_item(
        database_path,
        document_id="document-markdown",
        item_type="topic",
        label="embedded example",
        extractor="deterministic",
        evidence=(
            DiscoveryEvidenceDraft(
                document_id="document-markdown",
                source_unit_start_id="unit-markdown-6",
                source_unit_end_id="unit-markdown-6",
                quote=_UNSAFE_HTML,
                validation_status="validated",
            ),
        ),
        item_id="discovery-markdown",
    )
    brief = generate_document_brief(database_path, "document-markdown")
    brief_output = format_generated_brief(brief)
    packet = format_source_packet(
        query="embedded example",
        results=results,
        source_provenance={
            "document-markdown": PacketSourceProvenance(
                document_id="document-markdown",
                source_type="markdown",
                source_kind="local_path",
                submitted_reference="/tmp/council.md",
                resolved_reference="/tmp/council.md",
                retrieved_at=None,
                artifact_hash="hash-markdown",
            )
        },
    )

    assert profile.source_type == "markdown"
    assert profile.extent_type == "lines"
    assert profile.extent_count == 8
    assert item.evidence[0].location_type == "markdown_block"
    assert item.evidence[0].location_label == "Council Update — Budget — lines 6–8"
    assert brief.document.source_type == "markdown"
    assert brief.document.extent_type == "lines"
    assert "lines: 8" in brief_output
    assert "Council Update — Budget — lines 6–8" in brief_output
    assert "line 6–8" not in packet
    source_line = next(line for line in packet.splitlines() if "hash-markdown" in line)
    assert "lines 6–8" in source_line
    assert "page " not in source_line
    assert "source type: markdown" in source_line

    assert _UNSAFE_HTML not in packet
    assert _UNSAFE_HTML not in brief_output
    assert "&lt;script&gt;" in packet
    assert "&lt;script&gt;" in brief_output
    assert "<script" not in MarkdownIt().render(packet).casefold()
    assert "<script" not in MarkdownIt().render(brief_output).casefold()


def test_markdown_heading_and_label_markup_stays_inert_in_packet_and_brief(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "hostile-heading.md"
    source_path.write_text(
        "# Council <img src=x onerror=alert(1)> ![seal](https://example.test/seal.png)\n\n"
        "Evidence remains literal.\n",
        encoding="utf-8",
    )
    extracted = MarkdownSourceAdapter().extract(
        AdapterInput(
            artifact_path=source_path,
            content_hash="hash-hostile",
            media_type="text/markdown",
            work_dir=tmp_path,
        )
    )
    raw_heading_path = extracted.units[0].structure["heading_path"]
    assert isinstance(raw_heading_path, list)
    heading_path = tuple(str(value) for value in raw_heading_path)
    hostile_heading = heading_path[0]
    citation = format_citation(
        title=None,
        meeting_date="2026-06-01",
        page_number=3,
        heading_path=heading_path,
        location_label="line 3",
    )
    result = SearchResult(
        passage_id="passage-hostile",
        document_id="document-hostile",
        page_start=3,
        page_end=3,
        text="Evidence with <img src=x onerror=alert(2)>.",
        citation=citation,
        score=1.0,
        keyword_score=0.1,
        vector_score=None,
        meeting_date="2026-06-01",
        source_type="markdown",
    )
    provenance = PacketSourceProvenance(
        document_id="document-hostile",
        source_type="markdown",
        source_kind="local_path",
        submitted_reference=str(source_path),
        resolved_reference=str(source_path),
        retrieved_at=None,
        artifact_hash="hash-hostile",
    )
    packet = format_source_packet(
        query="literal evidence",
        results=(result,),
        source_provenance={result.document_id: provenance},
    )

    brief_record = DocumentBriefRecord(
        id="brief-hostile",
        document_id=result.document_id,
        summary=f"Summary for {hostile_heading}.",
        significance="Significance of ![badge](https://example.test/badge.png).",
        open_questions=(f"What follows {hostile_heading}?",),
        extractor="test",
        provider=None,
        model=None,
        status="validated",
        created_at="2026-06-01",
        updated_at="2026-06-01",
    )
    brief = GeneratedBrief(
        document=BriefDocumentContext(
            id=result.document_id,
            title=hostile_heading,
            metadata={"body": "<img src=x onerror=alert(3)>"},
            source_type="markdown",
            extent_type="lines",
            extent_count=3,
            text_length=20,
        ),
        record=brief_record,
        notable_items=(),
        evidence_lines=(
            BriefEvidenceLine(
                item_type="topic",
                label="![badge](https://example.test/badge.png)",
                summary="",
                location_type="markdown_block",
                location_label=f"{hostile_heading} — line 3",
                page_start=None,
                page_end=None,
                quote="<img src=x onerror=alert(4)>",
            ),
        ),
    )
    brief_output = format_generated_brief(brief)

    assert extracted.units[0].structure["heading_path"] == [hostile_heading]
    assert "<img" not in packet.casefold()
    assert "<img" not in brief_output.casefold()
    assert r"\!\[seal\]\(" in packet
    assert r"\!\[seal\]\(" in brief_output
    assert "<img" not in MarkdownIt().render(packet).casefold()
    assert "<img" not in MarkdownIt().render(brief_output).casefold()
    assert result.citation == citation
    assert provenance.submitted_reference == str(source_path)
    assert brief.record.summary == brief_record.summary


def test_markdown_cli_help_and_packet_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured_filters: list[SearchFilters] = []

    class FakeSearchEngine:
        def search(
            self,
            query: str,
            *,
            filters: SearchFilters | None = None,
            include_history: bool = False,
        ) -> list[SearchResult]:
            assert query == "budget"
            assert not include_history
            assert filters is not None
            captured_filters.append(filters)
            return []

    monkeypatch.setattr("newsrag.search.build_search_engine", lambda **_: FakeSearchEngine())
    output_path = tmp_path / "packet.md"
    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(tmp_path / ".newsrag"),
            "packet",
            "budget",
            "--source-type",
            "markdown",
            "--out",
            str(output_path),
        ],
    )

    assert result.exit_code == 0, result.stdout
    assert captured_filters == [SearchFilters(source_type="markdown")]
    assert "html, markdown, pdf, text" in runner.invoke(app, ["ingest", "--help"]).stdout
    assert "html, markdown, pdf, or text" in runner.invoke(app, ["search", "--help"]).stdout
    assert (
        "html, markdown, pdf, or text" in runner.invoke(app, ["documents", "list", "--help"]).stdout
    )
    assert "html, markdown, pdf, or text" in runner.invoke(app, ["packet", "--help"]).stdout


_UNSAFE_HTML = "<script>alert('x')</script>"
_MARKDOWN_UNIT_TEXTS = (
    "# Council Update",
    "Intro.",
    "",
    "## Budget",
    "Council approved a $250,000 contract.",
    f"```html\n{_UNSAFE_HTML}\n```",
)


def _seed_markdown_corpus(data_dir: Path) -> Path:
    database_path = initialize_storage(data_dir).database
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-markdown', 'local_path', '/tmp/council.md', '/tmp/council.md')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path,
                acquired_at, state
            )
            VALUES(
                'artifact-markdown', 'source-markdown', 'text/markdown; charset=utf-8',
                160, 'hash-markdown', '/tmp/council.md', CURRENT_TIMESTAMP, 'published'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO documents(id, source_path, title, source_hash, metadata_json, artifact_id)
            VALUES(
                'document-markdown', '/tmp/council.md', 'Council Markdown',
                'hash-markdown', '{}', 'artifact-markdown'
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
                ?, 'artifact-markdown', 'document-markdown', ?, 'markdown_block', ?, ?, ?, ?,
                'markdown-it'
            )
            """,
            [
                (
                    "unit-markdown-1",
                    1,
                    '{"line_start": 1, "line_end": 1}',
                    "line 1",
                    _MARKDOWN_UNIT_TEXTS[0],
                    '{"kind": "heading", "heading_path": ["Council Update"], "containers": []}',
                ),
                (
                    "unit-markdown-2",
                    2,
                    '{"line_start": 2, "line_end": 2}',
                    "line 2",
                    _MARKDOWN_UNIT_TEXTS[1],
                    '{"kind": "paragraph", "heading_path": ["Council Update"], "containers": []}',
                ),
                (
                    "unit-markdown-3",
                    3,
                    '{"line_start": 3, "line_end": 3}',
                    "line 3",
                    _MARKDOWN_UNIT_TEXTS[2],
                    '{"kind": "blank", "heading_path": ["Council Update"], "containers": []}',
                ),
                (
                    "unit-markdown-4",
                    4,
                    '{"line_start": 4, "line_end": 4}',
                    "line 4",
                    _MARKDOWN_UNIT_TEXTS[3],
                    '{"kind": "heading", "heading_path": ["Council Update", "Budget"], "containers": []}',
                ),
                (
                    "unit-markdown-5",
                    5,
                    '{"line_start": 5, "line_end": 5}',
                    "line 5",
                    _MARKDOWN_UNIT_TEXTS[4],
                    '{"kind": "paragraph", "heading_path": ["Council Update", "Budget"], "containers": []}',
                ),
                (
                    "unit-markdown-6",
                    6,
                    '{"line_start": 6, "line_end": 8}',
                    "lines 6–8",
                    _MARKDOWN_UNIT_TEXTS[5],
                    '{"kind": "fence", "heading_path": ["Council Update", "Budget"], "containers": []}',
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO chunks(
                id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, text
            )
            VALUES(
                'chunk-markdown-1', 'document-markdown', 6, 8,
                'unit-markdown-6', 'unit-markdown-6', ?
            )
            """,
            (_MARKDOWN_UNIT_TEXTS[5],),
        )
        connection.execute(
            """
            INSERT INTO passages(
                id, chunk_id, document_id, page_start, page_end, source_unit_start_id,
                source_unit_end_id, ordinal, text
            )
            VALUES(
                'passage-markdown-1', 'chunk-markdown-1', 'document-markdown', 6, 8,
                'unit-markdown-6', 'unit-markdown-6', 1, ?
            )
            """,
            (_MARKDOWN_UNIT_TEXTS[5],),
        )
        connection.execute(
            "INSERT INTO passages_fts(passage_id, text) VALUES('passage-markdown-1', ?)",
            (_MARKDOWN_UNIT_TEXTS[5],),
        )
        connection.execute(
            """
            INSERT INTO source_revisions(
                id, source_id, document_id, revision_number, published_at
            )
            VALUES(
                'revision-markdown', 'source-markdown', 'document-markdown', 1,
                CURRENT_TIMESTAMP
            )
            """
        )
        connection.execute(
            """
            UPDATE sources
            SET current_revision_id = 'revision-markdown', publication_generation = 1
            WHERE id = 'source-markdown'
            """
        )
        connection.commit()
    return database_path
