from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.config import EmbeddingConfig
from newsrag.embeddings import ChunkEmbedding, EmbeddingMetadata, QueryEmbedding
from newsrag.ingest import ChunkVectorRecord, LanceDbVectorStore
from newsrag.search import (
    SearchCandidate,
    build_search_engine,
    format_citation,
    merge_search_candidates,
)
from newsrag.storage import StoragePaths, initialize_storage

runner = CliRunner()


@dataclass(frozen=True)
class FakeQueryEmbeddingProvider:
    metadata: EmbeddingMetadata = EmbeddingMetadata(
        provider="ollama",
        model="nomic-embed-text",
        version="latest",
    )

    def embed_query(self, text: str) -> QueryEmbedding:
        if text == "stormwater downtown":
            return QueryEmbedding(text=text, vector=(0.1, 0.1), metadata=self.metadata)
        return QueryEmbedding(text=text, vector=(2.0, 2.0), metadata=self.metadata)

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        del texts
        return []


def test_search_over_indexed_chunks_returns_ranked_cited_passages(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    _seed_search_corpus(paths)

    engine = build_search_engine(
        database_path=paths.database,
        lancedb_path=paths.lancedb,
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
    )

    results = engine.search("stormwater downtown")

    assert [result.chunk_id for result in results] == ["chunk-a", "chunk-b"]
    assert results[0].citation == "Stormwater Report — 2026-05-01 — p. 3"
    assert "downtown stormwater improvements" in results[0].text


def test_unrelated_query_returns_no_results_even_with_indexed_chunks(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    _seed_search_corpus(paths)

    engine = build_search_engine(
        database_path=paths.database,
        lancedb_path=paths.lancedb,
        embedding_config=EmbeddingConfig(),
        embedding_provider=FakeQueryEmbeddingProvider(),
    )

    results = engine.search("banana telescope")

    assert results == []


def test_keyword_vector_and_overlapping_candidates_merge_deterministically(
    tmp_path: Path,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    _seed_search_corpus(paths)

    keyword_candidates = [
        SearchCandidate(
            chunk_id="chunk-a",
            document_id="document-a",
            page_start=3,
            page_end=3,
            text="downtown stormwater improvements",
            title="Stormwater Report",
            meeting_date="2026-05-01",
            keyword_score=0.1,
        ),
        SearchCandidate(
            chunk_id="chunk-b",
            document_id="document-b",
            page_start=7,
            page_end=7,
            text="budget hearing agenda item",
            title="Budget Packet",
            meeting_date="2026-04-20",
            keyword_score=0.3,
        ),
    ]
    vector_candidates = [
        SearchCandidate(
            chunk_id="chunk-a",
            document_id="document-a",
            page_start=3,
            page_end=3,
            text="downtown stormwater improvements",
            title=None,
            meeting_date=None,
            vector_score=0.1,
        ),
        SearchCandidate(
            chunk_id="chunk-c",
            document_id="document-c",
            page_start=2,
            page_end=2,
            text="zoning map amendments",
            title=None,
            meeting_date=None,
            vector_score=0.3,
        ),
    ]

    results = merge_search_candidates(
        keyword_candidates,
        vector_candidates,
        database_path=paths.database,
        limit=5,
        keyword_weight=0.5,
        vector_weight=0.5,
    )

    assert [result.chunk_id for result in results] == ["chunk-a", "chunk-b", "chunk-c"]


def test_citation_format_uses_concise_terminal_style() -> None:
    assert (
        format_citation(title="Stormwater Report", meeting_date="2026-05-01", page_number=3)
        == "Stormwater Report — 2026-05-01 — p. 3"
    )


def test_search_command_reports_no_evidence_for_empty_corpus(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    initialize_storage(data_dir)

    result = runner.invoke(app, ["--data-dir", str(data_dir), "search", "stormwater"])

    assert result.exit_code == 0
    assert result.stdout.strip() == "No evidence found."


def _seed_search_corpus(paths: StoragePaths) -> None:
    resolved_paths = paths
    with sqlite3.connect(resolved_paths.database) as connection:
        connection.execute(
            """
            INSERT INTO documents(id, source_path, source_url, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                "document-a",
                "/tmp/stormwater.pdf",
                "Stormwater Report",
                "hash-a",
                "/tmp/stormwater-ocr.pdf",
                '{"meeting_date": "2026-05-01"}',
            ),
        )
        connection.execute(
            """
            INSERT INTO documents(id, source_path, source_url, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                "document-b",
                "/tmp/budget.pdf",
                "Budget Packet",
                "hash-b",
                "/tmp/budget-ocr.pdf",
                '{"meeting_date": "2026-04-20"}',
            ),
        )
        connection.execute(
            """
            INSERT INTO documents(id, source_path, source_url, title, source_hash, normalized_path, metadata_json)
            VALUES(?, ?, NULL, ?, ?, ?, ?)
            """,
            (
                "document-c",
                "/tmp/zoning.pdf",
                "Zoning Packet",
                "hash-c",
                "/tmp/zoning-ocr.pdf",
                '{"meeting_date": "2026-03-15"}',
            ),
        )
        connection.executemany(
            """
            INSERT INTO chunks(id, document_id, page_start, page_end, text)
            VALUES(?, ?, ?, ?, ?)
            """,
            [
                ("chunk-a", "document-a", 3, 3, "downtown stormwater improvements"),
                ("chunk-b", "document-b", 7, 7, "budget hearing agenda item"),
                ("chunk-c", "document-c", 2, 2, "zoning map amendments"),
            ],
        )
        connection.executemany(
            """
            INSERT INTO chunks_fts(chunk_id, text)
            VALUES(?, ?)
            """,
            [
                ("chunk-a", "downtown stormwater improvements"),
                ("chunk-b", "budget hearing agenda item"),
                ("chunk-c", "zoning map amendments"),
            ],
        )
        connection.commit()

    LanceDbVectorStore(resolved_paths.lancedb).add_chunks(
        [
            ChunkVectorRecord(
                chunk_id="chunk-a",
                document_id="document-a",
                page_start=3,
                page_end=3,
                text="downtown stormwater improvements",
                vector=(0.1, 0.1),
                metadata=EmbeddingMetadata(
                    provider="ollama",
                    model="nomic-embed-text",
                    version="latest",
                ),
            ),
            ChunkVectorRecord(
                chunk_id="chunk-b",
                document_id="document-b",
                page_start=7,
                page_end=7,
                text="budget hearing agenda item",
                vector=(0.8, 0.8),
                metadata=EmbeddingMetadata(
                    provider="ollama",
                    model="nomic-embed-text",
                    version="latest",
                ),
            ),
        ]
    )
