from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import lancedb  # type: ignore[import-untyped]
import pytest
from typer.testing import CliRunner

from newsrag.adapters import (
    AdapterInput,
    AdapterResult,
    CanonicalSourceUnit,
    ExtractorIdentity,
)
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
    PDF_EXTRACTOR_AUTO,
    PDF_EXTRACTOR_PDFPLUMBER,
    PDF_EXTRACTOR_PYMUPDF,
    PDF_EXTRACTOR_TABLE,
    ExtractedPage,
    FallbackTextExtractor,
    IngestError,
    LanceDbVectorStore,
    PdfPlumberTextExtractor,
    PyMuPdfTextExtractor,
    build_ingest_handler,
    build_pdf_text_extractor,
    enqueue_ingest_url_job,
    list_chunk_vectors,
    list_chunks,
    list_documents,
    list_pages,
)
from newsrag.jobs import FAILED, create_job, get_job, list_jobs, set_job_status
from newsrag.manifests import ManifestError, load_manifest
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

    assert result.exit_code == 0, result.stdout
    assert "Enqueued 2 ingest job(s)" in result.stdout
    assert len(jobs) == 2
    assert all(job.kind == INGEST_JOB_KIND for job in jobs)
    assert jobs[0].payload["metadata"]["body"] == "City Council"
    assert jobs[0].payload["metadata"]["document_type"] == "agenda_packet"
    assert jobs[1].payload["metadata"]["body"] == "City Council"


def test_ingest_command_records_pdf_extractor_mode(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "packet.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nmock")

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "ingest",
            str(source_pdf),
            "--pdf-extractor",
            PDF_EXTRACTOR_TABLE,
        ],
    )

    paths = initialize_storage(data_dir)
    jobs = list_jobs(paths.database)

    assert result.exit_code == 0, result.stdout
    assert len(jobs) == 1
    assert jobs[0].payload["pdf_extractor"] == PDF_EXTRACTOR_TABLE


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
    with sqlite3.connect(paths.database) as connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute("SELECT * FROM sources").fetchone()
        artifact = connection.execute("SELECT * FROM source_artifacts").fetchone()

    assert len(documents) == 1
    assert documents[0].source_url == url
    assert documents[0].metadata["retrieved_at"] == retrieved_at
    assert source is not None
    assert source["kind"] == "url"
    assert source["submitted_reference"] == url
    assert source["normalized_reference"] == url
    assert artifact is not None
    assert artifact["source_id"] == source["id"]
    assert artifact["acquired_at"] == retrieved_at


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


