from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from threading import Barrier

import lancedb  # type: ignore[import-untyped]
import pytest
from typer.testing import CliRunner

from newsrag.acquisition import (
    SOURCE_KIND_URL,
    AcquisitionError,
    AcquisitionRequest,
    HttpResponseStream,
    SafeSourceArtifactAcquirer,
    StagedSourceArtifact,
)
from newsrag.adapters import (
    AdapterError,
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
    LanceDbVectorStore,
    PdfPlumberTextExtractor,
    PyMuPdfTextExtractor,
    build_ingest_handler,
    build_pdf_text_extractor,
    enqueue_ingest_source,
    list_chunk_vectors,
    list_chunks,
    list_documents,
    list_pages,
)
from newsrag.jobs import (
    FAILED,
    create_job,
    get_job,
    list_jobs,
    retry_failed_job,
    set_job_status,
)
from newsrag.manifests import ManifestError, load_manifest
from newsrag.sources import normalize_url_reference
from newsrag.storage import initialize_storage

runner = CliRunner()


def test_ingest_command_enqueues_local_pdf_jobs(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_dir = tmp_path / "pdfs"
    source_dir.mkdir()
    (source_dir / "packet-a.pdf").write_bytes(b"%PDF-1.4\nA")
    (source_dir / "packet-b.PDF").write_bytes(b"%PDF-1.4\nB")
    (source_dir / "alias.pdf").symlink_to(source_dir / "packet-a.pdf")
    (source_dir / "notes.txt").write_text("ignore me", encoding="utf-8")
    os.mkfifo(source_dir / "special.pdf")
    external_dir = tmp_path / "external"
    external_dir.mkdir()
    (external_dir / "hidden.pdf").write_bytes(b"%PDF-1.4\nhidden")
    (source_dir / "linked-directory").symlink_to(external_dir, target_is_directory=True)

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
    assert "Queued by type: pdf=2" in result.stdout
    assert "Skipped by type: special=1, symlink=2, txt=1" in result.stdout
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


def test_source_type_hint_selects_pdf_without_bypassing_validation(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_file = tmp_path / "not-a-pdf.bin"
    source_file.write_bytes(b"plain text")

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "ingest", str(source_file), "--type", "pdf"],
    )
    paths = initialize_storage(data_dir)
    jobs = list_jobs(paths.database)
    assert result.exit_code == 0, result.stdout
    assert jobs[0].payload["source_type"] == "pdf"

    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        ocr_runner=FakeOcrRunner(),
        text_extractor=FakeTextExtractor(pages=[]),
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

    updated_job = get_job(paths.database, jobs[0].id)
    assert updated_job.status == FAILED
    assert "invalid signature" in (updated_job.error or "")


def test_ingest_rejects_unsupported_source_type_hint_before_enqueue(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_file = tmp_path / "packet.pdf"
    source_file.write_bytes(b"%PDF-1.4\nmock")

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "ingest", str(source_file), "--type", "html"],
    )

    paths = initialize_storage(data_dir)
    assert result.exit_code == 1
    assert "Unsupported source type 'html'" in result.stdout
    assert list_jobs(paths.database) == []


