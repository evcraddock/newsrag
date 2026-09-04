from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.config import EmbeddingConfig
from newsrag.embeddings import ChunkEmbedding, EmbeddingMetadata, QueryEmbedding
from newsrag.search import (
    PassageVectorRecord,
    SearchCandidate,
    SearchError,
    SearchFilters,
    SearchResult,
    build_search_engine,
    format_citation,
    format_search_results,
    merge_search_candidates,
    search_keyword_candidates,
)
from newsrag.storage import initialize_storage

runner = CliRunner()


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


@dataclass(frozen=True)
class FakeQueryEmbeddingProvider:
    metadata: EmbeddingMetadata = EmbeddingMetadata(
        provider="openai_compatible",
        model="nomic-embed-text-v1.5",
        version="latest",
    )

    def embed_query(self, text: str) -> QueryEmbedding:
        vectors = {
            "stormwater downtown": (0.1, 0.1),
            "semantic zoning": (0.2, 0.2),
            "games": (0.3, 0.3),
            "Belt filter Press": (0.4, 0.4),
            "banana telescope": (2.0, 2.0),
            "books": (0.5, 0.5),
        }
        return QueryEmbedding(
            text=text, vector=vectors.get(text, (1.0, 1.0)), metadata=self.metadata
        )

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        return [
            ChunkEmbedding(
                text=text,
                vector=(float(index + 1), float(index + 1)),
                metadata=self.metadata,
            )
            for index, text in enumerate(texts)
        ]


@dataclass(frozen=True)
class FakeVectorSearcher:
    candidates_by_query: dict[str, list[SearchCandidate]] = field(default_factory=dict)

    def search(self, query_embedding: QueryEmbedding, *, limit: int) -> list[SearchCandidate]:
        return self.candidates_by_query.get(query_embedding.text, [])[:limit]


@dataclass
class FakeVectorStore:
    added_passages: list[PassageVectorRecord] = field(default_factory=list)

    def add_passages(self, passages: Sequence[PassageVectorRecord]) -> None:
        self.added_passages.extend(passages)


def test_search_over_indexed_passages_returns_ranked_cited_passages(tmp_path: Path) -> None:
    database_path = _seed_search_corpus(tmp_path)
    vector_store = FakeVectorStore()

    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(
            {
                "stormwater downtown": [
                    SearchCandidate(
                        passage_id="passage-a",
                        document_id="document-a",
                        page_start=3,
                        page_end=3,
                        text="downtown stormwater improvements",
                        title=None,
                        meeting_date=None,
                        vector_score=0.1,
                    )
                ]
            }
        ),
        vector_store=vector_store,
    )

    results = engine.search("stormwater downtown")

    assert [result.passage_id for result in results] == ["passage-a"]
    assert results[0].citation == "Stormwater Report — 2026-05-01 — p. 3"
    assert "downtown stormwater improvements" in results[0].text
    assert {record.passage_id for record in vector_store.added_passages} >= {
        "passage-a",
        "passage-b",
        "passage-c",
        "passage-d",
        "passage-e",
        "passage-f",
    }


