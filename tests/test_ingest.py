from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from newsrag.cli import app
from newsrag.config import EmbeddingConfig
from newsrag.daemon import DaemonRunner
from newsrag.embeddings import (
    ChunkEmbedding,
    EmbeddingMetadata,
    QueryEmbedding,
    list_embedding_records,
)
from newsrag.ingest import (
    INGEST_JOB_KIND,
    ExtractedPage,
    IngestError,
    LanceDbVectorStore,
    build_ingest_handler,
    enqueue_ingest_url_job,
    list_chunk_vectors,
    list_chunks,
    list_documents,
    list_pages,
)
from newsrag.jobs import FAILED, create_job, get_job, list_jobs
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_ingest_command_enqueues_local_pdf_jobs(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_dir = tmp_path / "pdfs"
    source_dir.mkdir()
    (source_dir / "packet-a.pdf").write_bytes(b"%PDF-1.4\nA")
    (source_dir / "packet-b.PDF").write_bytes(b"%PDF-1.4\nB")
    (source_dir / "notes.txt").write_text("ignore me", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "ingest",
            str(source_dir),
            "--body",
            "City Council",
            "--document-type",
            "agenda_packet",
        ],
    )

    paths = initialize_storage(data_dir)
    jobs = list_jobs(paths.database)

    assert result.exit_code == 0
    assert "Enqueued 2 ingest job(s)" in result.stdout
    assert len(jobs) == 2
    assert all(job.kind == INGEST_JOB_KIND for job in jobs)
    assert jobs[0].payload["metadata"]["body"] == "City Council"
    assert jobs[0].payload["metadata"]["document_type"] == "agenda_packet"
    assert jobs[1].payload["metadata"]["body"] == "City Council"


def test_ingest_url_command_downloads_pdf_and_enqueues_job(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / ".newsrag"
    url = "https://example.gov/packet.pdf"

    monkeypatch.setattr("newsrag.ingest.httpx.get", _fake_pdf_getter(url, b"%PDF-1.4\nurl-pdf"))

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "ingest-url",
            url,
            "--body",
            "City Council",
            "--document-type",
            "agenda_packet",
        ],
    )

    paths = initialize_storage(data_dir)
    jobs = list_jobs(paths.database)

    assert result.exit_code == 0
    assert "Enqueued 1 ingest job(s)" in result.stdout
    assert len(jobs) == 1
    assert jobs[0].kind == INGEST_JOB_KIND
    assert Path(jobs[0].payload["path"]).parent == paths.downloaded_pdfs
    assert Path(jobs[0].payload["path"]).is_file()
    assert jobs[0].payload["metadata"]["body"] == "City Council"
    assert jobs[0].payload["metadata"]["document_type"] == "agenda_packet"
    assert jobs[0].payload["metadata"]["source_url"] == url
    assert "retrieved_at" in jobs[0].payload["metadata"]


def test_enqueue_ingest_url_job_reuses_hash_named_download_for_unchanged_content(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    url = "https://example.gov/packet.pdf"

    monkeypatch.setattr("newsrag.ingest.httpx.get", _fake_pdf_getter(url, b"%PDF-1.4\nunchanged"))

    first_job = enqueue_ingest_url_job(paths.database, storage_paths=paths, url=url)
    second_job = enqueue_ingest_url_job(paths.database, storage_paths=paths, url=url)

    assert first_job.payload["path"] == second_job.payload["path"]
    assert Path(first_job.payload["path"]).is_file()


def test_url_ingest_stores_source_url_and_retrieved_at(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    url = "https://example.gov/packet.pdf"

    monkeypatch.setattr("newsrag.ingest.httpx.get", _fake_pdf_getter(url, b"%PDF-1.4\nurl-pdf"))

    job = enqueue_ingest_url_job(paths.database, storage_paths=paths, url=url)
    retrieved_at = str(job.payload["metadata"]["retrieved_at"])

    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        ocr_runner=FakeOcrRunner(),
        text_extractor=FakeTextExtractor(pages=[ExtractedPage(page_number=1, text="Agenda")]),
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )

    asyncio.run(
        DaemonRunner(
            database_path=paths.database,
            handlers={INGEST_JOB_KIND: handler},
            poll_interval=0,
        ).run_cycle()
    )

    documents = list_documents(paths.database)

    assert len(documents) == 1
    assert documents[0].source_url == url
    assert documents[0].metadata["retrieved_at"] == retrieved_at


def test_ingest_url_rejects_non_pdf_responses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    url = "https://example.gov/not-a-pdf"

    def fake_get(target_url: str, *, follow_redirects: bool, timeout: float) -> httpx.Response:
        del follow_redirects, timeout
        request = httpx.Request("GET", target_url)
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "text/html"},
            content=b"<html>nope</html>",
        )

    monkeypatch.setattr("newsrag.ingest.httpx.get", fake_get)

    with pytest.raises(IngestError, match="PDF-like response"):
        enqueue_ingest_url_job(paths.database, storage_paths=paths, url=url)


