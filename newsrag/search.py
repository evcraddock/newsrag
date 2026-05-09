from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol

import lancedb  # type: ignore[import-untyped]

from newsrag.config import EmbeddingConfig
from newsrag.embeddings import (
    EmbeddingMetadata,
    EmbeddingProvider,
    QueryEmbedding,
    build_embedding_provider,
    create_embedding_record,
)

DEFAULT_SEARCH_LIMIT = 5
DEFAULT_SEARCH_CANDIDATE_LIMIT = 20
DEFAULT_KEYWORD_WEIGHT = 0.6
DEFAULT_VECTOR_WEIGHT = 0.4
DEFAULT_MAX_VECTOR_DISTANCE = 1.0
DEFAULT_VECTOR_DISTANCE_MARGIN_WITH_KEYWORD = 0.08
DEFAULT_STRONG_VECTOR_DISTANCE_WITH_KEYWORD = 0.98
DEFAULT_SNIPPET_LENGTH = 700
DEFAULT_EMBEDDING_PROVIDER = "ollama"
PASSAGE_VECTOR_TABLE_NAME = "passage_embeddings"


class SearchError(Exception):
    """Raised when a search query cannot be executed."""


@dataclass(frozen=True)
class SearchFilters:
    """Metadata filters applied to search results."""

    body: str | None = None
    document_type: str | None = None
    jurisdiction: str | None = None
    source_url: str | None = None
    since: str | None = None
    until: str | None = None

    @property
    def is_active(self) -> bool:
        """Return whether any filter is active."""

        return bool(self.labels())

    def labels(self) -> tuple[str, ...]:
        """Return human-readable active filter labels."""

        labels = []
        for name, value in (
            ("body", self.body),
            ("document_type", self.document_type),
            ("jurisdiction", self.jurisdiction),
            ("source_url", self.source_url),
            ("since", self.since),
            ("until", self.until),
        ):
            resolved_value = _optional_string(value)
            if resolved_value is not None:
                labels.append(f"{name}={resolved_value}")
        return tuple(labels)

    def validate(self) -> None:
        """Validate filter values before search execution."""

        since_date = _parse_filter_date(self.since, option_name="--since")
        until_date = _parse_filter_date(self.until, option_name="--until")
        if since_date is not None and until_date is not None and since_date > until_date:
            raise SearchError("Invalid date range: --since must be on or before --until")


@dataclass(frozen=True)
class PassageVectorRecord:
    """One passage vector ready for LanceDB persistence."""

    passage_id: str
    document_id: str
    page_start: int
    page_end: int
    text: str
    vector: tuple[float, ...]
    metadata: EmbeddingMetadata


@dataclass(frozen=True)
class SearchCandidate:
    """One keyword or vector candidate before ranking."""

    passage_id: str
    document_id: str
    page_start: int
    page_end: int
    text: str
    title: str | None
    meeting_date: str | None
    body: str | None = None
    document_type: str | None = None
    jurisdiction: str | None = None
    source_url: str | None = None
    source_path: str | None = None
    keyword_score: float | None = None
    vector_score: float | None = None


@dataclass(frozen=True)
class SearchResult:
    """One ranked evidence result returned to the user."""

    passage_id: str
    document_id: str
    page_start: int
    page_end: int
    text: str
    citation: str
    score: float
    keyword_score: float | None
    vector_score: float | None
    title: str | None = None
    meeting_date: str | None = None
    body: str | None = None
    document_type: str | None = None
    jurisdiction: str | None = None
    source_url: str | None = None
    source_path: str | None = None


class Reranker(Protocol):
    """Protocol for optional result reranking."""

    def rerank(self, results: Sequence[SearchResult]) -> list[SearchResult]:
        """Return results in reranked order."""


class VectorSearcher(Protocol):
    """Protocol for vector candidate retrieval."""

    def search(self, query_embedding: QueryEmbedding, *, limit: int) -> list[SearchCandidate]:
        """Return vector candidates for one embedded query."""


class VectorStore(Protocol):
    """Protocol for vector persistence."""

    def add_passages(self, passages: Sequence[PassageVectorRecord]) -> None:
        """Persist embedded passage vectors."""