def test_search_candidates_and_results_retain_source_unit_ranges(tmp_path: Path) -> None:
    database_path = initialize_storage(tmp_path / ".newsrag").database

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES('source-1', 'local_path', '/tmp/report.pdf', '/tmp/report.pdf')
            """
        )
        connection.execute(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path, acquired_at
            )
            VALUES('artifact-1', 'source-1', 'application/pdf', 10, 'hash-1', '/tmp/report.pdf', CURRENT_TIMESTAMP)
            """
        )
        connection.execute(
            """
            INSERT INTO documents(
                id, source_path, title, source_hash, metadata_json, artifact_id
            )
            VALUES(
                'document-1',
                '/tmp/report.pdf',
                'Stormwater Report',
                'hash-1',
                '{"meeting_date": "2026-05-01"}',
                'artifact-1'
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO source_units(
                id,
                artifact_id,
                document_id,
                ordinal,
                location_type,
                location_json,
                human_label,
                normalized_text,
                extractor
            )
            VALUES(?, 'artifact-1', 'document-1', ?, 'page', ?, ?, ?, 'pymupdf')
            """,
            [
                ("unit-1", 1, '{"page_number": 7}', "p. 7", "stormwater"),
                ("unit-2", 2, '{"page_number": 8}', "p. 8", "improvements"),
            ],
        )
        connection.execute(
            """
            INSERT INTO chunks(
                id,
                document_id,
                page_start,
                page_end,
                source_unit_start_id,
                source_unit_end_id,
                text
            )
            VALUES('chunk-1', 'document-1', 1, 2, 'unit-1', 'unit-2', 'stormwater improvements')
            """
        )
        connection.execute(
            """
            INSERT INTO passages(
                id,
                chunk_id,
                document_id,
                page_start,
                page_end,
                source_unit_start_id,
                source_unit_end_id,
                ordinal,
                text
            )
            VALUES(
                'passage-1',
                'chunk-1',
                'document-1',
                1,
                2,
                'unit-1',
                'unit-2',
                1,
                'stormwater improvements'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO passages_fts(passage_id, text)
            VALUES('passage-1', 'stormwater improvements')
            """
        )
        connection.commit()

    candidates = search_keyword_candidates(database_path, "stormwater", limit=5)
    results = merge_search_candidates(
        candidates,
        [],
        database_path=database_path,
        limit=5,
        keyword_weight=0.6,
        vector_weight=0.4,
    )

    assert len(candidates) == 1
    assert candidates[0].source_unit_start_id == "unit-1"
    assert candidates[0].source_unit_end_id == "unit-2"
    assert len(results) == 1
    assert results[0].source_unit_start_id == "unit-1"
    assert results[0].source_unit_end_id == "unit-2"
    assert results[0].citation == "Stormwater Report — 2026-05-01 — p. 7"


def test_search_filters_by_document_metadata_and_meeting_date(tmp_path: Path) -> None:
    database_path = _seed_search_corpus(tmp_path)

    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(),
        vector_store=FakeVectorStore(),
    )

    results = engine.search(
        "stormwater downtown",
        filters=SearchFilters(
            body="Planning Commission",
            document_type="staff_report",
            jurisdiction="Example City",
            source_url="https://example.test/stormwater.pdf",
            since="2025-01-01",
        ),
    )

    assert [result.passage_id for result in results] == ["passage-a"]
    assert results[0].body == "Planning Commission"
    assert results[0].document_type == "staff_report"
    assert results[0].jurisdiction == "Example City"
    assert results[0].source_url == "https://example.test/stormwater.pdf"


def test_mixed_source_search_defaults_to_all_types_and_filters_by_source_type(
    tmp_path: Path,
) -> None:
    database_path = _seed_search_corpus(tmp_path)
    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(),
        vector_store=FakeVectorStore(),
    )

    all_results = engine.search("budget")
    html_results = engine.search("budget", filters=SearchFilters(source_type="html"))
    pdf_results = engine.search(
        "budget",
        filters=SearchFilters(source_type="pdf", body="City Council"),
    )

    assert {result.passage_id for result in all_results} == {"passage-b", "passage-j"}
    assert [result.passage_id for result in html_results] == ["passage-j"]
    assert html_results[0].source_type == "html"
    assert html_results[0].citation == "Web Notice — Budget — block 3"
    assert [result.passage_id for result in pdf_results] == ["passage-b"]
    assert pdf_results[0].source_type == "pdf"


def test_search_rejects_unsupported_source_type(tmp_path: Path) -> None:
    filters = SearchFilters(source_type="docx")

    with pytest.raises(
        SearchError,
        match="Unsupported --source-type 'docx'; expected one of: html, pdf",
    ):
        filters.validate()

    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)
    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "search", "budget", "--source-type", "docx"],
    )

    assert result.exit_code == 1
    assert "Unsupported --source-type 'docx'; expected one of: html, pdf" in result.stdout


