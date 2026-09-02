from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from newsrag.config import EmbeddingConfig
from newsrag.embeddings import (
    ChunkEmbedding,
    EmbeddingError,
    EmbeddingMetadata,
    EmbeddingProvider,
    OpenAICompatibleEmbeddingProvider,
    QueryEmbedding,
    build_embedding_provider,
    create_embedding_record,
    list_embedding_records,
)
from newsrag.storage import initialize_storage


def test_openai_compatible_provider_supports_local_query_and_chunk_embeddings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = build_embedding_provider(
        EmbeddingConfig(
            provider="openai_compatible",
            base_url="http://127.0.0.1:8080/v1",
            model="nomic-embed-text-v1.5",
        )
    )

    def fake_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        del timeout
        assert url == "http://127.0.0.1:8080/v1/embeddings"
        assert json["model"] == "nomic-embed-text-v1.5"
        assert headers is None
        inputs = json["input"]
        assert isinstance(inputs, list)

        request = httpx.Request("POST", url)
        if len(inputs) == 1:
            data = [{"index": 0, "embedding": [0.1, 0.2]}]
        else:
            data = [
                {"index": 0, "embedding": [0.3, 0.4]},
                {"index": 1, "embedding": [0.5, 0.6]},
            ]
        return httpx.Response(200, request=request, json={"data": data})

    monkeypatch.setattr("newsrag.embeddings.httpx.post", fake_post)

    query_embedding, chunk_embeddings = _exercise_provider(provider)

    assert query_embedding.vector == (0.1, 0.2)
    assert query_embedding.metadata.provider == "openai_compatible"
    assert query_embedding.metadata.model == "nomic-embed-text-v1.5"
    assert query_embedding.metadata.version == "latest"
    assert [chunk.text for chunk in chunk_embeddings] == ["chunk one", "chunk two"]
    assert [chunk.vector for chunk in chunk_embeddings] == [(0.3, 0.4), (0.5, 0.6)]


def test_openai_compatible_provider_reads_api_key_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_API_KEY", "secret-value")
    provider = build_embedding_provider(
        EmbeddingConfig(
            provider="openai_compatible",
            base_url="https://api.example.test/v1",
            model="text-embedding-3-small",
            api_key_env="TEST_EMBEDDING_API_KEY",
        )
    )

    def fake_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        del json, timeout
        assert headers == {"Authorization": "Bearer secret-value"}
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        )

    monkeypatch.setattr("newsrag.embeddings.httpx.post", fake_post)

    provider.embed_query("agenda")


def test_openai_compatible_provider_batches_and_restores_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:8080/v1",
        model="nomic-embed-text-v1.5",
        batch_size=2,
    )
    batches: list[list[str]] = []

    def fake_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        del timeout, headers
        inputs = json["input"]
        assert isinstance(inputs, list)
        batch = [str(value) for value in inputs]
        batches.append(batch)
        data = [
            {"index": index, "embedding": [float(int(text[-1])), 0.0]}
            for index, text in reversed(list(enumerate(batch)))
        ]
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json={"data": data})

    monkeypatch.setattr("newsrag.embeddings.httpx.post", fake_post)

    embeddings = provider.embed_chunks(["chunk 1", "chunk 2", "chunk 3"])

    assert batches == [["chunk 1", "chunk 2"], ["chunk 3"]]
    assert [embedding.vector for embedding in embeddings] == [
        (1.0, 0.0),
        (2.0, 0.0),
        (3.0, 0.0),
    ]


def test_openai_compatible_provider_rejects_dimensions_that_change_between_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:8080/v1",
        model="nomic-embed-text-v1.5",
        batch_size=1,
    )
    request_count = 0

    def fake_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        nonlocal request_count
        del json, timeout, headers
        request_count += 1
        vector = [0.1] if request_count == 1 else [0.2, 0.3]
        request = httpx.Request("POST", url)
        return httpx.Response(
            200,
            request=request,
            json={"data": [{"index": 0, "embedding": vector}]},
        )

    monkeypatch.setattr("newsrag.embeddings.httpx.post", fake_post)

    with pytest.raises(EmbeddingError, match="inconsistent embedding dimensions"):
        provider.embed_chunks(["one", "two"])


def test_openai_compatible_provider_requires_configured_api_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MISSING_EMBEDDING_API_KEY", raising=False)
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://api.example.test/v1",
        model="text-embedding-3-small",
        api_key_env="MISSING_EMBEDDING_API_KEY",
    )

    with pytest.raises(EmbeddingError, match="MISSING_EMBEDDING_API_KEY is not set"):
        provider.embed_query("agenda")


