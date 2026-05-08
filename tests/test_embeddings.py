from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from newsrag.config import EmbeddingConfig
from newsrag.embeddings import (
    ChunkEmbedding,
    EmbeddingError,
    EmbeddingMetadata,
    EmbeddingProvider,
    QueryEmbedding,
    build_embedding_provider,
    create_embedding_record,
    list_embedding_records,
)
from newsrag.storage import initialize_storage


def test_ollama_provider_supports_query_and_chunk_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = build_embedding_provider(EmbeddingConfig(provider="ollama"))

    def fake_post(url: str, *, json: dict[str, object], timeout: float) -> httpx.Response:
        del timeout
        assert url == "http://127.0.0.1:11434/api/embed"
        assert json["model"] == "nomic-embed-text"
        inputs = json["input"]
        assert isinstance(inputs, list)

        request = httpx.Request("POST", url)
        if len(inputs) == 1:
            return httpx.Response(200, request=request, json={"embeddings": [[0.1, 0.2]]})
        return httpx.Response(
            200,
            request=request,
            json={"embeddings": [[0.3, 0.4], [0.5, 0.6]]},
        )

    monkeypatch.setattr("newsrag.embeddings.httpx.post", fake_post)

    query_embedding, chunk_embeddings = _exercise_provider(provider)

    assert query_embedding.vector == (0.1, 0.2)
    assert query_embedding.metadata.provider == "ollama"
    assert query_embedding.metadata.model == "nomic-embed-text"
    assert query_embedding.metadata.version == "latest"
    assert [chunk.text for chunk in chunk_embeddings] == ["chunk one", "chunk two"]
    assert [chunk.vector for chunk in chunk_embeddings] == [(0.3, 0.4), (0.5, 0.6)]


def test_ollama_provider_raises_for_malformed_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = build_embedding_provider(
        EmbeddingConfig(provider="ollama", model="nomic-embed-text")
    )

    def fake_post(url: str, *, json: dict[str, object], timeout: float) -> httpx.Response:
        del json, timeout
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"embeddings": ["bad"]})

    monkeypatch.setattr("newsrag.embeddings.httpx.post", fake_post)

    with pytest.raises(EmbeddingError, match="non-list embedding"):
        provider.embed_query("agenda")


def test_create_embedding_record_stores_provider_model_and_version(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    embedding = ChunkEmbedding(
        text="chunk one",
        vector=(0.1, 0.2, 0.3),
        metadata=EmbeddingMetadata(
            provider="ollama",
            model="nomic-embed-text",
            version="latest",
        ),
    )

    record = create_embedding_record(
        paths.database,
        source_kind="chunk",
        source_key="chunk-1",
        embedding=embedding,
        record_id="embedding-1",
    )

    assert record.id == "embedding-1"
    assert record.source_kind == "chunk"
    assert record.source_key == "chunk-1"
    assert record.provider == "ollama"
    assert record.model == "nomic-embed-text"
    assert record.version == "latest"
    assert record.dimensions == 3
    assert list_embedding_records(paths.database) == [record]


def _exercise_provider(
    provider: EmbeddingProvider,
) -> tuple[QueryEmbedding, list[ChunkEmbedding]]:
    query_embedding = provider.embed_query("city council agenda")
    chunk_embeddings = provider.embed_chunks(["chunk one", "chunk two"])
    return query_embedding, chunk_embeddings
