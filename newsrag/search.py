from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import lancedb  # type: ignore[import-untyped]

from newsrag.config import EmbeddingConfig
from newsrag.embeddings import EmbeddingProvider, QueryEmbedding, build_embedding_provider
from newsrag.ingest import VECTOR_TABLE_NAME

DEFAULT_SEARCH_LIMIT = 5
DEFAULT_KEYWORD_WEIGHT = 0.5
DEFAULT_VECTOR_WEIGHT = 0.5
DEFAULT_MAX_VECTOR_DISTANCE = 1.0
DEFAULT_EMBEDDING_PROVIDER = "ollama"


class SearchError(Exception):
    """Raised when a search query cannot be executed."""


@dataclass(frozen=True)
class SearchCandidate:
    """One keyword or vector candidate before ranking."""

    chunk_id: str
    document_id: str
    page_start: int
    page_end: int
    text: str
    title: str | None
    meeting_date: str | None
    keyword_score: float | None = None
    vector_score: float | None = None


@dataclass(frozen=True)
class SearchResult:
    """One ranked evidence result returned to the user."""

    chunk_id: str
    document_id: str
    page_start: int
    page_end: int
    text: str
    citation: str
    score: float
    keyword_score: float | None
    vector_score: float | None


class Reranker(Protocol):
    """Protocol for optional result reranking."""

    def rerank(self, results: Sequence[SearchResult]) -> list[SearchResult]:
        """Return results in reranked order."""


@dataclass(frozen=True)
class NoOpReranker:
    """Default reranker hook that preserves result order."""

    def rerank(self, results: Sequence[SearchResult]) -> list[SearchResult]:
        return list(results)


@dataclass(frozen=True)
class LanceDbVectorSearcher:
    """Vector candidate retrieval backed by LanceDB."""

    lancedb_path: Path
    table_name: str = VECTOR_TABLE_NAME
    max_vector_distance: float | None = DEFAULT_MAX_VECTOR_DISTANCE

    def search(self, query_embedding: QueryEmbedding, *, limit: int) -> list[SearchCandidate]:
        database = lancedb.connect(self.lancedb_path)
        try:
            table = database.open_table(self.table_name)
        except ValueError:
            return []

        rows = table.search(list(query_embedding.vector)).limit(limit).to_list()
        candidates: list[SearchCandidate] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            distance = float(row["_distance"])
            if self.max_vector_distance is not None and distance > self.max_vector_distance:
                continue
            candidates.append(
                SearchCandidate(
                    chunk_id=str(row["chunk_id"]),
                    document_id=str(row["document_id"]),
                    page_start=int(row["page_start"]),
                    page_end=int(row["page_end"]),
                    text=str(row["text"]),
                    title=None,
                    meeting_date=None,
                    vector_score=distance,
                )
            )
        return candidates


@dataclass(frozen=True)
class SearchEngine:
    """Hybrid keyword/vector search over one corpus."""

    database_path: Path
    vector_searcher: LanceDbVectorSearcher
    embedding_provider: EmbeddingProvider
    reranker: Reranker = NoOpReranker()
    keyword_weight: float = DEFAULT_KEYWORD_WEIGHT
    vector_weight: float = DEFAULT_VECTOR_WEIGHT

    def search(self, query: str, *, limit: int = DEFAULT_SEARCH_LIMIT) -> list[SearchResult]:
        normalized_query = query.strip()
        if not normalized_query:
            raise SearchError("Search query must not be empty")
        if _count_chunks(self.database_path) == 0:
            return []

        keyword_candidates = search_keyword_candidates(
            self.database_path,
            normalized_query,
            limit=limit,
        )
        query_embedding = self.embedding_provider.embed_query(normalized_query)
        vector_candidates = self.vector_searcher.search(query_embedding, limit=limit)
        results = merge_search_candidates(
            keyword_candidates,
            vector_candidates,
            database_path=self.database_path,
            limit=limit,
            keyword_weight=self.keyword_weight,
            vector_weight=self.vector_weight,
        )
        return self.reranker.rerank(results)