def test_ingest_manifest_enqueues_one_job_per_document(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - url: https://example.gov/packet-1.pdf
            title: Packet One
            meeting_date: 2026-05-01
            body: City Council
            document_type: agenda_packet
          - url: https://example.gov/packet-2.pdf
            jurisdiction: Example City
        """.strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "newsrag.ingest.httpx.get",
        _fake_pdf_url_map_getter(
            {
                "https://example.gov/packet-1.pdf": b"%PDF-1.4\npacket-1",
                "https://example.gov/packet-2.pdf": b"%PDF-1.4\npacket-2",
            }
        ),
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )

    paths = initialize_storage(data_dir)
    jobs = list_jobs(paths.database)

    assert result.exit_code == 0
    assert "Enqueued 2 ingest job(s)" in result.stdout
    assert len(jobs) == 2

    jobs_by_url = {str(job.payload["metadata"]["source_url"]): job for job in jobs}
    first_job = jobs_by_url["https://example.gov/packet-1.pdf"]
    second_job = jobs_by_url["https://example.gov/packet-2.pdf"]

    assert first_job.payload["metadata"]["title"] == "Packet One"
    assert first_job.payload["metadata"]["meeting_date"] == "2026-05-01"
    assert second_job.payload["metadata"]["jurisdiction"] == "Example City"


def test_invalid_manifest_missing_url_fails_without_enqueuing(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - title: Missing URL
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )
    paths = initialize_storage(data_dir)

    assert result.exit_code == 1
    assert "missing a non-empty 'url'" in result.stdout
    assert list_jobs(paths.database) == []


def test_invalid_manifest_invalid_meeting_date_fails_without_enqueuing(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - url: https://example.gov/packet.pdf
            meeting_date: 05-01-2026
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )
    paths = initialize_storage(data_dir)

    assert result.exit_code == 1
    assert "must be YYYY-MM-DD" in result.stdout
    assert list_jobs(paths.database) == []


def test_invalid_manifest_duplicate_urls_fail_without_partial_work(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - url: https://example.gov/packet.pdf
          - url: https://example.gov/packet.pdf
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )
    paths = initialize_storage(data_dir)

    assert result.exit_code == 1
    assert "duplicate URL" in result.stdout
    assert list_jobs(paths.database) == []


def test_invalid_manifest_unsupported_fields_fail(tmp_path: Path) -> None:
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - url: https://example.gov/packet.pdf
            committee: Finance
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="unsupported fields"):
        load_manifest(manifest_path)


def test_manifest_metadata_is_preserved_on_created_documents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - url: https://example.gov/packet.pdf
            title: Manifest Packet
            meeting_date: 2026-05-01
            body: City Council
            document_type: agenda_packet
            jurisdiction: Example City
        """.strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        "newsrag.ingest.httpx.get",
        _fake_pdf_getter("https://example.gov/packet.pdf", b"%PDF-1.4\nmanifest"),
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )
    assert result.exit_code == 0, result.stdout

    paths = initialize_storage(data_dir)
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
    assert documents[0].title == "Manifest Packet"
    assert documents[0].metadata["meeting_date"] == "2026-05-01"
    assert documents[0].metadata["body"] == "City Council"
    assert documents[0].metadata["document_type"] == "agenda_packet"
    assert documents[0].metadata["jurisdiction"] == "Example City"


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


def test_ingestion_pipeline_processes_an_injected_source_adapter(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "packet.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nmock")
    paths = initialize_storage(data_dir)
    job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {}},
    )
    adapter = FakeSourceAdapter()

    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=adapter,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )

    with caplog.at_level(logging.INFO):
        asyncio.run(
            DaemonRunner(
                database_path=paths.database,
                handlers={INGEST_JOB_KIND: handler},
                poll_interval=0,
            ).run_cycle()
        )

    assert get_job(paths.database, job.id).status == "done"
    for stage in (
        "artifact_preparation",
        "adapter_extraction",
        "chunking",
        "chunk_embeddings",
        "passage_embeddings",
        "publication",
    ):
        assert f"stage={stage}" in caplog.text
    assert "Adapter agenda" not in caplog.text
    assert len(adapter.inputs) == 1
    assert adapter.inputs[0].artifact_path.parent == paths.source_pdfs
    assert adapter.inputs[0].media_type == "application/pdf"
    with sqlite3.connect(paths.database) as connection:
        unit = connection.execute(
            """
            SELECT location_type, location_json, human_label, normalized_text, extractor
            FROM source_units
            """
        ).fetchone()
    assert unit is not None
    assert unit == ("page", '{"page_number": 1}', "p. 1", "Adapter agenda", "fake-adapter")