@dataclass(frozen=True)
class NoOpReranker:
    """Default reranker hook that preserves result order."""

    def rerank(self, results: Sequence[SearchResult]) -> list[SearchResult]:
        return list(results)


@dataclass(frozen=True)
class LanceDbPassageVectorStore:
    """Passage vector persistence backed by LanceDB."""

    lancedb_path: Path
    table_name: str = PASSAGE_VECTOR_TABLE_NAME

    def add_passages(self, passages: Sequence[PassageVectorRecord]) -> None:
        if not passages:
            return

        records = [
            {
                "passage_id": passage.passage_id,
                "document_id": passage.document_id,
                "page_start": passage.page_start,
                "page_end": passage.page_end,
                "text": passage.text,
                "vector": list(passage.vector),
                "provider": passage.metadata.provider,
                "model": passage.metadata.model,
                "version": passage.metadata.version,
            }
            for passage in passages
        ]

        database = lancedb.connect(self.lancedb_path)
        try:
            table = database.open_table(self.table_name)
        except ValueError:
            database.create_table(self.table_name, data=records)
            return

        table.add(records)


@dataclass(frozen=True)
class LanceDbPassageVectorSearcher:
    """Passage vector search backed by LanceDB."""

    lancedb_path: Path
    table_name: str = PASSAGE_VECTOR_TABLE_NAME
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
                    passage_id=str(row["passage_id"]),
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
    vector_searcher: VectorSearcher
    vector_store: VectorStore
    embedding_provider: EmbeddingProvider
    reranker: Reranker = NoOpReranker()
    keyword_weight: float = DEFAULT_KEYWORD_WEIGHT
    vector_weight: float = DEFAULT_VECTOR_WEIGHT

    def search(
        self,
        query: str,
        *,
        limit: int = DEFAULT_SEARCH_LIMIT,
        filters: SearchFilters | None = None,
    ) -> list[SearchResult]:
        normalized_query = query.strip()
        if not normalized_query:
            raise SearchError("Search query must not be empty")
        resolved_filters = filters or SearchFilters()
        resolved_filters.validate()
        if _count_passages(self.database_path) == 0:
            return []

        _ensure_passage_embeddings(
            self.database_path,
            embedding_provider=self.embedding_provider,
            vector_store=self.vector_store,
        )
        candidate_limit = max(limit * 4, DEFAULT_SEARCH_CANDIDATE_LIMIT)
        keyword_candidates = _filter_candidates_by_metadata(
            _expand_contextual_keyword_candidates(
                self.database_path,
                search_keyword_candidates(
                    self.database_path,
                    normalized_query,
                    limit=candidate_limit,
                ),
                limit=candidate_limit,
            ),
            filters=resolved_filters,
        )
        query_embedding = self.embedding_provider.embed_query(normalized_query)
        vector_candidates = _filter_vector_candidates(
            keyword_candidates,
            self.vector_searcher.search(query_embedding, limit=candidate_limit),
        )
        results = merge_search_candidates(
            keyword_candidates,
            vector_candidates,
            database_path=self.database_path,
            limit=limit,
            keyword_weight=self.keyword_weight,
            vector_weight=self.vector_weight,
            filters=resolved_filters,
        )
        return self.reranker.rerank(results)