def build_search_engine(
    *,
    database_path: Path,
    lancedb_path: Path,
    embedding_config: EmbeddingConfig,
    embedding_provider: EmbeddingProvider | None = None,
    vector_searcher: LanceDbVectorSearcher | None = None,
    reranker: Reranker | None = None,
) -> SearchEngine:
    """Build the default hybrid search engine for one corpus."""

    resolved_embedding_provider = embedding_provider or build_embedding_provider(
        _resolve_embedding_config(embedding_config)
    )
    resolved_vector_searcher = vector_searcher or LanceDbVectorSearcher(lancedb_path)
    return SearchEngine(
        database_path=database_path,
        vector_searcher=resolved_vector_searcher,
        embedding_provider=resolved_embedding_provider,
        reranker=reranker or NoOpReranker(),
    )


def search_keyword_candidates(
    database_path: Path,
    query: str,
    *,
    limit: int,
) -> list[SearchCandidate]:
    """Search keyword candidates from SQLite FTS5."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                chunks.id AS chunk_id,
                chunks.document_id AS document_id,
                chunks.page_start AS page_start,
                chunks.page_end AS page_end,
                chunks.text AS chunk_text,
                documents.title AS title,
                documents.metadata_json AS metadata_json,
                bm25(chunks_fts) AS keyword_score
            FROM chunks_fts
            JOIN chunks ON chunks.id = chunks_fts.chunk_id
            JOIN documents ON documents.id = chunks.document_id
            WHERE chunks_fts MATCH ?
            ORDER BY bm25(chunks_fts) ASC, chunks.id ASC
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()

    candidates: list[SearchCandidate] = []
    for row in rows:
        metadata = _load_metadata(row["metadata_json"])
        meeting_date = _optional_string(metadata.get("meeting_date"))
        candidates.append(
            SearchCandidate(
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                page_start=int(row["page_start"]),
                page_end=int(row["page_end"]),
                text=str(row["chunk_text"]),
                title=str(row["title"]) if row["title"] is not None else None,
                meeting_date=meeting_date,
                keyword_score=float(row["keyword_score"]),
            )
        )
    return candidates


def merge_search_candidates(
    keyword_candidates: Sequence[SearchCandidate],
    vector_candidates: Sequence[SearchCandidate],
    *,
    database_path: Path,
    limit: int,
    keyword_weight: float,
    vector_weight: float,
) -> list[SearchResult]:
    """Merge keyword and vector candidates into ranked search results."""

    chunk_context = _load_chunk_context(database_path, keyword_candidates, vector_candidates)
    keyword_normalized = _normalize_lower_better_scores(
        {candidate.chunk_id: candidate.keyword_score for candidate in keyword_candidates}
    )
    vector_normalized = _normalize_lower_better_scores(
        {candidate.chunk_id: candidate.vector_score for candidate in vector_candidates}
    )

    merged: dict[str, SearchResult] = {}
    for chunk_id in sorted(set(chunk_context)):
        context = chunk_context[chunk_id]
        keyword_score = context.keyword_score
        vector_score = context.vector_score
        score = keyword_weight * keyword_normalized.get(
            chunk_id, 0.0
        ) + vector_weight * vector_normalized.get(chunk_id, 0.0)
        merged[chunk_id] = SearchResult(
            chunk_id=context.chunk_id,
            document_id=context.document_id,
            page_start=context.page_start,
            page_end=context.page_end,
            text=context.text,
            citation=format_citation(
                title=context.title,
                meeting_date=context.meeting_date,
                page_number=context.page_start,
            ),
            score=score,
            keyword_score=keyword_score,
            vector_score=vector_score,
        )

    return sorted(
        merged.values(),
        key=lambda result: (-result.score, result.citation, result.chunk_id),
    )[:limit]


def format_citation(*, title: str | None, meeting_date: str | None, page_number: int) -> str:
    """Format one concise terminal citation."""

    parts = []
    if title:
        parts.append(title)
    if meeting_date:
        parts.append(meeting_date)
    parts.append(f"p. {page_number}")
    return " — ".join(parts)


def format_search_results(results: Sequence[SearchResult]) -> str:
    """Format ranked search results for terminal output."""

    if not results:
        return "No evidence found."

    lines = ["NewsRAG Search"]
    for result in results:
        lines.append(result.citation)
        lines.append(result.text)
        lines.append("")
    return "\n".join(lines).rstrip()


def _resolve_embedding_config(embedding_config: EmbeddingConfig) -> EmbeddingConfig:
    if embedding_config.provider is not None:
        return embedding_config

    return EmbeddingConfig(
        provider=DEFAULT_EMBEDDING_PROVIDER,
        base_url=embedding_config.base_url,
        model=embedding_config.model,
        api_key_env=embedding_config.api_key_env,
    )


def _normalize_lower_better_scores(
    scores: dict[str, float | None],
) -> dict[str, float]:
    filtered = {chunk_id: score for chunk_id, score in scores.items() if score is not None}
    if not filtered:
        return {}
    if len(filtered) == 1:
        only_chunk_id = next(iter(filtered))
        return {only_chunk_id: 1.0}

    values = list(filtered.values())
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        return dict.fromkeys(filtered, 1.0)

    return {
        chunk_id: (maximum - score) / (maximum - minimum) for chunk_id, score in filtered.items()
    }


def _load_chunk_context(
    database_path: Path,
    keyword_candidates: Sequence[SearchCandidate],
    vector_candidates: Sequence[SearchCandidate],
) -> dict[str, SearchCandidate]:
    merged: dict[str, SearchCandidate] = {
        candidate.chunk_id: candidate for candidate in keyword_candidates
    }
    chunk_ids_to_load = [
        candidate.chunk_id for candidate in vector_candidates if candidate.chunk_id not in merged
    ]

    if chunk_ids_to_load:
        placeholders = ", ".join("?" for _ in chunk_ids_to_load)
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT
                    chunks.id AS chunk_id,
                    chunks.document_id AS document_id,
                    chunks.page_start AS page_start,
                    chunks.page_end AS page_end,
                    chunks.text AS chunk_text,
                    documents.title AS title,
                    documents.metadata_json AS metadata_json
                FROM chunks
                JOIN documents ON documents.id = chunks.document_id
                WHERE chunks.id IN ({placeholders})
                """,
                tuple(chunk_ids_to_load),
            ).fetchall()

        for row in rows:
            metadata = _load_metadata(row["metadata_json"])
            merged[str(row["chunk_id"])] = SearchCandidate(
                chunk_id=str(row["chunk_id"]),
                document_id=str(row["document_id"]),
                page_start=int(row["page_start"]),
                page_end=int(row["page_end"]),
                text=str(row["chunk_text"]),
                title=str(row["title"]) if row["title"] is not None else None,
                meeting_date=_optional_string(metadata.get("meeting_date")),
            )

    for candidate in vector_candidates:
        existing = merged[candidate.chunk_id]
        merged[candidate.chunk_id] = SearchCandidate(
            chunk_id=existing.chunk_id,
            document_id=existing.document_id,
            page_start=existing.page_start,
            page_end=existing.page_end,
            text=existing.text,
            title=existing.title,
            meeting_date=existing.meeting_date,
            keyword_score=existing.keyword_score,
            vector_score=candidate.vector_score,
        )

    return merged


def _count_chunks(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM chunks").fetchone()
    if row is None:
        return 0
    return int(row[0])


def _load_metadata(raw_metadata: object) -> dict[str, object]:
    if not isinstance(raw_metadata, str):
        return {}
    try:
        metadata = json.loads(raw_metadata)
    except ValueError:
        return {}
    if not isinstance(metadata, dict):
        return {}
    return metadata


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None