def test_failed_vector_publication_leaves_no_searchable_document(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "packet.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nmock")
    paths = initialize_storage(data_dir)
    job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {}},
    )
    chunk_vector_store = RecordingChunkVectorStore()
    passage_vector_store = FailingPassageVectorStore()

    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=FakeSourceAdapter(),
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=chunk_vector_store,
        passage_vector_store=passage_vector_store,
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
    assert "passage vector boom" in (updated_job.error or "")
    with sqlite3.connect(paths.database) as connection:
        for table_name in (
            "sources",
            "source_artifacts",
            "documents",
            "source_units",
            "pages",
            "chunks",
            "passages",
            "embedding_records",
            "chunks_fts",
            "passages_fts",
        ):
            count = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
            assert count == (0,)
    assert len(chunk_vector_store.added) == 1
    assert len(chunk_vector_store.removed_document_ids) == 1
    assert passage_vector_store.removed_document_ids == chunk_vector_store.removed_document_ids


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
    with sqlite3.connect(paths.database) as connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute("SELECT * FROM sources").fetchone()
        artifact = connection.execute("SELECT * FROM source_artifacts").fetchone()
        source_units = connection.execute(
            "SELECT * FROM source_units ORDER BY ordinal ASC"
        ).fetchall()
        chunk_ranges = connection.execute(
            """
            SELECT source_unit_start_id, source_unit_end_id
            FROM chunks
            ORDER BY page_start ASC
            """
        ).fetchall()
        passage_ranges = connection.execute(
            """
            SELECT source_unit_start_id, source_unit_end_id
            FROM passages
            ORDER BY page_start ASC, ordinal ASC
            """
        ).fetchall()
    passage_vectors = (
        lancedb.connect(paths.lancedb).open_table("passage_embeddings").to_arrow().to_pylist()
    )

    assert processed is True
    assert get_job(paths.database, job.id).status == "done"
    assert len(documents) == 1
    assert documents[0].title == "packet"
    assert documents[0].metadata["body"] == "City Council"
    assert documents[0].metadata["source_filename"] == "packet.pdf"
    assert len(pages) == 2
    assert len(chunks) == 2
    assert len(vectors) == 2
    assert len(embedding_records) == 4
    assert {record.source_kind for record in embedding_records} == {"chunk", "passage"}
    assert {record.provider for record in embedding_records} == {"openai_compatible"}
    assert source is not None
    assert source["kind"] == "local_path"
    assert source["submitted_reference"] == str(source_pdf.resolve())
    assert artifact is not None
    assert artifact["source_id"] == source["id"]
    assert artifact["content_hash"] == documents[0].source_hash
    assert artifact["byte_size"] == source_pdf.stat().st_size
    assert len(source_units) == 2
    assert [unit["ordinal"] for unit in source_units] == [1, 2]
    assert [json.loads(unit["location_json"]) for unit in source_units] == [
        {"page_number": 1},
        {"page_number": 2},
    ]
    assert [unit["human_label"] for unit in source_units] == ["p. 1", "p. 2"]
    unit_ids = [str(unit["id"]) for unit in source_units]
    assert [tuple(row) for row in chunk_ranges] == [
        (unit_ids[0], unit_ids[0]),
        (unit_ids[1], unit_ids[1]),
    ]
    assert [tuple(row) for row in passage_ranges] == [
        (unit_ids[0], unit_ids[0]),
        (unit_ids[1], unit_ids[1]),
    ]
    assert {vector["document_id"] for vector in vectors} == {documents[0].id}
    assert {vector["source_unit_start_id"] for vector in vectors} == set(unit_ids)
    assert {vector["source_unit_end_id"] for vector in vectors} == set(unit_ids)
    assert {vector["source_unit_start_id"] for vector in passage_vectors} == set(unit_ids)
    assert {vector["source_unit_end_id"] for vector in passage_vectors} == set(unit_ids)
    assert {record.source_unit_start_id for record in embedding_records} == set(unit_ids)
    assert {record.source_unit_end_id for record in embedding_records} == set(unit_ids)


def test_build_pdf_text_extractor_can_choose_extraction_path_under_test() -> None:
    assert isinstance(build_pdf_text_extractor(PDF_EXTRACTOR_AUTO), FallbackTextExtractor)
    assert isinstance(build_pdf_text_extractor(PDF_EXTRACTOR_PYMUPDF), PyMuPdfTextExtractor)
    assert isinstance(build_pdf_text_extractor(PDF_EXTRACTOR_PDFPLUMBER), PdfPlumberTextExtractor)
    assert isinstance(build_pdf_text_extractor(PDF_EXTRACTOR_TABLE), PdfPlumberTextExtractor)