def build_search_engine(
    *,
    database_path: Path,
    lancedb_path: Path,
    embedding_config: EmbeddingConfig,
    embedding_provider: EmbeddingProvider | None = None,
    vector_searcher: VectorSearcher | None = None,
    vector_store: VectorStore | None = None,
    reranker: Reranker | None = None,
) -> SearchEngine:
    """Build the default hybrid search engine for one corpus."""

    resolved_embedding_provider = embedding_provider or build_embedding_provider(
        _resolve_embedding_config(embedding_config)
    )
    resolved_vector_store = vector_store or LanceDbPassageVectorStore(lancedb_path)
    resolved_vector_searcher = vector_searcher or LanceDbPassageVectorSearcher(lancedb_path)
    return SearchEngine(
        database_path=database_path,
        vector_searcher=resolved_vector_searcher,
        vector_store=resolved_vector_store,
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
                passages.id AS passage_id,
                passages.document_id AS document_id,
                passages.page_start AS page_start,
                passages.page_end AS page_end,
                passages.text AS passage_text,
                documents.title AS title,
                documents.source_url AS source_url,
                documents.source_path AS source_path,
                documents.metadata_json AS metadata_json,
                bm25(passages_fts) AS keyword_score
            FROM passages_fts
            JOIN passages ON passages.id = passages_fts.passage_id
            JOIN documents ON documents.id = passages.document_id
            WHERE passages_fts MATCH ?
            ORDER BY bm25(passages_fts) ASC, passages.id ASC
            LIMIT ?
            """,
            (query, limit),
        ).fetchall()

    candidates: list[SearchCandidate] = []
    for row in rows:
        metadata = _load_metadata(row["metadata_json"])
        candidates.append(
            SearchCandidate(
                passage_id=str(row["passage_id"]),
                document_id=str(row["document_id"]),
                page_start=int(row["page_start"]),
                page_end=int(row["page_end"]),
                text=str(row["passage_text"]),
                title=str(row["title"]) if row["title"] is not None else None,
                meeting_date=_optional_string(metadata.get("meeting_date")),
                body=_optional_string(metadata.get("body")),
                document_type=_optional_string(metadata.get("document_type")),
                jurisdiction=_optional_string(metadata.get("jurisdiction")),
                source_url=_optional_string(row["source_url"])
                or _optional_string(metadata.get("source_url")),
                source_path=_optional_string(row["source_path"])
                or _optional_string(metadata.get("stored_source_path"))
                or _optional_string(metadata.get("source_filename")),
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
    filters: SearchFilters | None = None,
) -> list[SearchResult]:
    """Merge keyword and vector candidates into ranked search results."""

    resolved_filters = filters or SearchFilters()
    passage_context = _filter_passage_context_by_metadata(
        _load_passage_context(database_path, keyword_candidates, vector_candidates),
        filters=resolved_filters,
    )
    filtered_passage_ids = set(passage_context)
    keyword_normalized = _normalize_lower_better_scores(
        {
            candidate.passage_id: candidate.keyword_score
            for candidate in keyword_candidates
            if candidate.passage_id in filtered_passage_ids
        }
    )
    vector_normalized = _normalize_lower_better_scores(
        {
            candidate.passage_id: candidate.vector_score
            for candidate in vector_candidates
            if candidate.passage_id in filtered_passage_ids
        }
    )

    merged: dict[str, SearchResult] = {}
    for passage_id in sorted(set(passage_context)):
        context = passage_context[passage_id]
        score = keyword_weight * keyword_normalized.get(
            passage_id, 0.0
        ) + vector_weight * vector_normalized.get(passage_id, 0.0)
        merged[passage_id] = SearchResult(
            passage_id=context.passage_id,
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
            keyword_score=context.keyword_score,
            vector_score=context.vector_score,
            title=context.title,
            meeting_date=context.meeting_date,
            body=context.body,
            document_type=context.document_type,
            jurisdiction=context.jurisdiction,
            source_url=context.source_url,
            source_path=context.source_path,
        )

    return sorted(
        merged.values(),
        key=lambda result: (-result.score, result.citation, result.passage_id),
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


def format_search_results(
    results: Sequence[SearchResult],
    *,
    query: str | None = None,
    filters: SearchFilters | None = None,
) -> str:
    """Format ranked search results for terminal output."""

    resolved_filters = filters or SearchFilters()
    if not results:
        if resolved_filters.is_active:
            return f"No evidence found matching filters: {', '.join(resolved_filters.labels())}."
        return "No evidence found."

    lines = ["NewsRAG Search"]
    if resolved_filters.is_active:
        lines.append(f"filters: {', '.join(resolved_filters.labels())}")
    for result in results:
        lines.append(result.citation)
        metadata_line = _format_result_metadata(result)
        if metadata_line is not None:
            lines.append(metadata_line)
        lines.append(_truncate_text(" ".join(result.text.split()), query=query))
        lines.append("")
    return "\n".join(lines).rstrip()


def _ensure_passage_embeddings(
    database_path: Path,
    *,
    embedding_provider: EmbeddingProvider,
    vector_store: VectorStore,
) -> None:
    metadata = _embedding_metadata(embedding_provider)
    missing_passages = _load_missing_passages(database_path, metadata)
    if not missing_passages:
        return

    for batch in _batched(missing_passages, size=32):
        embeddings = embedding_provider.embed_chunks([passage.text for passage in batch])
        vector_store.add_passages(
            [
                PassageVectorRecord(
                    passage_id=passage.passage_id,
                    document_id=passage.document_id,
                    page_start=passage.page_start,
                    page_end=passage.page_end,
                    text=passage.text,
                    vector=embedding.vector,
                    metadata=embedding.metadata,
                )
                for passage, embedding in zip(batch, embeddings, strict=True)
            ]
        )
        for passage, embedding in zip(batch, embeddings, strict=True):
            create_embedding_record(
                database_path,
                source_kind="passage",
                source_key=passage.passage_id,
                embedding=embedding,
            )


@dataclass(frozen=True)
class _PassageForEmbedding:
    passage_id: str
    document_id: str
    page_start: int
    page_end: int
    text: str


def _load_missing_passages(
    database_path: Path,
    metadata: EmbeddingMetadata,
) -> list[_PassageForEmbedding]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                passages.id AS passage_id,
                passages.document_id AS document_id,
                passages.page_start AS page_start,
                passages.page_end AS page_end,
                passages.text AS passage_text
            FROM passages
            LEFT JOIN embedding_records
                ON embedding_records.source_kind = 'passage'
                AND embedding_records.source_key = passages.id
                AND embedding_records.provider = ?
                AND embedding_records.model = ?
                AND embedding_records.version = ?
            WHERE embedding_records.id IS NULL
            ORDER BY passages.document_id ASC, passages.page_start ASC, passages.ordinal ASC, passages.id ASC
            """,
            (metadata.provider, metadata.model, metadata.version),
        ).fetchall()

    return [
        _PassageForEmbedding(
            passage_id=str(row["passage_id"]),
            document_id=str(row["document_id"]),
            page_start=int(row["page_start"]),
            page_end=int(row["page_end"]),
            text=str(row["passage_text"]),
        )
        for row in rows
    ]


