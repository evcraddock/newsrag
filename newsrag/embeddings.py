from __future__ import annotations

import math
import os
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import httpx

from newsrag.config import EmbeddingConfig

EMBEDDING_PROVIDER_OPENAI_COMPATIBLE = "openai_compatible"
DEFAULT_EMBEDDING_BATCH_SIZE = 64
DEFAULT_EMBEDDING_TIMEOUT_SECONDS = 30.0


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
class OpenAICompatibleEmbeddingProvider:
    """Embedding provider backed by the OpenAI-compatible embeddings API."""

    base_url: str
    model: str
    api_key_env: str | None = None
    batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    timeout_seconds: float = DEFAULT_EMBEDDING_TIMEOUT_SECONDS

    def __post_init__(self) -> None:
        if self.batch_size < 1:
            raise EmbeddingError("Embedding batch size must be at least 1")

    @property
    def metadata(self) -> EmbeddingMetadata:
        model_name, version = _split_model_identity(self.model)
        return EmbeddingMetadata(
            provider=EMBEDDING_PROVIDER_OPENAI_COMPATIBLE,
            model=model_name,
            version=version,
        )

    def embed_query(self, text: str) -> QueryEmbedding:
        vectors = self._embed_inputs([text])
        return QueryEmbedding(text=text, vector=vectors[0], metadata=self.metadata)

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        resolved_texts = list(texts)
        if not resolved_texts:
            return []

        vectors = self._embed_inputs(resolved_texts)
        metadata = self.metadata
        return [
            ChunkEmbedding(text=text, vector=vector, metadata=metadata)
            for text, vector in zip(resolved_texts, vectors, strict=True)
        ]

    def _embed_inputs(self, texts: list[str]) -> list[tuple[float, ...]]:
        headers = self._request_headers()
        vectors: list[tuple[float, ...]] = []
        expected_dimensions: int | None = None

        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            batch_vectors = self._request_batch(batch, headers=headers)
            batch_dimensions = len(batch_vectors[0])
            if expected_dimensions is None:
                expected_dimensions = batch_dimensions
            elif batch_dimensions != expected_dimensions:
                raise EmbeddingError(
                    "OpenAI-compatible response contained inconsistent embedding dimensions"
                )
            vectors.extend(batch_vectors)

        return vectors

    def _request_batch(
        self,
        texts: list[str],
        *,
        headers: dict[str, str] | None,
    ) -> list[tuple[float, ...]]:
        endpoint = f"{self.base_url.rstrip('/')}/embeddings"
        try:
            response = httpx.post(
                endpoint,
                json={"model": self.model, "input": texts},
                timeout=self.timeout_seconds,
                headers=headers,
            )
            response.raise_for_status()
            payload = response.json()
        except httpx.HTTPError as exc:
            raise EmbeddingError(
                f"OpenAI-compatible embedding request failed for {endpoint}: {exc}"
            ) from exc
        except ValueError as exc:
            raise EmbeddingError("OpenAI-compatible embedding response was not valid JSON") from exc

        return _extract_embedding_vectors(payload, expected_count=len(texts))

    def _request_headers(self) -> dict[str, str] | None:
        if self.api_key_env is None:
            return None

        api_key = os.getenv(self.api_key_env)
        if not api_key:
            raise EmbeddingError(
                f"Embedding API key environment variable {self.api_key_env} is not set"
            )
        return {"Authorization": f"Bearer {api_key}"}


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
    source_unit_start_id: str | None = None
    source_unit_end_id: str | None = None