@pytest.mark.parametrize(
    ("payload", "inputs", "message"),
    [
        ([], ["one"], "unexpected payload shape"),
        ({}, ["one"], "missing a data list"),
        ({"data": [{"index": 0}]}, ["one"], "missing an embedding vector"),
        (
            {
                "data": [
                    {"index": 0, "embedding": [0.1]},
                    {"index": 0, "embedding": [0.2]},
                ]
            },
            ["one", "two"],
            "duplicate index 0",
        ),
        (
            {"data": [{"index": 1, "embedding": [0.1]}]},
            ["one"],
            "invalid index 1",
        ),
        (
            {"data": [{"index": 0, "embedding": [0.1]}]},
            ["one", "two"],
            "returned 1 vectors for 2 inputs",
        ),
        (
            {
                "data": [
                    {"index": 0, "embedding": [0.1]},
                    {"index": 1, "embedding": [0.2, 0.3]},
                ]
            },
            ["one", "two"],
            "inconsistent embedding dimensions",
        ),
        (
            {"data": [{"index": 0, "embedding": ["bad"]}]},
            ["one"],
            "non-numeric value",
        ),
    ],
)
def test_openai_compatible_provider_rejects_malformed_payloads(
    monkeypatch: pytest.MonkeyPatch,
    payload: Any,
    inputs: list[str],
    message: str,
) -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:8080/v1",
        model="nomic-embed-text-v1.5",
    )

    def fake_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        del json, timeout, headers
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, json=payload)

    monkeypatch.setattr("newsrag.embeddings.httpx.post", fake_post)

    with pytest.raises(EmbeddingError, match=message):
        provider.embed_chunks(inputs)


def test_openai_compatible_provider_does_not_expose_api_key_on_http_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TEST_EMBEDDING_API_KEY", "secret-value")
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="https://api.example.test/v1",
        model="text-embedding-3-small",
        api_key_env="TEST_EMBEDDING_API_KEY",
    )

    def fake_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        del json, timeout
        request = httpx.Request("POST", url, headers=headers)
        return httpx.Response(401, request=request)

    monkeypatch.setattr("newsrag.embeddings.httpx.post", fake_post)

    with pytest.raises(EmbeddingError) as error:
        provider.embed_query("agenda")

    assert "401 Unauthorized" in str(error.value)
    assert "secret-value" not in str(error.value)


def test_openai_compatible_provider_reports_http_and_json_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = OpenAICompatibleEmbeddingProvider(
        base_url="http://127.0.0.1:8080/v1",
        model="nomic-embed-text-v1.5",
    )

    def failed_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        del json, timeout, headers
        request = httpx.Request("POST", url)
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr("newsrag.embeddings.httpx.post", failed_post)
    with pytest.raises(EmbeddingError, match="embedding request failed"):
        provider.embed_query("agenda")

    def malformed_post(
        url: str,
        *,
        json: dict[str, object],
        timeout: float,
        headers: dict[str, str] | None,
    ) -> httpx.Response:
        del json, timeout, headers
        request = httpx.Request("POST", url)
        return httpx.Response(200, request=request, content=b"not-json")

    monkeypatch.setattr("newsrag.embeddings.httpx.post", malformed_post)
    with pytest.raises(EmbeddingError, match="response was not valid JSON"):
        provider.embed_query("agenda")


def test_build_embedding_provider_rejects_missing_and_removed_configuration() -> None:
    with pytest.raises(EmbeddingError, match="No embedding provider configured"):
        build_embedding_provider(EmbeddingConfig())

    with pytest.raises(EmbeddingError, match="native Ollama provider was removed"):
        build_embedding_provider(EmbeddingConfig(provider="ollama"))

    with pytest.raises(EmbeddingError, match="embedding.base_url is required"):
        build_embedding_provider(
            EmbeddingConfig(provider="openai_compatible", model="nomic-embed-text-v1.5")
        )

    with pytest.raises(EmbeddingError, match="embedding.model is required"):
        build_embedding_provider(
            EmbeddingConfig(
                provider="openai_compatible",
                base_url="http://127.0.0.1:8080/v1",
            )
        )


def test_create_embedding_record_stores_provider_model_and_version(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    embedding = ChunkEmbedding(
        text="chunk one",
        vector=(0.1, 0.2, 0.3),
        metadata=EmbeddingMetadata(
            provider="openai_compatible",
            model="nomic-embed-text-v1.5",
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
    assert record.provider == "openai_compatible"
    assert record.model == "nomic-embed-text-v1.5"
    assert record.version == "latest"
    assert record.dimensions == 3
    assert list_embedding_records(paths.database) == [record]


def _exercise_provider(
    provider: EmbeddingProvider,
) -> tuple[QueryEmbedding, list[ChunkEmbedding]]:
    query_embedding = provider.embed_query("city council agenda")
    chunk_embeddings = provider.embed_chunks(["chunk one", "chunk two"])
    return query_embedding, chunk_embeddings
