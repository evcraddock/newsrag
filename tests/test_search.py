from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.config import EmbeddingConfig
from newsrag.embeddings import ChunkEmbedding, EmbeddingMetadata, QueryEmbedding
from newsrag.search import (
    PassageVectorRecord,
    SearchCandidate,
    SearchFilters,
    SearchResult,
    build_search_engine,
    format_citation,
    format_search_results,
    search_keyword_candidates,
)
from newsrag.storage import initialize_storage

runner = CliRunner()


def _strip_ansi(value: str) -> str:
    return re.sub(r"\x1b\[[0-9;]*m", "", value)


@dataclass(frozen=True)
class FakeQueryEmbeddingProvider:
    metadata: EmbeddingMetadata = EmbeddingMetadata(
        provider="ollama",
        model="nomic-embed-text",
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


def test_search_command_reports_no_evidence_for_empty_corpus(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(app, ["--data-dir", str(data_dir), "search", "stormwater"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "No evidence found."


def _seed_search_corpus(tmp_path: Path) -> Path:
    data_dir = tmp_path / ".newsrag"
    database_path = initialize_storage(data_dir).database

    with sqlite3.connect(database_path) as connection:
        connection.executemany(
            """
            INSERT INTO documents(id, source_path, source_url, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
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
                ),
                (
                    "document-b",
                    "/tmp/budget.pdf",
                    "https://example.test/budget.pdf",
                    "Budget Packet",
                    "hash-b",
                    "/tmp/budget-ocr.pdf",
                    '{"body": "City Council", "document_type": "agenda_packet", "jurisdiction": "Example City", "meeting_date": "2026-04-20"}',
                ),
                (
                    "document-c",
                    "/tmp/zoning.pdf",
                    "https://example.test/zoning.pdf",
                    "Zoning Packet",
                    "hash-c",
                    "/tmp/zoning-ocr.pdf",
                    '{"body": "Zoning Board", "document_type": "packet", "jurisdiction": "Example City", "meeting_date": "2026-03-15"}',
                ),
                (
                    "document-d",
                    "/tmp/mustang.pdf",
                    "https://example.test/mustang.pdf",
                    "Mustang City Manager Report",
                    "hash-d",
                    "/tmp/mustang-ocr.pdf",
                    '{"body": "City Manager", "document_type": "manager_report", "jurisdiction": "Mustang", "meeting_date": "2026-05-01"}',
                ),
            ],
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
            ],
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
            ],
        )
        connection.commit()

    return database_path