def test_search_filters_vector_candidates_without_leaking_out_of_filter_results(
    tmp_path: Path,
) -> None:
    database_path = _seed_search_corpus(tmp_path)

    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(
            {
                "semantic zoning": [
                    SearchCandidate(
                        passage_id="passage-c",
                        document_id="document-c",
                        page_start=2,
                        page_end=2,
                        text="zoning map amendments",
                        title=None,
                        meeting_date=None,
                        vector_score=0.1,
                    ),
                    SearchCandidate(
                        passage_id="passage-a",
                        document_id="document-a",
                        page_start=3,
                        page_end=3,
                        text="downtown stormwater improvements",
                        title=None,
                        meeting_date=None,
                        vector_score=0.2,
                    ),
                ]
            }
        ),
        vector_store=FakeVectorStore(),
    )

    results = engine.search("semantic zoning", filters=SearchFilters(body="Planning Commission"))

    assert [result.passage_id for result in results] == ["passage-a"]


def test_filtered_no_result_output_mentions_filters(tmp_path: Path) -> None:
    database_path = _seed_search_corpus(tmp_path)

    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(),
        vector_store=FakeVectorStore(),
    )

    results = engine.search("stormwater downtown", filters=SearchFilters(body="City Council"))
    output = format_search_results(
        results,
        query="stormwater downtown",
        filters=SearchFilters(body="City Council"),
    )

    assert output == "No evidence found matching filters: body=City Council."


def test_search_rejects_invalid_date_filters(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "search", "stormwater", "--since", "bad-date"]
    )

    assert result.exit_code == 1
    assert "Invalid --since date 'bad-date'; expected YYYY-MM-DD" in result.stdout


def test_search_help_documents_metadata_filters() -> None:
    result = runner.invoke(app, ["search", "--help"])
    plain_output = _strip_ansi(result.stdout)

    assert result.exit_code == 0
    assert "--body" in plain_output
    assert "--document-type" in plain_output
    assert "--source-url" in plain_output
    assert "--source-type" in plain_output
    assert "--since" in plain_output


def test_search_uses_vector_candidates_when_keyword_search_is_empty(tmp_path: Path) -> None:
    database_path = _seed_search_corpus(tmp_path)

    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(
            {
                "semantic zoning": [
                    SearchCandidate(
                        passage_id="passage-c",
                        document_id="document-c",
                        page_start=2,
                        page_end=2,
                        text="zoning map amendments",
                        title=None,
                        meeting_date=None,
                        vector_score=0.2,
                    )
                ]
            }
        ),
        vector_store=FakeVectorStore(),
    )

    results = engine.search("semantic zoning")

    assert [result.passage_id for result in results] == ["passage-c"]
    assert results[0].citation == "Zoning Packet — 2026-03-15 — p. 2"


def test_search_ignores_orphan_vectors_from_failed_publication(tmp_path: Path) -> None:
    database_path = _seed_search_corpus(tmp_path)
    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(
            {
                "banana telescope": [
                    SearchCandidate(
                        passage_id="passage-orphan",
                        document_id="document-orphan",
                        page_start=1,
                        page_end=1,
                        text="uncommitted vector",
                        title=None,
                        meeting_date=None,
                        vector_score=0.1,
                    )
                ]
            }
        ),
        vector_store=FakeVectorStore(),
    )

    assert engine.search("banana telescope") == []