def test_ingest_url_download_failures_fail_clearly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    url = "https://example.gov/packet.pdf"

    def fake_get(target_url: str, *, follow_redirects: bool, timeout: float) -> httpx.Response:
        del follow_redirects, timeout
        request = httpx.Request("GET", target_url)
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr("newsrag.ingest.httpx.get", fake_get)

    with pytest.raises(IngestError, match="Failed downloading"):
        enqueue_ingest_url_job(paths.database, storage_paths=paths, url=url)


def test_mocked_local_pdf_job_creates_document_pages_chunks_and_vector_records(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "packet.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nmock")
    paths = initialize_storage(data_dir)
    job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={
            "path": str(source_pdf.resolve()),
            "metadata": {"body": "City Council", "document_type": "agenda_packet"},
        },
    )

    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        ocr_runner=FakeOcrRunner(),
        text_extractor=FakeTextExtractor(
            pages=[
                ExtractedPage(page_number=1, text="Agenda item one"),
                ExtractedPage(page_number=2, text="Public comment section"),
            ]
        ),
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )

    processed = asyncio.run(
        DaemonRunner(
            database_path=paths.database,
            handlers={INGEST_JOB_KIND: handler},
            poll_interval=0,
        ).run_cycle()
    )

    documents = list_documents(paths.database)
    pages = list_pages(paths.database)
    chunks = list_chunks(paths.database)
    vectors = list_chunk_vectors(paths.lancedb)
    embedding_records = list_embedding_records(paths.database)

    assert processed is True
    assert get_job(paths.database, job.id).status == "done"
    assert len(documents) == 1
    assert documents[0].title == "packet"
    assert documents[0].metadata["body"] == "City Council"
    assert documents[0].metadata["source_filename"] == "packet.pdf"
    assert len(pages) == 2
    assert len(chunks) == 2
    assert len(vectors) == 2
    assert len(embedding_records) == 2
    assert {record.provider for record in embedding_records} == {"ollama"}
    assert {vector["document_id"] for vector in vectors} == {documents[0].id}


def test_reingesting_unchanged_pdf_does_not_duplicate_records(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "packet.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nmock")
    paths = initialize_storage(data_dir)

    create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {"body": "City Council"}},
        job_id="job-first",
    )
    create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {"body": "City Council"}},
        job_id="job-second",
    )

    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        ocr_runner=FakeOcrRunner(),
        text_extractor=FakeTextExtractor(pages=[ExtractedPage(page_number=1, text="Agenda")]),
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )
    runner_instance = DaemonRunner(
        database_path=paths.database,
        handlers={INGEST_JOB_KIND: handler},
        poll_interval=0,
    )

    asyncio.run(runner_instance.run_cycle())
    asyncio.run(runner_instance.run_cycle())

    assert len(list_documents(paths.database)) == 1
    assert len(list_pages(paths.database)) == 1
    assert len(list_chunks(paths.database)) == 1
    assert len(list_chunk_vectors(paths.lancedb)) == 1
    assert len(list_embedding_records(paths.database)) == 1


def test_ingest_failures_are_recorded_with_context(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "packet.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nmock")
    paths = initialize_storage(data_dir)
    job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {}},
    )

    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        ocr_runner=FailingOcrRunner(),
        text_extractor=FakeTextExtractor(pages=[ExtractedPage(page_number=1, text="Agenda")]),
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )

    asyncio.run(
        DaemonRunner(
            database_path=paths.database,
            handlers={INGEST_JOB_KIND: handler},
            poll_interval=0,
        ).run_cycle()
    )

    updated_job = get_job(paths.database, job.id)

    assert updated_job.status == FAILED
    assert str(source_pdf.resolve()) in (updated_job.error or "")
    assert "ocr boom" in (updated_job.error or "")
    assert list_documents(paths.database) == []
    assert list_chunks(paths.database) == []
    assert list_chunk_vectors(paths.lancedb) == []


@dataclass(frozen=True)
class FakeOcrRunner:
    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(source_path.read_bytes())


@dataclass(frozen=True)
class FailingOcrRunner:
    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        del source_path, output_path
        raise RuntimeError("ocr boom")


@dataclass(frozen=True)
class FakeTextExtractor:
    pages: list[ExtractedPage]

    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        del pdf_path
        return list(self.pages)


@dataclass(frozen=True)
class FakeEmbeddingProvider:
    metadata: EmbeddingMetadata = EmbeddingMetadata(
        provider="ollama",
        model="nomic-embed-text",
        version="latest",
    )

    def embed_query(self, text: str) -> QueryEmbedding:
        return QueryEmbedding(text=text, vector=(0.1, 0.2), metadata=self.metadata)

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        return [
            ChunkEmbedding(
                text=text,
                vector=(float(index + 1), float(index + 2)),
                metadata=self.metadata,
            )
            for index, text in enumerate(texts)
        ]


def _fake_pdf_getter(
    url: str,
    content: bytes,
) -> Callable[..., httpx.Response]:
    def fake_get(target_url: str, *, follow_redirects: bool, timeout: float) -> httpx.Response:
        del follow_redirects, timeout
        assert target_url == url
        request = httpx.Request("GET", target_url)
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "application/pdf"},
            content=content,
        )

    return fake_get