def test_ingest_rejects_unsupported_source_scheme_before_enqueue(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"

    result = runner.invoke(
        app,
        ["--data-dir", str(data_dir), "ingest", "ftp://example.gov/packet.pdf"],
    )

    paths = initialize_storage(data_dir)
    assert result.exit_code == 1
    assert "Unsupported source scheme 'ftp'" in result.stdout
    assert list_jobs(paths.database) == []


def test_ingest_command_enqueues_url_for_background_acquisition(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    url = "https://example.gov/packet.pdf?token=secret"

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "ingest",
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
    assert "token=secret" not in result.stdout
    assert len(jobs) == 1
    assert jobs[0].kind == INGEST_JOB_KIND
    assert jobs[0].payload["url"] == url
    assert "path" not in jobs[0].payload
    assert jobs[0].payload["metadata"]["body"] == "City Council"
    assert jobs[0].payload["metadata"]["document_type"] == "agenda_packet"
    assert list(paths.downloaded_pdfs.iterdir()) == []
    assert list(paths.source_artifacts.iterdir()) == []


def test_ingest_url_rejects_credentials_without_persisting_or_displaying_them(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"

    result = runner.invoke(
        app,
        [
            "--data-dir",
            str(data_dir),
            "ingest",
            "https://user:secret@example.gov/packet.pdf",
        ],
    )

    paths = initialize_storage(data_dir)
    assert result.exit_code == 1
    assert "URL credentials are not allowed" in result.stdout
    assert "user:secret" not in result.stdout
    assert list_jobs(paths.database) == []


def test_enqueue_ingest_url_does_not_fetch_before_daemon_processing(tmp_path: Path) -> None:
    paths = initialize_storage(tmp_path / ".newsrag")
    url = "https://example.gov/packet.pdf"

    first_job = enqueue_ingest_source(paths.database, source=url).jobs[0]
    second_job = enqueue_ingest_source(paths.database, source=url).jobs[0]

    assert first_job.payload["url"] == url
    assert second_job.payload["url"] == url
    assert list(paths.downloaded_pdfs.iterdir()) == []
    assert list(paths.source_artifacts.iterdir()) == []


def test_url_ingest_stores_source_url_and_acquisition_provenance(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    url = "https://example.gov/packet.pdf"
    resolved_url = "https://cdn.example.gov/packet.pdf"
    acquirer = FakeUrlAcquirer(
        content_by_url={url: b"%PDF-1.4\nurl-pdf"},
        resolved_url_by_url={url: resolved_url},
    )

    enqueue_ingest_source(paths.database, source=url)

    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        acquirer=acquirer,
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
    assert documents[0].metadata["retrieved_at"] == FakeUrlAcquirer.ACQUIRED_AT
    assert source is not None
    assert source["kind"] == "url"
    assert source["submitted_reference"] == url
    assert source["normalized_reference"] == url
    assert source["resolved_reference"] == resolved_url
    assert artifact is not None
    assert artifact["source_id"] == source["id"]
    assert artifact["acquired_at"] == FakeUrlAcquirer.ACQUIRED_AT
    assert artifact["reported_media_type"] == "application/pdf"
    assert json.loads(artifact["provenance_json"]) == {
        "redirects": [resolved_url],
        "resolved_url": resolved_url,
        "retrieved_at": FakeUrlAcquirer.ACQUIRED_AT,
        "submitted_url": url,
    }
    assert Path(artifact["stored_path"]).parent == paths.source_artifacts
    assert Path(artifact["stored_path"]).read_bytes() == b"%PDF-1.4\nurl-pdf"


def test_safe_url_acquisition_runs_inside_daemon(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    url = "https://example.gov/download"
    response = PipelineHttpResponse(
        status_code=200,
        headers={"content-type": "application/pdf"},
        chunks=(b"%PDF-1.4\nbackground",),
    )
    transport = PipelineHttpTransport(response=response)
    job = enqueue_ingest_source(paths.database, source=url).jobs[0]
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        acquirer=SafeSourceArtifactAcquirer(
            resolver=PublicResolver(),
            transport=transport,
        ),
        adapter=FakeSourceAdapter(),
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

    assert get_job(paths.database, job.id).status == "done"
    assert transport.requests == [(url, "93.184.216.34")]
    assert response.closed is True
    assert len(list(paths.source_artifacts.iterdir())) == 1


def test_ingest_url_reports_non_pdf_validation_failure_from_background_job(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    url = "https://example.gov/not-a-pdf"
    job = enqueue_ingest_source(paths.database, source=url).jobs[0]
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        acquirer=FakeUrlAcquirer(
            content_by_url={url: b"<html>nope</html>"},
            media_type_by_url={url: "text/html"},
        ),
        ocr_runner=FakeOcrRunner(),
        text_extractor=FakeTextExtractor(pages=[]),
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
    assert "Unsupported source type" in (updated_job.error or "")
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM source_artifacts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
        assert connection.execute("SELECT media_type FROM source_artifacts").fetchone() == (
            "application/octet-stream",
        )

    hinted_job = enqueue_ingest_source(paths.database, source=url, source_type="pdf").jobs[0]
    asyncio.run(
        DaemonRunner(
            database_path=paths.database,
            handlers={INGEST_JOB_KIND: handler},
            poll_interval=0,
        ).run_cycle()
    )
    assert get_job(paths.database, hinted_job.id).status == FAILED
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT media_type FROM source_artifacts").fetchone() == (
            "application/pdf",
        )


def test_ingest_manifest_enqueues_one_job_per_document(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - source: https://example.gov/packet-1.pdf
            title: Packet One
            meeting_date: 2026-05-01
            body: City Council
            document_type: agenda_packet
          - source: https://example.gov/packet-2.pdf
            jurisdiction: Example City
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )

    paths = initialize_storage(data_dir)
    jobs = list_jobs(paths.database)

    assert result.exit_code == 0
    assert "Enqueued 2 ingest job(s)" in result.stdout
    assert len(jobs) == 2

    jobs_by_url = {str(job.payload["url"]): job for job in jobs}
    first_job = jobs_by_url["https://example.gov/packet-1.pdf"]
    second_job = jobs_by_url["https://example.gov/packet-2.pdf"]

    assert first_job.payload["metadata"]["title"] == "Packet One"
    assert first_job.payload["metadata"]["meeting_date"] == "2026-05-01"
    assert second_job.payload["metadata"]["jurisdiction"] == "Example City"
    assert list(paths.source_artifacts.iterdir()) == []


def test_manifest_accepts_url_and_relative_local_path_with_type_hint(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_dir = tmp_path / "manifest"
    manifest_dir.mkdir()
    local_pdf = manifest_dir / "packet.pdf"
    local_pdf.write_bytes(b"%PDF-1.4\nlocal")
    manifest_path = manifest_dir / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - source: https://example.gov/remote.pdf
            type: pdf
          - source: ./packet.pdf
            type: pdf
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )

    paths = initialize_storage(data_dir)
    jobs = list_jobs(paths.database)
    assert result.exit_code == 0, result.stdout
    assert "Queued by type: pdf=2" in result.stdout
    assert len(jobs) == 2
    payloads_by_reference = {
        str(job.payload.get("url") or job.payload.get("path")): job.payload for job in jobs
    }
    assert "https://example.gov/remote.pdf" in payloads_by_reference
    assert str(local_pdf) in payloads_by_reference
    assert all(job.payload["source_type"] == "pdf" for job in jobs)


def test_manifest_validation_is_atomic_for_unsupported_type(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - source: https://example.gov/packet.pdf
          - source: https://example.gov/page.html
            type: html
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )

    paths = initialize_storage(data_dir)
    assert result.exit_code == 1
    assert "Unsupported source type 'html'" in result.stdout
    assert list_jobs(paths.database) == []


def test_manifest_missing_local_path_fails_before_any_jobs_are_created(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - source: https://example.gov/packet.pdf
          - source: ./missing.pdf
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )

    paths = initialize_storage(data_dir)
    assert result.exit_code == 1
    assert "Local source path does not exist" in result.stdout
    assert list_jobs(paths.database) == []


def test_invalid_manifest_missing_source_fails_without_enqueuing(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - title: Missing Source
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )
    paths = initialize_storage(data_dir)

    assert result.exit_code == 1
    assert "missing a non-empty 'source'" in result.stdout
    assert list_jobs(paths.database) == []


def test_invalid_manifest_invalid_meeting_date_fails_without_enqueuing(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - source: https://example.gov/packet.pdf
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


def test_invalid_manifest_duplicate_sources_fail_without_partial_work(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - source: https://example.gov/packet.pdf?token=secret
          - source: https://example.gov/packet.pdf?token=secret
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )
    paths = initialize_storage(data_dir)

    assert result.exit_code == 1
    assert "duplicate source" in result.stdout
    assert "token=secret" not in result.stdout
    assert list_jobs(paths.database) == []


def test_invalid_manifest_unsupported_fields_fail(tmp_path: Path) -> None:
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - source: https://example.gov/packet.pdf
            committee: Finance
        """.strip(),
        encoding="utf-8",
    )

    with pytest.raises(ManifestError, match="unsupported fields"):
        load_manifest(manifest_path)


def test_manifest_metadata_is_preserved_on_created_documents(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    manifest_path = tmp_path / "sources.yaml"
    manifest_path.write_text(
        """
        documents:
          - source: https://example.gov/packet.pdf
            title: Manifest Packet
            meeting_date: 2026-05-01
            body: City Council
            document_type: agenda_packet
            jurisdiction: Example City
        """.strip(),
        encoding="utf-8",
    )

    result = runner.invoke(
        app, ["--data-dir", str(data_dir), "ingest-manifest", str(manifest_path)]
    )
    assert result.exit_code == 0, result.stdout

    paths = initialize_storage(data_dir)
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        acquirer=FakeUrlAcquirer(
            content_by_url={"https://example.gov/packet.pdf": b"%PDF-1.4\nmanifest"}
        ),
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


def test_ingest_url_acquisition_failures_are_recorded_by_background_job(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    url = "https://example.gov/packet.pdf"
    job = enqueue_ingest_source(paths.database, source=url).jobs[0]
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        acquirer=FakeUrlAcquirer(
            content_by_url={},
            error=AcquisitionError("remote_request", "Request failed safely"),
        ),
        adapter=FakeSourceAdapter(),
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

    updated_job = get_job(paths.database, job.id)
    assert updated_job.status == FAILED
    assert "Acquisition remote_request failed" in (updated_job.error or "")
    assert "stage=artifact_acquisition" in caplog.text
    assert "ingest_stage_failed" in caplog.text
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM source_artifacts").fetchone() == (0,)


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
        "artifact_acquisition",
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
    assert adapter.inputs[0].artifact_path.parent == paths.source_artifacts
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
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone() == (1,)
        assert connection.execute("SELECT state FROM source_artifacts").fetchone() == (
            "processing",
        )
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
    assert source["resolved_reference"] == str(source_pdf.resolve())
    assert artifact is not None
    assert artifact["source_id"] == source["id"]
    assert artifact["content_hash"] == documents[0].source_hash
    assert artifact["byte_size"] == source_pdf.stat().st_size
    assert artifact["reported_media_type"] == "application/pdf"
    assert Path(artifact["stored_path"]).parent == paths.source_artifacts
    local_provenance = json.loads(artifact["provenance_json"])
    assert local_provenance["submitted_path"] == str(source_pdf.resolve())
    assert local_provenance["resolved_path"] == str(source_pdf.resolve())
    assert local_provenance["file_mtime_ns"] == source_pdf.stat().st_mtime_ns
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


def test_explicit_symlink_is_acquired_in_background_with_both_paths(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    target_path = tmp_path / "target.pdf"
    target_path.write_bytes(b"%PDF-1.4\nsymlink")
    submitted_path = tmp_path / "submitted.pdf"
    submitted_path.symlink_to(target_path)
    paths = initialize_storage(data_dir)
    jobs = list(enqueue_ingest_source(paths.database, source=str(submitted_path)).jobs)
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=FakeSourceAdapter(),
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

    assert len(jobs) == 1
    assert get_job(paths.database, jobs[0].id).status == "done"
    with sqlite3.connect(paths.database) as connection:
        connection.row_factory = sqlite3.Row
        source = connection.execute("SELECT * FROM sources").fetchone()
        artifact = connection.execute("SELECT * FROM source_artifacts").fetchone()
    assert source is not None
    assert source["submitted_reference"] == str(submitted_path)
    assert source["normalized_reference"] == str(submitted_path)
    assert source["resolved_reference"] == str(target_path.resolve())
    assert artifact is not None
    provenance = json.loads(artifact["provenance_json"])
    assert provenance["submitted_path"] == str(submitted_path)
    assert provenance["resolved_path"] == str(target_path.resolve())


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

    first_job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {"body": "City Council"}},
        job_id="job-first",
    )
    second_job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {"body": "Other Council"}},
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

    documents = list_documents(paths.database)
    first_result = get_job(paths.database, first_job.id).result
    updated_second_job = get_job(paths.database, second_job.id)
    second_result = updated_second_job.result

    assert len(documents) == 1
    assert documents[0].metadata["body"] == "City Council"
    assert first_result is not None
    assert first_result["outcome"] == "created"
    assert second_result is not None
    assert second_result["outcome"] == "duplicate_ignored"
    assert second_result["document_id"] == first_result["document_id"]
    assert second_result["artifact_id"] == first_result["artifact_id"]
    assert updated_second_job.payload == {}
    assert len(list_pages(paths.database)) == 1
    assert len(list_chunks(paths.database)) == 1
    assert len(list_chunk_vectors(paths.lancedb)) == 1
    assert len(list_embedding_records(paths.database)) == 2


def test_same_bytes_from_different_local_paths_ignore_the_second_source(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "renamed.pdf"
    content = b"%PDF-1.4\nidentical"
    first_pdf.write_bytes(content)
    second_pdf.write_bytes(content)
    paths = initialize_storage(data_dir)
    first_job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(first_pdf.resolve()), "metadata": {}},
        job_id="job-a-first",
    )
    second_job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(second_pdf.resolve()), "metadata": {}},
        job_id="job-z-second",
    )
    adapter = FakeSourceAdapter()
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=adapter,
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

    with sqlite3.connect(paths.database) as connection:
        sources = connection.execute(
            "SELECT submitted_reference FROM sources ORDER BY id"
        ).fetchall()
        artifact_count = connection.execute("SELECT COUNT(*) FROM source_artifacts").fetchone()
    assert get_job(paths.database, first_job.id).result["outcome"] == "created"  # type: ignore[index]
    updated_second_job = get_job(paths.database, second_job.id)
    assert updated_second_job.result["outcome"] == "duplicate_ignored"  # type: ignore[index]
    assert updated_second_job.payload == {}
    assert sources == [(str(first_pdf.resolve()),)]
    assert artifact_count == (1,)
    assert len(adapter.inputs) == 1


def test_same_bytes_from_different_urls_ignore_the_second_source(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    first_url = "https://one.example.gov/packet.pdf"
    second_url = "https://two.example.gov/renamed.pdf"
    content = b"%PDF-1.4\nidentical-url"
    acquirer = FakeUrlAcquirer(content_by_url={first_url: content, second_url: content})
    first_job = enqueue_ingest_source(paths.database, source=first_url).jobs[0]
    adapter = FakeSourceAdapter()
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        acquirer=acquirer,
        adapter=adapter,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )
    runner_instance = DaemonRunner(
        database_path=paths.database,
        handlers={INGEST_JOB_KIND: handler},
        poll_interval=0,
    )

    asyncio.run(runner_instance.run_cycle())
    second_job = enqueue_ingest_source(paths.database, source=second_url).jobs[0]
    asyncio.run(runner_instance.run_cycle())

    with sqlite3.connect(paths.database) as connection:
        sources = connection.execute(
            "SELECT submitted_reference FROM sources ORDER BY id"
        ).fetchall()
    first_result = get_job(paths.database, first_job.id).result
    second_result = get_job(paths.database, second_job.id).result
    assert first_result is not None and first_result["outcome"] == "created"
    assert second_result is not None and second_result["outcome"] == "duplicate_ignored"
    assert get_job(paths.database, second_job.id).payload == {}
    assert sources == [(first_url,)]
    assert list(paths.downloaded_pdfs.iterdir()) == []
    assert len(list(paths.source_artifacts.iterdir())) == 1
    assert len(adapter.inputs) == 1


def test_changed_source_bytes_are_preserved_without_replacing_document(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "packet.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nfirst")
    paths = initialize_storage(data_dir)
    first_job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {"title": "First title"}},
    )
    adapter = FakeSourceAdapter()
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=adapter,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )
    runner_instance = DaemonRunner(
        database_path=paths.database,
        handlers={INGEST_JOB_KIND: handler},
        poll_interval=0,
    )
    asyncio.run(runner_instance.run_cycle())

    source_pdf.write_bytes(b"%PDF-1.4\nchanged")
    changed_job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {"title": "Changed title"}},
    )
    asyncio.run(runner_instance.run_cycle())
    repeated_path = tmp_path / "same-change-at-new-path.pdf"
    repeated_path.write_bytes(b"%PDF-1.4\nchanged")
    repeated_change_job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(repeated_path.resolve()), "metadata": {}},
    )
    asyncio.run(runner_instance.run_cycle())

    documents = list_documents(paths.database)
    with sqlite3.connect(paths.database) as connection:
        artifact_states = connection.execute(
            "SELECT state FROM source_artifacts ORDER BY state"
        ).fetchall()
        source_count = connection.execute("SELECT COUNT(*) FROM sources").fetchone()
    first_result = get_job(paths.database, first_job.id).result
    changed_result = get_job(paths.database, changed_job.id).result
    repeated_result = get_job(paths.database, repeated_change_job.id).result
    assert first_result is not None and first_result["outcome"] == "created"
    assert changed_result is not None
    assert changed_result["outcome"] == "change_detected_artifact_saved"
    assert changed_result["document_id"] == first_result["document_id"]
    assert repeated_result is not None
    assert repeated_result["outcome"] == "change_already_detected"
    assert len(documents) == 1
    assert documents[0].title == "First title"
    assert artifact_states == [("change_detected",), ("published",)]
    assert source_count == (1,)
    assert len(adapter.inputs) == 1


def test_different_raw_bytes_with_same_extracted_text_create_distinct_documents(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    first_pdf.write_bytes(b"%PDF-1.4\nraw-a")
    second_pdf.write_bytes(b"%PDF-1.4\nraw-b")
    paths = initialize_storage(data_dir)
    jobs = [
        create_job(
            paths.database,
            kind=INGEST_JOB_KIND,
            payload={"path": str(path.resolve()), "metadata": {}},
        )
        for path in (first_pdf, second_pdf)
    ]
    adapter = FakeSourceAdapter()
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=adapter,
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

    assert [get_job(paths.database, job.id).result["outcome"] for job in jobs] == [  # type: ignore[index]
        "created",
        "created",
    ]
    assert len(list_documents(paths.database)) == 2
    assert len(adapter.inputs) == 2


def test_failed_unpublished_artifact_is_reused_on_retry(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    source_pdf = tmp_path / "packet.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\nretry")
    paths = initialize_storage(data_dir)
    job = create_job(
        paths.database,
        kind=INGEST_JOB_KIND,
        payload={"path": str(source_pdf.resolve()), "metadata": {}},
    )
    failing_handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=FailingSourceAdapter(),
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )
    asyncio.run(
        DaemonRunner(
            database_path=paths.database,
            handlers={INGEST_JOB_KIND: failing_handler},
            poll_interval=0,
        ).run_cycle()
    )

    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM source_artifacts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM documents").fetchone() == (0,)
    retry_failed_job(paths.database, job.id)
    successful_handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=FakeSourceAdapter(),
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )
    asyncio.run(
        DaemonRunner(
            database_path=paths.database,
            handlers={INGEST_JOB_KIND: successful_handler},
            poll_interval=0,
        ).run_cycle()
    )

    updated_job = get_job(paths.database, job.id)
    assert updated_job.status == "done"
    assert updated_job.result is not None and updated_job.result["outcome"] == "created"
    assert len(list_documents(paths.database)) == 1
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM source_artifacts").fetchone() == (1,)
        assert connection.execute("SELECT state FROM source_artifacts").fetchone() == ("published",)


def test_concurrent_exact_duplicates_converge_on_one_document(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    first_pdf = tmp_path / "first.pdf"
    second_pdf = tmp_path / "second.pdf"
    content = b"%PDF-1.4\nconcurrent"
    first_pdf.write_bytes(content)
    second_pdf.write_bytes(content)
    paths = initialize_storage(data_dir)
    jobs = [
        create_job(
            paths.database,
            kind=INGEST_JOB_KIND,
            payload={"path": str(path.resolve()), "metadata": {}},
        )
        for path in (first_pdf, second_pdf)
    ]
    adapter = BarrierSourceAdapter(Barrier(2))
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=adapter,
        embedding_provider=FakeEmbeddingProvider(),
        vector_store=LanceDbVectorStore(paths.lancedb),
    )

    def run_one_cycle() -> bool:
        return asyncio.run(
            DaemonRunner(
                database_path=paths.database,
                handlers={INGEST_JOB_KIND: handler},
                poll_interval=0,
            ).run_cycle()
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        processed = list(executor.map(lambda _: run_one_cycle(), range(2)))

    outcomes = sorted(
        str(get_job(paths.database, job.id).result["outcome"])  # type: ignore[index]
        for job in jobs
    )
    assert processed == [True, True]
    assert outcomes == ["created", "duplicate_ignored"]
    assert len(list_documents(paths.database)) == 1
    assert len(list_pages(paths.database)) == 1
    assert len(list_chunks(paths.database)) == 1
    assert len(list_chunk_vectors(paths.lancedb)) == 1
    assert len(list(paths.source_artifacts.iterdir())) == 1
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM sources").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM source_artifacts").fetchone() == (1,)


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


def test_missing_local_source_failure_is_recorded_by_background_job(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    paths = initialize_storage(data_dir)
    missing_path = tmp_path / "missing.pdf"
    jobs = list(enqueue_ingest_source(paths.database, source=str(missing_path)).jobs)
    handler = build_ingest_handler(
        data_dir=data_dir,
        embedding_config=EmbeddingConfig(),
        adapter=FakeSourceAdapter(),
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

    updated_job = get_job(paths.database, jobs[0].id)
    assert updated_job.status == FAILED
    assert "Acquisition local_validation failed" in (updated_job.error or "")
    assert str(missing_path) in (updated_job.error or "")
    with sqlite3.connect(paths.database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM source_artifacts").fetchone() == (0,)


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


@dataclass(frozen=True)
class FailingSourceAdapter:
    @property
    def media_types(self) -> tuple[str, ...]:
        return ("application/pdf",)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        raise AdapterError(f"extract failed for {artifact.artifact_path}")


@dataclass(frozen=True)
class BarrierSourceAdapter:
    barrier: Barrier

    @property
    def media_types(self) -> tuple[str, ...]:
        return ("application/pdf",)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        self.barrier.wait(timeout=5)
        return AdapterResult(
            media_type="application/pdf",
            units=(
                CanonicalSourceUnit(
                    ordinal=1,
                    location_type="page",
                    location={"page_number": 1},
                    human_label="p. 1",
                    normalized_text="Concurrent agenda",
                    structure={},
                    extractor=ExtractorIdentity(name="barrier-adapter"),
                ),
            ),
            extractor=ExtractorIdentity(name="barrier-adapter"),
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


@dataclass
class PipelineHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    chunks: tuple[bytes, ...]
    closed: bool = False

    def iter_raw(self) -> Iterator[bytes]:
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


@dataclass
class PipelineHttpTransport:
    response: PipelineHttpResponse
    requests: list[tuple[str, str]] = field(default_factory=list)

    def get(
        self,
        *,
        url: str,
        connect_ip: str,
        timeout_seconds: float,
    ) -> HttpResponseStream:
        del timeout_seconds
        self.requests.append((url, connect_ip))
        return self.response


@dataclass
class PublicResolver:
    def __call__(self, host: str, port: int) -> tuple[str, ...]:
        del host, port
        return ("93.184.216.34",)


@dataclass
class FakeUrlAcquirer:
    content_by_url: dict[str, bytes]
    resolved_url_by_url: dict[str, str] = field(default_factory=dict)
    media_type_by_url: dict[str, str] = field(default_factory=dict)
    error: AcquisitionError | None = None

    ACQUIRED_AT = "2026-09-04T00:00:00+00:00"

    def acquire(
        self,
        request: AcquisitionRequest,
        staging_dir: Path,
    ) -> StagedSourceArtifact:
        if self.error is not None:
            raise self.error
        assert request.kind == SOURCE_KIND_URL
        content = self.content_by_url[request.reference]
        resolved_url = self.resolved_url_by_url.get(request.reference, request.reference)
        staging_dir.mkdir(parents=True, exist_ok=True)
        staged_path = staging_dir / f"fake-{len(list(staging_dir.iterdir()))}.artifact"
        staged_path.write_bytes(content)
        redirects = [resolved_url] if resolved_url != request.reference else []
        return StagedSourceArtifact(
            source_kind=SOURCE_KIND_URL,
            submitted_reference=request.reference,
            normalized_reference=normalize_url_reference(request.reference),
            resolved_reference=resolved_url,
            staged_path=staged_path,
            content_hash=hashlib.sha256(content).hexdigest(),
            byte_size=len(content),
            acquired_at=self.ACQUIRED_AT,
            reported_media_type=self.media_type_by_url.get(
                request.reference,
                "application/pdf",
            ),
            provenance={
                "redirects": redirects,
                "resolved_url": resolved_url,
                "retrieved_at": self.ACQUIRED_AT,
                "submitted_url": request.reference,
            },
        )