def test_search_keeps_strong_semantic_passage_when_keyword_hits_exist(tmp_path: Path) -> None:
    database_path = _seed_search_corpus(tmp_path)

    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(
            {
                "games": [
                    SearchCandidate(
                        passage_id="passage-e",
                        document_id="document-d",
                        page_start=10,
                        page_end=10,
                        text="• Teen Kickback – There will be games, snacks, and craft supplies available.",
                        title=None,
                        meeting_date=None,
                        vector_score=0.92,
                    ),
                    SearchCandidate(
                        passage_id="passage-f",
                        document_id="document-d",
                        page_start=10,
                        page_end=10,
                        text="• Dungeons & Dragons – Take up a weapon and defeat various foes.",
                        title=None,
                        meeting_date=None,
                        vector_score=0.96,
                    ),
                ]
            }
        ),
        vector_store=FakeVectorStore(),
    )

    results = engine.search("games")

    assert [result.passage_id for result in results] == ["passage-e", "passage-f"]


def test_search_drops_weak_vector_tail_when_keyword_hits_exist(tmp_path: Path) -> None:
    database_path = _seed_search_corpus(tmp_path)

    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(
            {
                "Belt filter Press": [
                    SearchCandidate(
                        passage_id="passage-d",
                        document_id="document-d",
                        page_start=3,
                        page_end=3,
                        text=(
                            "• Belt Filter Press - Contractors are 95% complete with the replacement of the "
                            "Belt Filter Press at the Sewer Treatment Plant."
                        ),
                        title=None,
                        meeting_date=None,
                        vector_score=0.80,
                    ),
                    SearchCandidate(
                        passage_id="passage-f",
                        document_id="document-d",
                        page_start=10,
                        page_end=10,
                        text="• Dungeons & Dragons – Take up a weapon and defeat various foes.",
                        title=None,
                        meeting_date=None,
                        vector_score=0.97,
                    ),
                ]
            }
        ),
        vector_store=FakeVectorStore(),
    )

    results = engine.search("Belt filter Press")

    assert [result.passage_id for result in results] == ["passage-d"]


def test_books_query_returns_multiple_book_club_passages(tmp_path: Path) -> None:
    database_path = _seed_search_corpus(tmp_path)

    engine = build_search_engine(
        database_path=database_path,
        lancedb_path=tmp_path / ".newsrag" / "lancedb",
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
        vector_searcher=FakeVectorSearcher(),
        vector_store=FakeVectorStore(),
    )

    results = engine.search("books")

    assert {result.passage_id for result in results[:3]} == {"passage-g", "passage-h", "passage-i"}


def test_keyword_search_uses_stemmed_passages(tmp_path: Path) -> None:
    database_path = _seed_search_corpus(tmp_path)

    candidates = search_keyword_candidates(database_path, "books", limit=10)

    assert {candidate.passage_id for candidate in candidates[:3]} == {
        "passage-g",
        "passage-h",
        "passage-i",
    }


def test_citation_format_uses_concise_terminal_style() -> None:
    assert (
        format_citation(title="Stormwater Report", meeting_date="2026-05-01", page_number=3)
        == "Stormwater Report — 2026-05-01 — p. 3"
    )


def test_format_search_results_returns_full_matching_passage() -> None:
    output = format_search_results(
        [
            SearchResult(
                passage_id="passage-d",
                document_id="document-d",
                page_start=3,
                page_end=3,
                text=(
                    "• Belt Filter Press - Contractors are 95% complete with the replacement of the Belt Filter Press at the Sewer Treatment Plant. "
                    "The previous Belt Filter Press had been in service for decades, and the cost of maintenance and the difficulty of sourcing replacement parts for the old equipment led to a need for its replacement. "
                    "The new Belt Filter Press is currently undergoing performance testing and final inspections. The replacement is being funded in part with infrastructure funds from the American Rescue Plan Act."
                ),
                citation="Mustang City Manager Report — 2026-05-01 — p. 3",
                score=1.0,
                keyword_score=0.1,
                vector_score=0.1,
            )
        ],
        query="Belt Filter Press",
    )

    assert "American Rescue Plan Act" in output
    assert "Belt Filter Press" in output
    assert "NewsRAG Search" in output


