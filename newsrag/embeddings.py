from __future__ import annotations

import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from newsrag.config import EmbeddingConfig

DEFAULT_OLLAMA_BASE_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "nomic-embed-text"
EMBEDDING_PROVIDER_OLLAMA = "ollama"


class EmbeddingError(Exception):
    """Raised when embedding provider setup or execution fails."""


@dataclass(frozen=True)
class EmbeddingMetadata:
    """Identity metadata for one embedding provider/model pair."""

    provider: str
    model: str
    version: str


@dataclass(frozen=True)
class QueryEmbedding:
    """One embedded query vector."""

    text: str
    vector: tuple[float, ...]
    metadata: EmbeddingMetadata


@dataclass(frozen=True)
class ChunkEmbedding:
    """One embedded chunk vector."""

    text: str
    vector: tuple[float, ...]
    metadata: EmbeddingMetadata


class EmbeddingProvider(Protocol):
    """Provider interface for query and chunk embeddings."""

    def embed_query(self, text: str) -> QueryEmbedding:
        """Embed one search query string."""

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        """Embed one or more chunk strings."""


@dataclass(frozen=True)
class OllamaEmbeddingProvider:
    """Embedding provider backed by the Ollama HTTP API."""

    model: str = DEFAULT_OLLAMA_MODEL
    base_url: str = DEFAULT_OLLAMA_BASE_URL

    @property
    def metadata(self) -> EmbeddingMetadata:
        model_name, version = _split_model_identity(self.model)
        return EmbeddingMetadata(
            provider=EMBEDDING_PROVIDER_OLLAMA,
            model=model_name,
            version=version,
        )

    def embed_query(self, text: str) -> QueryEmbedding:
        vectors = self._embed_inputs([text])
        if len(vectors) != 1:
            raise EmbeddingError(f"Ollama returned {len(vectors)} vectors for 1 query")
        return QueryEmbedding(text=text, vector=vectors[0], metadata=self.metadata)

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        resolved_texts = list(texts)
        if not resolved_texts:
            return []

        vectors = self._embed_inputs(resolved_texts)
        if len(vectors) != len(resolved_texts):
            raise EmbeddingError(
                f"Ollama returned {len(vectors)} vectors for {len(resolved_texts)} inputs"
            )

        metadata = self.metadata
        return [
            ChunkEmbedding(text=text, vector=vector, metadata=metadata)
            for text, vector in zip(resolved_texts, vectors, strict=True)
        ]

    def _embed_inputs(self, texts: list[str]) -> list[tuple[float, ...]]:
        endpoint = f"{self.base_url.rstrip('/')}/api/embed"
        try:
            response = httpx.post(
                endpoint, json={"model": self.model, "input": texts}, timeout=30.0
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(f"Ollama embedding request failed: {exc}") from exc
        except ValueError as exc:
            raise EmbeddingError("Ollama embedding response was not valid JSON") from exc

        return _extract_embedding_vectors(payload)


@dataclass(frozen=True)
class EmbeddingRecord:
    """One durable embedding provenance record."""

    id: str
    source_kind: str
    source_key: str
    provider: str
    model: str
    version: str
    dimensions: int
    created_at: str


def build_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    """Build an embedding provider from resolved config."""

    provider = config.provider
    if provider is None:
        raise EmbeddingError("No embedding provider configured")

    normalized_provider = provider.lower()
    if normalized_provider == EMBEDDING_PROVIDER_OLLAMA:
        return OllamaEmbeddingProvider(
            model=config.model or DEFAULT_OLLAMA_MODEL,
            base_url=config.base_url or DEFAULT_OLLAMA_BASE_URL,
        )

    raise EmbeddingError(f"Embedding provider '{provider}' is not implemented")


def create_embedding_record(
    database_path: Path,
    *,
    source_kind: str,
    source_key: str,
    embedding: QueryEmbedding | ChunkEmbedding,
    record_id: str | None = None,
) -> EmbeddingRecord:
    """Persist provider/model/version provenance for one embedding result."""

    resolved_record_id = record_id or f"embedding-{uuid.uuid4().hex[:8]}"
    metadata = embedding.metadata

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO embedding_records(
                id,
                source_kind,
                source_key,
                provider,
                model,
                version,
                dimensions
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_record_id,
                source_kind,
                source_key,
                metadata.provider,
                metadata.model,
                metadata.version,
                len(embedding.vector),
            ),
        )
        connection.commit()

    return get_embedding_record(database_path, resolved_record_id)


def get_embedding_record(database_path: Path, record_id: str) -> EmbeddingRecord:
    """Load one embedding provenance record by ID."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT id, source_kind, source_key, provider, model, version, dimensions, created_at
            FROM embedding_records
            WHERE id = ?
            """,
            (record_id,),
        ).fetchone()

    if row is None:
        raise KeyError(record_id)
    return _row_to_embedding_record(row)


def list_embedding_records(database_path: Path) -> list[EmbeddingRecord]:
    """List durable embedding provenance records."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, source_kind, source_key, provider, model, version, dimensions, created_at
            FROM embedding_records
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()

    return [_row_to_embedding_record(row) for row in rows]


def _extract_embedding_vectors(payload: Any) -> list[tuple[float, ...]]:
    if not isinstance(payload, dict):
        raise EmbeddingError("Ollama embedding response had an unexpected payload shape")

    embeddings = payload.get("embeddings")
    if not isinstance(embeddings, list):
        raise EmbeddingError("Ollama embedding response is missing an embeddings list")

    vectors: list[tuple[float, ...]] = []
    for embedding in embeddings:
        if not isinstance(embedding, list):
            raise EmbeddingError("Ollama embedding response contained a non-list embedding")

        vector = tuple(_coerce_vector_value(value) for value in embedding)
        if not vector:
            raise EmbeddingError("Ollama embedding response contained an empty embedding vector")
        vectors.append(vector)

    if not vectors:
        raise EmbeddingError("Ollama embedding response did not return any vectors")
    return vectors


def _coerce_vector_value(value: object) -> float:
    if isinstance(value, int | float):
        return float(value)
    raise EmbeddingError("Ollama embedding response contained a non-numeric value")


def _split_model_identity(model: str) -> tuple[str, str]:
    if ":" not in model:
        return model, "latest"

    base_model, _, version = model.partition(":")
    if not version:
        return base_model, "latest"
    return base_model, version


def _row_to_embedding_record(row: sqlite3.Row) -> EmbeddingRecord:
    return EmbeddingRecord(
        id=str(row["id"]),
        source_kind=str(row["source_kind"]),
        source_key=str(row["source_key"]),
        provider=str(row["provider"]),
        model=str(row["model"]),
        version=str(row["version"]),
        dimensions=int(row["dimensions"]),
        created_at=str(row["created_at"]),
    )