def _embedding_metadata(embedding_provider: EmbeddingProvider) -> EmbeddingMetadata:
    metadata = getattr(embedding_provider, "metadata", None)
    if isinstance(metadata, EmbeddingMetadata):
        return metadata
    return embedding_provider.embed_query("metadata probe").metadata


def _filter_vector_candidates(
    keyword_candidates: Sequence[SearchCandidate],
    vector_candidates: Sequence[SearchCandidate],
) -> list[SearchCandidate]:
    if not keyword_candidates:
        return list(vector_candidates)

    keyword_passage_ids = {candidate.passage_id for candidate in keyword_candidates}
    if len(keyword_candidates) >= 2:
        return [
            candidate
            for candidate in vector_candidates
            if candidate.passage_id in keyword_passage_ids
        ]

    overlapping = [
        candidate
        for candidate in vector_candidates
        if candidate.passage_id in keyword_passage_ids and candidate.vector_score is not None
    ]
    if not overlapping:
        return []

    best_overlap_distance = min(
        candidate.vector_score for candidate in overlapping if candidate.vector_score is not None
    )
    max_allowed_distance = min(
        DEFAULT_STRONG_VECTOR_DISTANCE_WITH_KEYWORD,
        best_overlap_distance + DEFAULT_VECTOR_DISTANCE_MARGIN_WITH_KEYWORD,
    )
    return [
        candidate
        for candidate in vector_candidates
        if candidate.passage_id in keyword_passage_ids
        or (candidate.vector_score is not None and candidate.vector_score <= max_allowed_distance)
    ]