def build_embedding_provider(config: EmbeddingConfig) -> EmbeddingProvider:
    """Build the OpenAI-compatible embedding provider from resolved config."""

    provider = config.provider
    if provider is None:
        raise EmbeddingError(
            "No embedding provider configured; set embedding.provider=openai_compatible"
        )

    normalized_provider = provider.lower()
    if normalized_provider == "ollama":
        raise EmbeddingError(
            "The native Ollama provider was removed; set provider=openai_compatible and "
            "base_url=http://127.0.0.1:11434/v1"
        )
    if normalized_provider != EMBEDDING_PROVIDER_OPENAI_COMPATIBLE:
        raise EmbeddingError(
            f"Embedding provider '{provider}' is not supported; use openai_compatible"
        )
    if config.base_url is None:
        raise EmbeddingError("embedding.base_url is required for provider=openai_compatible")
    if config.model is None:
        raise EmbeddingError("embedding.model is required for provider=openai_compatible")

    return OpenAICompatibleEmbeddingProvider(
        base_url=config.base_url,
        model=config.model,
        api_key_env=config.api_key_env,
    )


def create_embedding_record(
    database_path: Path,
    *,
    source_kind: str,
    source_key: str,
    embedding: QueryEmbedding | ChunkEmbedding,
    record_id: str | None = None,
    source_unit_start_id: str | None = None,
    source_unit_end_id: str | None = None,
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
                dimensions,
                source_unit_start_id,
                source_unit_end_id
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_record_id,
                source_kind,
                source_key,
                metadata.provider,
                metadata.model,
                metadata.version,
                len(embedding.vector),
                source_unit_start_id,
                source_unit_end_id,
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
            SELECT
                id,
                source_kind,
                source_key,
                provider,
                model,
                version,
                dimensions,
                created_at,
                source_unit_start_id,
                source_unit_end_id
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
            SELECT
                id,
                source_kind,
                source_key,
                provider,
                model,
                version,
                dimensions,
                created_at,
                source_unit_start_id,
                source_unit_end_id
            FROM embedding_records
            ORDER BY created_at ASC, id ASC
            """
        ).fetchall()

    return [_row_to_embedding_record(row) for row in rows]


def _extract_embedding_vectors(
    payload: Any,
    *,
    expected_count: int,
) -> list[tuple[float, ...]]:
    if not isinstance(payload, dict):
        raise EmbeddingError("OpenAI-compatible embedding response had an unexpected payload shape")

    data = payload.get("data")
    if not isinstance(data, list):
        raise EmbeddingError("OpenAI-compatible embedding response is missing a data list")
    if len(data) != expected_count:
        raise EmbeddingError(
            f"OpenAI-compatible embedding response returned {len(data)} vectors for "
            f"{expected_count} inputs"
        )

    vectors_by_index: dict[int, tuple[float, ...]] = {}
    dimensions: int | None = None
    for item in data:
        if not isinstance(item, dict):
            raise EmbeddingError(
                "OpenAI-compatible embedding response contained a non-object data item"
            )

        index = item.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < expected_count:
            raise EmbeddingError(
                f"OpenAI-compatible embedding response contained invalid index {index!r}"
            )
        if index in vectors_by_index:
            raise EmbeddingError(
                f"OpenAI-compatible embedding response contained duplicate index {index}"
            )

        embedding = item.get("embedding")
        if not isinstance(embedding, list):
            raise EmbeddingError(
                "OpenAI-compatible embedding response is missing an embedding vector"
            )
        vector = tuple(_coerce_vector_value(value) for value in embedding)
        if not vector:
            raise EmbeddingError(
                "OpenAI-compatible embedding response contained an empty embedding vector"
            )
        if dimensions is None:
            dimensions = len(vector)
        elif len(vector) != dimensions:
            raise EmbeddingError(
                "OpenAI-compatible response contained inconsistent embedding dimensions"
            )
        vectors_by_index[index] = vector

    return [vectors_by_index[index] for index in range(expected_count)]


def _coerce_vector_value(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise EmbeddingError("OpenAI-compatible embedding response contained a non-numeric value")

    resolved_value = float(value)
    if not math.isfinite(resolved_value):
        raise EmbeddingError("OpenAI-compatible embedding response contained a non-finite value")
    return resolved_value


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
        source_unit_start_id=(
            str(row["source_unit_start_id"]) if row["source_unit_start_id"] is not None else None
        ),
        source_unit_end_id=(
            str(row["source_unit_end_id"]) if row["source_unit_end_id"] is not None else None
        ),
    )