def test_search_command_requires_explicit_embedding_configuration(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    config_path = tmp_path / "missing-config.yaml"
    initialize_storage(data_dir)

    result = runner.invoke(
        app,
        [
            "--config-path",
            str(config_path),
            "--data-dir",
            str(data_dir),
            "search",
            "stormwater",
        ],
    )

    assert result.exit_code == 1
    assert "No embedding provider configured" in result.stdout
    assert "embedding.provider=openai_compatible" in result.stdout


def _seed_search_corpus(tmp_path: Path) -> Path:
    data_dir = tmp_path / ".newsrag"
    database_path = initialize_storage(data_dir).database

    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO sources(id, kind, submitted_reference, normalized_reference)
            VALUES(?, 'local_path', ?, ?)
            """,
            [
                ("source-a", "/tmp/stormwater.pdf", "/tmp/stormwater.pdf"),
                ("source-b", "/tmp/budget.pdf", "/tmp/budget.pdf"),
                ("source-c", "/tmp/zoning.pdf", "/tmp/zoning.pdf"),
                ("source-d", "/tmp/mustang.pdf", "/tmp/mustang.pdf"),
                ("source-html", "/tmp/notice.html", "/tmp/notice.html"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO source_artifacts(
                id, source_id, media_type, byte_size, content_hash, stored_path, acquired_at, state
            )
            VALUES(?, ?, ?, 10, ?, ?, CURRENT_TIMESTAMP, 'published')
            """,
            [
                ("artifact-a", "source-a", "application/pdf", "hash-a", "/tmp/stormwater.pdf"),
                ("artifact-b", "source-b", "application/pdf", "hash-b", "/tmp/budget.pdf"),
                ("artifact-c", "source-c", "application/pdf", "hash-c", "/tmp/zoning.pdf"),
                ("artifact-d", "source-d", "application/pdf", "hash-d", "/tmp/mustang.pdf"),
                ("artifact-html", "source-html", "text/html", "hash-html", "/tmp/notice.html"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO documents(
                id, source_path, source_url, title, source_hash, normalized_path,
                metadata_json, artifact_id
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "document-a",
                    "/tmp/stormwater.pdf",
                    "https://example.test/stormwater.pdf",
                    "Stormwater Report",
                    "hash-a",
                    "/tmp/stormwater-ocr.pdf",
                    '{"body": "Planning Commission", "document_type": "staff_report", "jurisdiction": "Example City", "meeting_date": "2026-05-01"}',
                    "artifact-a",
                ),
                (
                    "document-b",
                    "/tmp/budget.pdf",
                    "https://example.test/budget.pdf",
                    "Budget Packet",
                    "hash-b",
                    "/tmp/budget-ocr.pdf",
                    '{"body": "City Council", "document_type": "agenda_packet", "jurisdiction": "Example City", "meeting_date": "2026-04-20"}',
                    "artifact-b",
                ),
                (
                    "document-c",
                    "/tmp/zoning.pdf",
                    "https://example.test/zoning.pdf",
                    "Zoning Packet",
                    "hash-c",
                    "/tmp/zoning-ocr.pdf",
                    '{"body": "Zoning Board", "document_type": "packet", "jurisdiction": "Example City", "meeting_date": "2026-03-15"}',
                    "artifact-c",
                ),
                (
                    "document-d",
                    "/tmp/mustang.pdf",
                    "https://example.test/mustang.pdf",
                    "Mustang City Manager Report",
                    "hash-d",
                    "/tmp/mustang-ocr.pdf",
                    '{"body": "City Manager", "document_type": "manager_report", "jurisdiction": "Mustang", "meeting_date": "2026-05-01"}',
                    "artifact-d",
                ),
                (
                    "document-html",
                    "/tmp/notice.html",
                    "https://example.test/notice.html",
                    "Web Notice",
                    "hash-html",
                    None,
                    '{"body": "City Council", "document_type": "notice", "jurisdiction": "Example City"}',
                    "artifact-html",
                ),
            ],
        )
        connection.execute(
            """
            INSERT INTO source_units(
                id, artifact_id, document_id, ordinal, location_type, location_json,
                human_label, normalized_text, structure_json, extractor
            )
            VALUES(
                'unit-html-3', 'artifact-html', 'document-html', 3, 'html_block',
                '{"block_number": 3}', 'block 3', 'budget online public update',
                '{"element_kind": "paragraph", "heading_path": ["Web Notice", "Budget"]}',
                'static-html'
            )
            """
        )
        connection.executemany(
            """
            INSERT INTO passages(id, chunk_id, document_id, page_start, page_end, ordinal, text)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "passage-a",
                    "chunk-a",
                    "document-a",
                    3,
                    3,
                    1,
                    "downtown stormwater improvements",
                ),
                (
                    "passage-b",
                    "chunk-b",
                    "document-b",
                    7,
                    7,
                    1,
                    "budget hearing agenda item",
                ),
                (
                    "passage-c",
                    "chunk-c",
                    "document-c",
                    2,
                    2,
                    1,
                    "zoning map amendments",
                ),
                (
                    "passage-d",
                    "chunk-d",
                    "document-d",
                    3,
                    3,
                    1,
                    "• Belt Filter Press - Contractors are 95% complete with the replacement of the Belt Filter Press at the Sewer Treatment Plant.",
                ),
                (
                    "passage-e",
                    "chunk-e",
                    "document-d",
                    10,
                    10,
                    1,
                    "• Teen Kickback – There will be games, snacks, and craft supplies available.",
                ),
                (
                    "passage-f",
                    "chunk-f",
                    "document-d",
                    10,
                    10,
                    2,
                    "• Dungeons & Dragons – Take up a weapon and defeat various foes.",
                ),
                (
                    "passage-g",
                    "chunk-g",
                    "document-d",
                    10,
                    10,
                    3,
                    "• Paperbacks & Playdates Book Club – A low-pressure book club for stay-at-home parents.",
                ),
                (
                    "passage-h",
                    "chunk-h",
                    "document-d",
                    10,
                    10,
                    4,
                    "• Brown Bag Book Club – Bring your own lunch and discuss The Storyteller.",
                ),
                (
                    "passage-i",
                    "chunk-i",
                    "document-d",
                    10,
                    10,
                    5,
                    "• Geeky Cauldron Book Club – A book club for adults who love reading Young Adult books.",
                ),
                (
                    "passage-j",
                    "chunk-j",
                    "document-html",
                    3,
                    3,
                    1,
                    "budget online public update",
                ),
            ],
        )
        connection.execute(
            """
            UPDATE passages
            SET source_unit_start_id = 'unit-html-3', source_unit_end_id = 'unit-html-3'
            WHERE id = 'passage-j'
            """
        )
        connection.executemany(
            """
            INSERT INTO passages_fts(passage_id, text)
            VALUES(?, ?)
            """,
            [
                ("passage-a", "downtown stormwater improvements"),
                ("passage-b", "budget hearing agenda item"),
                ("passage-c", "zoning map amendments"),
                (
                    "passage-d",
                    "Belt Filter Press Contractors are 95 percent complete with the replacement of the Belt Filter Press at the Sewer Treatment Plant",
                ),
                (
                    "passage-e",
                    "Teen Kickback There will be games snacks and craft supplies available",
                ),
                ("passage-f", "Dungeons Dragons Take up a weapon and defeat various foes"),
                (
                    "passage-g",
                    "Paperbacks Playdates Book Club A low-pressure book club for stay-at-home parents",
                ),
                (
                    "passage-h",
                    "Brown Bag Book Club Bring your own lunch and discuss The Storyteller",
                ),
                (
                    "passage-i",
                    "Geeky Cauldron Book Club A book club for adults who love reading Young Adult books",
                ),
                ("passage-j", "budget online public update"),
            ],
        )
        connection.commit()

    return database_path