def _expand_contextual_keyword_candidates(
    database_path: Path,
    keyword_candidates: Sequence[SearchCandidate],
    *,
    limit: int,
) -> list[SearchCandidate]:
    expanded = list(keyword_candidates)
    seen_passage_ids = {candidate.passage_id for candidate in keyword_candidates}
    chunk_hit_counts = _load_chunk_hit_counts(database_path, keyword_candidates)

    for candidate in keyword_candidates:
        if len(expanded) >= limit:
            break
        if chunk_hit_counts.get(candidate.passage_id, 0) > 1:
            continue
        if not candidate.text.startswith("•") or len(candidate.text) > 220:
            continue

        for neighbor in _load_adjacent_passages(database_path, candidate.passage_id):
            if len(expanded) >= limit or neighbor.passage_id in seen_passage_ids:
                continue
            if not neighbor.text.startswith("•") or len(neighbor.text) > 320:
                continue
            expanded.append(
                SearchCandidate(
                    passage_id=neighbor.passage_id,
                    document_id=neighbor.document_id,
                    page_start=neighbor.page_start,
                    page_end=neighbor.page_end,
                    text=neighbor.text,
                    title=neighbor.title,
                    meeting_date=neighbor.meeting_date,
                    body=neighbor.body,
                    document_type=neighbor.document_type,
                    jurisdiction=neighbor.jurisdiction,
                    source_url=neighbor.source_url,
                    source_path=neighbor.source_path,
                    keyword_score=(candidate.keyword_score or 0.0) + 0.5,
                )
            )
            seen_passage_ids.add(neighbor.passage_id)

    return expanded


def _resolve_embedding_config(embedding_config: EmbeddingConfig) -> EmbeddingConfig:
    if embedding_config.provider is not None:
        return embedding_config

    return EmbeddingConfig(
        provider=DEFAULT_EMBEDDING_PROVIDER,
        base_url=embedding_config.base_url,
        model=embedding_config.model,
        api_key_env=embedding_config.api_key_env,
    )


def _normalize_lower_better_scores(scores: dict[str, float | None]) -> dict[str, float]:
    filtered = {passage_id: score for passage_id, score in scores.items() if score is not None}
    if not filtered:
        return {}
    if len(filtered) == 1:
        only_passage_id = next(iter(filtered))
        return {only_passage_id: 1.0}

    values = list(filtered.values())
    minimum = min(values)
    maximum = max(values)
    if minimum == maximum:
        return dict.fromkeys(filtered, 1.0)

    return {
        passage_id: (maximum - score) / (maximum - minimum)
        for passage_id, score in filtered.items()
    }


def _load_passage_context(
    database_path: Path,
    keyword_candidates: Sequence[SearchCandidate],
    vector_candidates: Sequence[SearchCandidate],
) -> dict[str, SearchCandidate]:
    merged: dict[str, SearchCandidate] = {
        candidate.passage_id: candidate for candidate in keyword_candidates
    }
    passage_ids_to_load = [
        candidate.passage_id
        for candidate in vector_candidates
        if candidate.passage_id not in merged
    ]

    if passage_ids_to_load:
        placeholders = ", ".join("?" for _ in passage_ids_to_load)
        with sqlite3.connect(database_path) as connection:
            connection.row_factory = sqlite3.Row
            rows = connection.execute(
                f"""
                SELECT
                    passages.id AS passage_id,
                    passages.document_id AS document_id,
                    passages.page_start AS page_start,
                    passages.page_end AS page_end,
                    passages.text AS passage_text,
                    documents.title AS title,
                    documents.source_url AS source_url,
                    documents.source_path AS source_path,
                    documents.metadata_json AS metadata_json
                FROM passages
                JOIN documents ON documents.id = passages.document_id
                WHERE passages.id IN ({placeholders})
                """,
                tuple(passage_ids_to_load),
            ).fetchall()

        for row in rows:
            metadata = _load_metadata(row["metadata_json"])
            merged[str(row["passage_id"])] = SearchCandidate(
                passage_id=str(row["passage_id"]),
                document_id=str(row["document_id"]),
                page_start=int(row["page_start"]),
                page_end=int(row["page_end"]),
                text=str(row["passage_text"]),
                title=str(row["title"]) if row["title"] is not None else None,
                meeting_date=_optional_string(metadata.get("meeting_date")),
                body=_optional_string(metadata.get("body")),
                document_type=_optional_string(metadata.get("document_type")),
                jurisdiction=_optional_string(metadata.get("jurisdiction")),
                source_url=_optional_string(row["source_url"])
                or _optional_string(metadata.get("source_url")),
                source_path=_optional_string(row["source_path"])
                or _optional_string(metadata.get("stored_source_path"))
                or _optional_string(metadata.get("source_filename")),
            )

    for candidate in vector_candidates:
        existing = merged[candidate.passage_id]
        merged[candidate.passage_id] = SearchCandidate(
            passage_id=existing.passage_id,
            document_id=existing.document_id,
            page_start=existing.page_start,
            page_end=existing.page_end,
            text=existing.text,
            title=existing.title,
            meeting_date=existing.meeting_date,
            body=existing.body,
            document_type=existing.document_type,
            jurisdiction=existing.jurisdiction,
            source_url=existing.source_url,
            source_path=existing.source_path,
            keyword_score=existing.keyword_score,
            vector_score=candidate.vector_score,
        )

    return merged