def test_low_quality_primary_extraction_falls_back_and_persists_page_provenance(
    tmp_path: Path,
) -> None:
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
        ocr_runner=FakeOcrRunner(),
        text_extractor=FallbackTextExtractor(
            primary=FakeTextExtractor(
                pages=[ExtractedPage(page_number=1, text="", extractor="pymupdf")]
            ),
            fallback=FakeTextExtractor(
                pages=[ExtractedPage(page_number=1, text="Fallback agenda", extractor="pdfplumber")]
            ),
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

    pages = list_pages(paths.database)

    assert processed is True
    assert get_job(paths.database, job.id).status == "done"
    assert len(pages) == 1
    assert pages[0].document_id == list_documents(paths.database)[0].id
    assert pages[0].page_number == 1
    assert pages[0].text == "Fallback agenda"
    assert pages[0].extractor == "pdfplumber"


def test_extraction_failures_are_recorded_with_document_path_and_stage(tmp_path: Path) -> None:
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
        ocr_runner=FakeOcrRunner(),
        text_extractor=FallbackTextExtractor(primary=FailingTextExtractor()),
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
    assert "primary PDF text extraction" in (updated_job.error or "")
    assert "extract boom" in (updated_job.error or "")
    assert list_pages(paths.database) == []


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
    assert len(list_embedding_records(paths.database)) == 2


def test_retried_ingest_job_keeps_duplicate_detection_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "packet.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nmock")
    paths = initialize_storage(data_dir)

    first_job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {"body": "City Council"}},
        job_id="job-a-first",
    )
    failed_duplicate_job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {"body": "City Council"}},
        job_id="job-z-duplicate",
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
    set_job_status(paths.database, failed_duplicate_job.id, status=FAILED, error="transient boom")

    retry_result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "jobs", "retry", failed_duplicate_job.id],
    )
    asyncio.run(runner_instance.run_cycle())

    assert retry_result.exit_code == 0
    assert get_job(paths.database, first_job.id).status == "done"
    assert get_job(paths.database, failed_duplicate_job.id).status == "done"
    assert len(list_documents(paths.database)) == 1
    assert len(list_pages(paths.database)) == 1
    assert len(list_chunks(paths.database)) == 1
    assert len(list_chunk_vectors(paths.lancedb)) == 1
    assert len(list_embedding_records(paths.database)) == 2


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


@dataclass
class FakeSourceAdapter:
    inputs: list[AdapterInput] = field(default_factory=list)

    @property
    def media_types(self) -> tuple[str, ...]:
        return ("application/pdf",)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        self.inputs.append(artifact)
        return AdapterResult(
            media_type="application/pdf",
            units=(
                CanonicalSourceUnit(
                    ordinal=1,
                    location_type="page",
                    location={"page_number": 1},
                    human_label="p. 1",
                    normalized_text="Adapter agenda",
                    structure={},
                    extractor=ExtractorIdentity(name="fake-adapter"),
                ),
            ),
            extractor=ExtractorIdentity(name="fake-adapter"),
            derived_artifact_path=artifact.artifact_path,
        )


@dataclass
class RecordingChunkVectorStore:
    added: list[object] = field(default_factory=list)
    removed_document_ids: list[str] = field(default_factory=list)

    def add_chunks(self, chunks: Sequence[object]) -> None:
        self.added.extend(chunks)

    def delete_document(self, document_id: str) -> None:
        self.removed_document_ids.append(document_id)


@dataclass
class FailingPassageVectorStore:
    removed_document_ids: list[str] = field(default_factory=list)

    def add_passages(self, passages: Sequence[object]) -> None:
        del passages
        raise RuntimeError("passage vector boom")

    def delete_document(self, document_id: str) -> None:
        self.removed_document_ids.append(document_id)


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
class FailingTextExtractor:
    extractor_name: str = "failing"

    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        del pdf_path
        raise RuntimeError("extract boom")


@dataclass(frozen=True)
class FakeEmbeddingProvider:
    metadata: EmbeddingMetadata = EmbeddingMetadata(
        provider="openai_compatible",
        model="nomic-embed-text-v1.5",
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


def _fake_pdf_url_map_getter(
    url_map: dict[str, bytes],
) -> Callable[..., httpx.Response]:
    def fake_get(target_url: str, *, follow_redirects: bool, timeout: float) -> httpx.Response:
        del follow_redirects, timeout
        assert target_url in url_map
        request = httpx.Request("GET", target_url)
        return httpx.Response(
            200,
            request=request,
            headers={"Content-Type": "application/pdf"},
            content=url_map[target_url],
        )

    return fake_get