def _load_chunk_hit_counts(
    database_path: Path,
    keyword_candidates: Sequence[SearchCandidate],
) -> dict[str, int]:
    if not keyword_candidates:
        return {}

    passage_ids = [candidate.passage_id for candidate in keyword_candidates]
    placeholders = ", ".join("?" for _ in passage_ids)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT candidate.id AS passage_id, chunk_counts.hit_count AS hit_count
            FROM passages AS candidate
            JOIN (
                SELECT chunk_id, COUNT(*) AS hit_count
                FROM passages
                WHERE id IN ({placeholders})
                GROUP BY chunk_id
            ) AS chunk_counts
                ON chunk_counts.chunk_id = candidate.chunk_id
            WHERE candidate.id IN ({placeholders})
            """,
            tuple(passage_ids + passage_ids),
        ).fetchall()

    return {str(row["passage_id"]): int(row["hit_count"]) for row in rows}


def _load_adjacent_passages(
    database_path: Path,
    passage_id: str,
) -> list[SearchCandidate]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            WITH origin AS (
                SELECT chunk_id, ordinal
                FROM passages
                WHERE id = ?
            )
            SELECT
                passages.id AS passage_id,
                passages.document_id AS document_id,
                passages.page_start AS page_start,
                passages.page_end AS page_end,
                passages.text AS passage_text,
                documents.title AS title,
                documents.source_url AS source_url,
                documents.source_path AS source_path,
                documents.metadata_json AS metadata_json
            FROM passages
            JOIN origin
                ON passages.chunk_id = origin.chunk_id
                AND ABS(passages.ordinal - origin.ordinal) = 1
            JOIN documents ON documents.id = passages.document_id
            ORDER BY passages.ordinal ASC, passages.id ASC
            """,
            (passage_id,),
        ).fetchall()

    candidates: list[SearchCandidate] = []
    for row in rows:
        metadata = _load_metadata(row["metadata_json"])
        candidates.append(
            SearchCandidate(
                passage_id=str(row["passage_id"]),
                document_id=str(row["document_id"]),
                page_start=int(row["page_start"]),
                page_end=int(row["page_end"]),
                text=str(row["passage_text"]),
                title=str(row["title"]) if row["title"] is not None else None,
                meeting_date=_optional_string(metadata.get("meeting_date")),
                body=_optional_string(metadata.get("body")),
                document_type=_optional_string(metadata.get("document_type")),
                jurisdiction=_optional_string(metadata.get("jurisdiction")),
                source_url=_optional_string(row["source_url"])
                or _optional_string(metadata.get("source_url")),
                source_path=_optional_string(row["source_path"])
                or _optional_string(metadata.get("stored_source_path"))
                or _optional_string(metadata.get("source_filename")),
            )
        )
    return candidates


def _filter_candidates_by_metadata(
    candidates: Sequence[SearchCandidate], *, filters: SearchFilters
) -> list[SearchCandidate]:
    filters.validate()
    return [candidate for candidate in candidates if _matches_search_filters(candidate, filters)]


def _filter_passage_context_by_metadata(
    passage_context: dict[str, SearchCandidate], *, filters: SearchFilters
) -> dict[str, SearchCandidate]:
    filters.validate()
    return {
        passage_id: candidate
        for passage_id, candidate in passage_context.items()
        if _matches_search_filters(candidate, filters)
    }


def _matches_search_filters(candidate: SearchCandidate, filters: SearchFilters) -> bool:
    return (
        _matches_text_filter(candidate.body, filters.body)
        and _matches_text_filter(candidate.document_type, filters.document_type)
        and _matches_text_filter(candidate.jurisdiction, filters.jurisdiction)
        and _matches_text_filter(candidate.source_url, filters.source_url)
        and _matches_date_filter(candidate.meeting_date, since=filters.since, until=filters.until)
    )


def _matches_text_filter(candidate_value: str | None, filter_value: str | None) -> bool:
    resolved_filter_value = _optional_string(filter_value)
    if resolved_filter_value is None:
        return True
    resolved_candidate_value = _optional_string(candidate_value)
    if resolved_candidate_value is None:
        return False
    return resolved_candidate_value.casefold() == resolved_filter_value.casefold()


def _matches_date_filter(meeting_date: str | None, *, since: str | None, until: str | None) -> bool:
    since_date = _parse_filter_date(since, option_name="--since")
    until_date = _parse_filter_date(until, option_name="--until")
    if since_date is None and until_date is None:
        return True

    parsed_meeting_date = _parse_metadata_date(meeting_date)
    if parsed_meeting_date is None:
        return False
    if since_date is not None and parsed_meeting_date < since_date:
        return False
    if until_date is not None and parsed_meeting_date > until_date:
        return False
    return True


def _parse_filter_date(value: str | None, *, option_name: str) -> date | None:
    resolved_value = _optional_string(value)
    if resolved_value is None:
        return None
    try:
        return date.fromisoformat(resolved_value)
    except ValueError as exc:
        raise SearchError(
            f"Invalid {option_name} date {resolved_value!r}; expected YYYY-MM-DD"
        ) from exc


def _parse_metadata_date(value: str | None) -> date | None:
    resolved_value = _optional_string(value)
    if resolved_value is None:
        return None
    try:
        return date.fromisoformat(resolved_value)
    except ValueError:
        return None


def _has_text(value: str | None) -> bool:
    return _optional_string(value) is not None


def _format_result_metadata(result: SearchResult) -> str | None:
    parts = []
    for name, value in (
        ("body", result.body),
        ("document_type", result.document_type),
        ("jurisdiction", result.jurisdiction),
        ("source_url", result.source_url),
    ):
        resolved_value = _optional_string(value)
        if resolved_value is not None:
            parts.append(f"{name}={resolved_value}")
    if not parts:
        return None
    return f"metadata: {'; '.join(parts)}"


def _count_passages(database_path: Path) -> int:
    with sqlite3.connect(database_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM passages").fetchone()
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


def _truncate_text(
    text: str, *, query: str | None, max_length: int = DEFAULT_SNIPPET_LENGTH
) -> str:
    if len(text) <= max_length:
        return text

    query_terms = [term.casefold() for term in (query or "").split() if term.strip()]
    lowered_text = text.casefold()
    match_index = min(
        (index for term in query_terms if (index := lowered_text.find(term)) >= 0),
        default=-1,
    )
    if match_index >= 0:
        half_window = max_length // 2
        start = max(0, match_index - half_window)
        end = min(len(text), start + max_length)
        start = max(0, end - max_length)
    else:
        start = 0
        end = min(len(text), max_length)

    snippet = text[start:end].strip()
    if start > 0:
        snippet = f"…{snippet}"
    if end < len(text):
        snippet = f"{snippet}…"
    return snippet


def _batched(
    values: Sequence[_PassageForEmbedding], *, size: int
) -> Iterator[Sequence[_PassageForEmbedding]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]
