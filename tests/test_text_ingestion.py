from __future__ import annotations

import asyncio
import codecs
import hashlib
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import lancedb  # type: ignore[import-untyped]
import pytest
from typer.testing import CliRunner

from newsrag.acquisition import (
    AcquisitionRequest,
    HttpResponseStream,
    SafeSourceArtifactAcquirer,
    StagedSourceArtifact,
)
from newsrag.briefs import format_generated_brief, generate_document_brief
from newsrag.cli import app
from newsrag.config import EmbeddingConfig
from newsrag.daemon import DaemonRunner
from newsrag.discovery import create_document_profile
from newsrag.embeddings import ChunkEmbedding, EmbeddingMetadata, QueryEmbedding
from newsrag.facts import extract_document_facts
from newsrag.ingest import (
    INGEST_JOB_KIND,
    IngestionPipeline,
    enqueue_ingest_source,
    list_chunks,
    list_documents,
    list_pages,
)
from newsrag.jobs import Job, get_job, list_jobs
from newsrag.packets import (
    format_source_packet,
    load_packet_source_provenance,
)
from newsrag.pdf_adapter import ExtractedPage
from newsrag.refresh import REFRESH_JOB_KIND, RefreshPipeline, enqueue_refresh
from newsrag.reprocess import REPROCESS_JOB_KIND, ReprocessingPipeline, enqueue_reprocessing
from newsrag.search import (
    LanceDbPassageVectorStore,
    SearchFilters,
    SearchResult,
    merge_search_candidates,
    search_keyword_candidates,
)
from newsrag.sources import TEXT_MAX_SOURCE_BYTES
from newsrag.storage import StoragePaths, initialize_storage
from newsrag.text_adapter import MAX_TEXT_CHARS, MAX_TEXT_LINES

_RUNNER = CliRunner()


@dataclass
class FakeEmbeddingProvider:
    metadata: EmbeddingMetadata = EmbeddingMetadata("fake", "text-lifecycle", "1")

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        return [
            ChunkEmbedding(
                text=text,
                vector=(1.0, float(sum(text.encode("utf-8")) % 17 + 1)),
                metadata=self.metadata,
            )
            for text in texts
        ]

    def embed_query(self, text: str) -> QueryEmbedding:
        return QueryEmbedding(
            text=text,
            vector=(1.0, float(sum(text.encode("utf-8")) % 17 + 1)),
            metadata=self.metadata,
        )


@dataclass(frozen=True)
class CopyOcrRunner:
    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(source_path.read_bytes())


@dataclass(frozen=True)
class FakePdfTextExtractor:
    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        del pdf_path
        return [
            ExtractedPage(
                page_number=1,
                text="PDF council approved a drainage project.",
                extractor="fake-pdf",
            )
        ]


@dataclass
class Corpus:
    paths: StoragePaths
    embeddings: FakeEmbeddingProvider
    ingestion: IngestionPipeline
    refresh: RefreshPipeline
    reprocessing: ReprocessingPipeline
    runner: DaemonRunner

    def run(self, job: Job) -> Job:
        assert asyncio.run(self.runner.run_cycle())
        return get_job(self.paths.database, job.id)

    def drain(self) -> list[Job]:
        while asyncio.run(self.runner.run_cycle()):
            pass
        return list_jobs(self.paths.database)

    def ingest(
        self,
        source: str,
        *,
        metadata: dict[str, Any] | None = None,
        source_type: str | None = None,
    ) -> Job:
        job = enqueue_ingest_source(
            self.paths.database,
            source=source,
            metadata=metadata,
            source_type=source_type,
        ).jobs[0]
        return self.run(job)

    def rows(
        self,
        sql: str,
        parameters: Sequence[object] = (),
    ) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.paths.database) as connection:
            return connection.execute(sql, parameters).fetchall()


def _corpus(
    data_dir: Path,
    *,
    acquirer: SafeSourceArtifactAcquirer | None = None,
) -> Corpus:
    paths = initialize_storage(data_dir)
    embeddings = FakeEmbeddingProvider()
    ingestion = IngestionPipeline(
        storage_paths=paths,
        embedding_config=EmbeddingConfig(),
        acquirer=acquirer,
        ocr_runner=CopyOcrRunner(),
        text_extractor=FakePdfTextExtractor(),
        embedding_provider=embeddings,
    )
    refresh = RefreshPipeline(ingestion)
    reprocessing = ReprocessingPipeline(ingestion)
    runner = DaemonRunner(
        database_path=paths.database,
        handlers={
            INGEST_JOB_KIND: ingestion.handle_job,
            REFRESH_JOB_KIND: refresh.handle_job,
            REPROCESS_JOB_KIND: reprocessing.handle_job,
        },
        poll_interval=0,
    )
    return Corpus(paths, embeddings, ingestion, refresh, reprocessing, runner)


def _keyword_results(
    database_path: Path,
    query: str,
    *,
    include_history: bool = False,
    filters: SearchFilters | None = None,
) -> list[SearchResult]:
    candidates = search_keyword_candidates(
        database_path,
        query,
        limit=20,
        include_history=include_history,
    )
    return merge_search_candidates(
        candidates,
        (),
        database_path=database_path,
        limit=20,
        keyword_weight=1.0,
        vector_weight=0.0,
        filters=filters,
        include_history=include_history,
    )


def _lance_rows(path: Path, base_name: str) -> list[dict[str, Any]]:
    database = lancedb.connect(path)
    response = database.list_tables()
    names = sorted(
        name
        for name in response.tables
        if name == base_name or name.startswith(f"{base_name}__dim_")
    )
    return [row for name in names for row in database.open_table(name).to_arrow().to_pylist()]


def test_local_text_ingestion_preserves_lines_metadata_and_real_indexes(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path / ".newsrag")
    source = tmp_path / "council-notes.txt"
    content = (
        b"Council approved a $250,000 stormwater contract.\r\n"
        b"\r\n"
        b"Work starts Monday.\r"
        b"Public comment remains open.\n"
    )
    source.write_bytes(content)

    completed = corpus.ingest(
        str(source),
        metadata={
            "title": "Approved Council Notes",
            "body": "City Council",
            "meeting_date": "2026-06-01",
            "text_encoding": "reviewed-encoding",
            "source_size_bytes": 1,
        },
    )

    assert completed.status == "done", completed.error
    assert completed.result is not None and completed.result["outcome"] == "created"
    document = list_documents(corpus.paths.database)[0]
    assert document.title == "Approved Council Notes"
    assert document.metadata["body"] == "City Council"
    assert document.metadata["text_encoding"] == "reviewed-encoding"
    assert document.metadata["source_size_bytes"] == len(content)
    assert list_pages(corpus.paths.database) == []
    assert corpus.rows(
        "SELECT ordinal, location_type, human_label, normalized_text "
        "FROM source_units ORDER BY ordinal"
    ) == [
        (1, "text_line", "line 1", "Council approved a $250,000 stormwater contract."),
        (2, "text_line", "line 2", ""),
        (3, "text_line", "line 3", "Work starts Monday."),
        (4, "text_line", "line 4", "Public comment remains open."),
    ]
    assert [chunk.page_start for chunk in list_chunks(corpus.paths.database)] == [1, 3, 4]
    assert len(_lance_rows(corpus.paths.lancedb, "chunk_embeddings")) == 3
    assert len(_lance_rows(corpus.paths.lancedb, "passage_embeddings")) == 3
    assert corpus.rows("SELECT media_type, reported_media_type FROM source_artifacts") == [
        ("text/plain", "text/plain")
    ]
    options = json.loads(corpus.rows("SELECT ingestion_options_json FROM documents")[0][0])
    assert options["source_media_type"] == "text/plain"


@dataclass
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    chunks: tuple[bytes, ...]
    closed: bool = False

    def iter_raw(self) -> Iterator[bytes]:
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


@dataclass
class HttpTransport:
    responses: list[HttpResponse]
    requests: list[tuple[str, str, float]] = field(default_factory=list)

    def get(
        self,
        *,
        url: str,
        connect_ip: str,
        timeout_seconds: float,
    ) -> HttpResponseStream:
        self.requests.append((url, connect_ip, timeout_seconds))
        return self.responses.pop(0)


def _public_resolver(host: str, port: int) -> tuple[str, ...]:
    del host, port
    return ("93.184.216.34",)


def test_public_text_url_preserves_cp1252_charset_bytes_and_provenance(tmp_path: Path) -> None:
    content = "Council’s café approved the parks budget.\nSecond line.".encode("cp1252")
    response = HttpResponse(
        status_code=200,
        headers={
            "content-type": "text/plain; charset=windows-1252",
            "content-length": str(len(content)),
        },
        chunks=(content[:12], content[12:]),
    )
    transport = HttpTransport([response])
    corpus = _corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=_public_resolver,
            transport=transport,
        ),
    )
    url = "https://example.gov/download?id=public-notes"

    completed = corpus.ingest(url)

    assert completed.status == "done", completed.error
    document = list_documents(corpus.paths.database)[0]
    assert document.source_url == url
    assert document.metadata["text_encoding"] == "cp1252"
    assert corpus.rows("SELECT normalized_text FROM source_units ORDER BY ordinal") == [
        ("Council’s café approved the parks budget.",),
        ("Second line.",),
    ]
    artifact = corpus.rows(
        "SELECT reported_media_type, stored_path, provenance_json FROM source_artifacts"
    )[0]
    assert artifact[0] == "text/plain; charset=windows-1252"
    assert Path(artifact[1]).read_bytes() == content
    provenance = json.loads(artifact[2])
    assert provenance["submitted_url"] == url
    assert provenance["resolved_url"] == url
    assert transport.requests == [(url, "93.184.216.34", 30.0)]
    assert response.closed


def test_declared_ascii_and_latin1_charsets_flow_through_pipeline(tmp_path: Path) -> None:
    payloads = (
        (
            "https://example.gov/ascii",
            b"ASCII council notice.\n",
            "text/plain; charset=US-ASCII",
            "ascii",
            "ASCII council notice.",
        ),
        (
            "https://example.gov/latin1",
            b"Latin-1 caf\xe9 notice.\n",
            "text/plain; charset=iso-8859-1",
            "iso8859-1",
            "Latin-1 café notice.",
        ),
    )
    responses = [
        HttpResponse(
            status_code=200,
            headers={"content-type": media_type, "content-length": str(len(content))},
            chunks=(content,),
        )
        for _, content, media_type, _, _ in payloads
    ]
    corpus = _corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=_public_resolver,
            transport=HttpTransport(responses),
        ),
    )

    completed = [corpus.ingest(url) for url, _, _, _, _ in payloads]

    assert all(job.status == "done" for job in completed)
    documents = {
        document.source_url: document for document in list_documents(corpus.paths.database)
    }
    units = dict(
        corpus.rows(
            "SELECT documents.source_url, source_units.normalized_text "
            "FROM source_units JOIN documents ON documents.id = source_units.document_id"
        )
    )
    for url, _, _, encoding, expected_text in payloads:
        assert documents[url].metadata["text_encoding"] == encoding
        assert units[url] == expected_text


@pytest.mark.parametrize(
    "content,error",
    [
        (b"GIF89a printable payload", "non-text file signature"),
        (b"approved\x00contract", "unsupported control characters"),
        (b"caf\xe9", "not valid utf-8"),
    ],
)
def test_pipeline_rejects_binary_control_and_decoding_mismatches_without_indexes(
    tmp_path: Path,
    content: bytes,
    error: str,
) -> None:
    corpus = _corpus(tmp_path / ".newsrag")
    source = tmp_path / "invalid.txt"
    source.write_bytes(content)

    failed = corpus.ingest(str(source))

    assert failed.status == "failed"
    assert error in str(failed.error)
    assert corpus.rows("SELECT COUNT(*) FROM sources") == [(1,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_artifacts") == [(1,)]
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(0,)]
    assert _lance_rows(corpus.paths.lancedb, "chunk_embeddings") == []
    assert _lance_rows(corpus.paths.lancedb, "passage_embeddings") == []


def test_recursive_mixed_directory_ingests_text_html_pdf_and_skips_markdown(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / ".newsrag"
    sources = tmp_path / "sources"
    nested = sources / "nested"
    nested.mkdir(parents=True)
    (sources / "agenda.pdf").write_bytes(b"%PDF-1.4\nfixture")
    (nested / "notice.html").write_text(
        "<!doctype html><html><body><p>HTML zoning hearing.</p></body></html>",
        encoding="utf-8",
    )
    (nested / "minutes.TXT").write_text("Text transit hearing.", encoding="utf-8")
    (sources / "README.md").write_text("Markdown must not be ingested.", encoding="utf-8")

    queued = _RUNNER.invoke(app, ["--data-dir", str(data_dir), "ingest", str(sources)])
    corpus = _corpus(data_dir)
    completed = corpus.drain()

    assert queued.exit_code == 0, queued.stdout
    assert "Queued by type: html=1, pdf=1, text=1" in queued.stdout
    assert "Skipped by type: md=1" in queued.stdout
    assert len(completed) == 3
    assert all(job.status == "done" for job in completed)
    assert set(corpus.rows("SELECT media_type FROM source_artifacts")) == {
        ("application/pdf",),
        ("text/html",),
        ("text/plain",),
    }
    assert len(list_documents(corpus.paths.database)) == 3
    assert len(_lance_rows(corpus.paths.lancedb, "passage_embeddings")) == 3


def test_manifest_text_type_validation_is_atomic(tmp_path: Path) -> None:
    data_dir = tmp_path / ".newsrag"
    text_path = tmp_path / "notes.data"
    text_path.write_text("Explicit plain text.", encoding="utf-8")
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(
        f"""
        documents:
          - source: {text_path.name}
            type: text
          - source: https://example.gov/notes.txt
            type: markdown
        """.strip(),
        encoding="utf-8",
    )

    rejected = _RUNNER.invoke(
        app,
        ["--data-dir", str(data_dir), "ingest-manifest", str(manifest)],
    )

    paths = initialize_storage(data_dir)
    assert rejected.exit_code == 1
    assert "Unsupported source type 'markdown'" in rejected.stdout
    assert list_jobs(paths.database) == []


def test_markdown_is_not_autoaccepted_but_explicit_text_handles_unknown_extension(
    tmp_path: Path,
) -> None:
    corpus = _corpus(tmp_path / ".newsrag")
    markdown = tmp_path / "notes.md"
    markdown.write_text("Markdown-looking evidence.", encoding="utf-8")
    untyped = corpus.ingest(str(markdown))
    assert untyped.status == "failed"
    assert "Unsupported source type" in str(untyped.error)
    assert list_documents(corpus.paths.database) == []

    explicit = tmp_path / "approved.data"
    explicit.write_text("Explicit text evidence.", encoding="utf-8")
    accepted = corpus.ingest(str(explicit), source_type="text")
    assert accepted.status == "done", accepted.error
    assert corpus.rows("SELECT media_type FROM source_artifacts WHERE state = 'published'") == [
        ("text/plain",)
    ]


def test_text_refresh_duplicate_revision_history_filters_and_reactivation(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path / ".newsrag")
    source = tmp_path / "budget.txt"
    original = b"Header\nHistoricbudget approved for stormwater.\n"
    revised = b"Header\nRevisedbudget approved for transit.\n"
    source.write_bytes(original)
    first = corpus.ingest(
        str(source),
        metadata={
            "title": "Budget Notes",
            "body": "City Council",
            "meeting_date": "2026-07-01",
        },
    )
    assert first.result is not None
    source_id = str(first.result["source_id"])
    first_document_id = str(first.result["document_id"])

    duplicate = corpus.ingest(str(source), metadata={"title": "Ignored title"})
    assert duplicate.result is not None and duplicate.result["outcome"] == "duplicate_ignored"
    source.write_bytes(revised)
    detected = corpus.ingest(str(source))
    assert detected.result is not None
    assert detected.result["outcome"] == "change_detected_artifact_saved"
    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert refreshed.status == "done", refreshed.error
    assert refreshed.result is not None and refreshed.result["outcome"] == "revision_created"

    assert _keyword_results(corpus.paths.database, "Historicbudget") == []
    historical = _keyword_results(
        corpus.paths.database,
        "Historicbudget",
        include_history=True,
        filters=SearchFilters(source_type="text", body="City Council"),
    )
    current = _keyword_results(
        corpus.paths.database,
        "Revisedbudget",
        filters=SearchFilters(source_type="text", body="City Council"),
    )
    assert len(historical) == 1
    assert historical[0].citation == "Budget Notes — 2026-07-01 — line 2"
    assert historical[0].revision_number == 1
    assert historical[0].is_current_snapshot is False
    assert len(current) == 1
    assert (
        _keyword_results(
            corpus.paths.database,
            "Revisedbudget",
            filters=SearchFilters(source_type="pdf"),
        )
        == []
    )
    assert current[0].citation == "Budget Notes — 2026-07-01 — line 2"
    assert current[0].revision_number == 2
    assert current[0].is_current_snapshot is True

    source.write_bytes(original)
    reactivated = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert reactivated.status == "done", reactivated.error
    assert reactivated.result is not None
    assert reactivated.result["outcome"] == "revision_reactivated"
    assert reactivated.result["document_id"] == first_document_id
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(2,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(2,)]
    assert corpus.rows("SELECT publication_generation FROM sources") == [(3,)]
    restored = _keyword_results(corpus.paths.database, "Historicbudget")
    assert len(restored) == 1 and restored[0].is_current_snapshot is True


def test_ingested_text_drives_discovery_brief_and_packet_provenance(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path / ".newsrag")
    source = tmp_path / "contract.txt"
    content = (
        "Council approved a $250,000 stormwater contract with ABC Construction.\n"
        "Work must be completed by June 1, 2026 and is 95% funded.\n"
    )
    source.write_text(content, encoding="utf-8")
    completed = corpus.ingest(
        str(source),
        metadata={
            "title": "Contract Notes",
            "body": "City Council",
            "meeting_date": "2026-06-01",
        },
    )
    assert completed.result is not None
    document_id = str(completed.result["document_id"])

    profile = create_document_profile(
        corpus.paths.database,
        document_id=document_id,
        text_length=len(content),
        extractor="integration-test",
    )
    facts = extract_document_facts(corpus.paths.database, document_id)
    brief = generate_document_brief(corpus.paths.database, document_id)
    results = _keyword_results(corpus.paths.database, "stormwater")
    provenance = load_packet_source_provenance(corpus.paths.database, results)
    packet = format_source_packet(
        query="stormwater",
        results=results,
        source_provenance=provenance,
    )

    assert profile.source_type == "text"
    assert profile.extent_type == "lines"
    assert profile.extent_count == 2
    assert facts.created
    assert all(item.evidence[0].location_type == "text_line" for item in facts.created)
    assert brief.document.source_type == "text"
    assert "line 1" in format_generated_brief(brief)
    assert results[0].citation == "Contract Notes — 2026-06-01 — line 1"
    source_provenance = provenance[document_id]
    assert source_provenance.source_type == "text"
    assert source_provenance.source_kind == "local_path"
    assert source_provenance.submitted_reference == str(source)
    assert source_provenance.artifact_hash == hashlib.sha256(source.read_bytes()).hexdigest()
    assert source_provenance.processing_generation_id == results[0].processing_generation_id
    assert "source type: text" in packet
    assert "line 1" in packet
    assert "artifact SHA-256:" in packet


@dataclass
class ExplodingAcquirer:
    calls: int = 0

    def acquire(self, request: AcquisitionRequest, staging_dir: Path) -> StagedSourceArtifact:
        del request, staging_dir
        self.calls += 1
        raise AssertionError("reprocessing must not reacquire unavailable live sources")


def test_reprocessing_uses_pinned_non_utf8_media_type_without_live_source(
    tmp_path: Path,
) -> None:
    content = "Council’s café approved a $500,000 contract.\n".encode("cp1252")
    response = HttpResponse(
        200,
        {
            "content-type": "text/plain; charset=windows-1252",
            "content-length": str(len(content)),
        },
        (content,),
    )
    corpus = _corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=_public_resolver,
            transport=HttpTransport([response]),
        ),
    )
    completed = corpus.ingest("https://example.gov/non-utf8")
    assert completed.result is not None
    document_id = str(completed.result["document_id"])
    old_generation = corpus.rows(
        "SELECT current_processing_generation_id FROM documents WHERE id = ?",
        (document_id,),
    )[0][0]
    unavailable = ExplodingAcquirer()
    corpus.ingestion.acquirer = unavailable
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "text-lifecycle", "2")

    reprocessed = corpus.run(enqueue_reprocessing(corpus.paths.database, [document_id])[0])

    assert reprocessed.status == "done", reprocessed.error
    assert reprocessed.result is not None and reprocessed.result["outcome"] == "reprocessed"
    new_generation = reprocessed.result["processing_generation_id"]
    assert new_generation != old_generation
    assert unavailable.calls == 0
    assert corpus.rows(
        "SELECT normalized_text FROM source_units "
        "WHERE document_id = ? AND processing_generation_id = ? ORDER BY ordinal",
        (document_id, new_generation),
    ) == [("Council’s café approved a $500,000 contract.",)]
    assert corpus.rows("SELECT COUNT(*) FROM processing_generations") == [(2,)]
    assert len(_lance_rows(corpus.paths.lancedb, "chunk_embeddings")) == 2
    options = json.loads(
        corpus.rows("SELECT ingestion_options_json FROM documents WHERE id = ?", (document_id,))[0][
            0
        ]
    )
    assert options["source_media_type"] == "text/plain; charset=windows-1252"
    saved_configuration = get_job(corpus.paths.database, reprocessed.id).payload["configuration"]
    assert saved_configuration["options"]["source_media_type"] == (
        "text/plain; charset=windows-1252"
    )


def test_corrupt_saved_text_bytes_cannot_replace_current_processing_generation(
    tmp_path: Path,
) -> None:
    corpus = _corpus(tmp_path / ".newsrag")
    source = tmp_path / "preserved.txt"
    source.write_text("Preservedevidence remains searchable.\n", encoding="utf-8")
    completed = corpus.ingest(str(source))
    assert completed.result is not None
    document_id = str(completed.result["document_id"])
    old_generation = corpus.rows(
        "SELECT current_processing_generation_id FROM documents WHERE id = ?",
        (document_id,),
    )[0][0]
    vectors_before = _lance_rows(corpus.paths.lancedb, "passage_embeddings")
    stored_path = Path(
        corpus.rows(
            "SELECT stored_path FROM source_artifacts "
            "JOIN documents ON documents.artifact_id = source_artifacts.id "
            "WHERE documents.id = ?",
            (document_id,),
        )[0][0]
    )
    stored_path.write_bytes(b"corrupted bytes")
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "text-lifecycle", "2")

    failed = corpus.run(enqueue_reprocessing(corpus.paths.database, [document_id])[0])

    assert failed.status == "failed"
    assert "artifact_integrity" in str(failed.error)
    assert corpus.rows(
        "SELECT current_processing_generation_id FROM documents WHERE id = ?",
        (document_id,),
    ) == [(old_generation,)]
    assert corpus.rows("SELECT COUNT(*) FROM processing_generations") == [(1,)]
    assert _lance_rows(corpus.paths.lancedb, "passage_embeddings") == vectors_before
    results = _keyword_results(corpus.paths.database, "Preservedevidence")
    assert len(results) == 1 and results[0].document_id == document_id


@dataclass
class FailingPassageIndex:
    delegate: LanceDbPassageVectorStore

    def add_passages(self, passages: Sequence[Any]) -> None:
        self.delegate.add_passages(passages)
        raise RuntimeError("injected passage index failure")

    def delete_passages(self, ids: Sequence[str]) -> None:
        self.delegate.delete_passages(ids)

    def delete_document(self, document_id: str) -> None:
        self.delegate.delete_document(document_id)


def test_refresh_index_failure_keeps_previous_revision_and_real_vectors(
    tmp_path: Path,
) -> None:
    corpus = _corpus(tmp_path / ".newsrag")
    source = tmp_path / "safe-refresh.txt"
    source.write_text("Originalevidence approved for parks.\n", encoding="utf-8")
    completed = corpus.ingest(str(source))
    assert completed.result is not None
    source_id = str(completed.result["source_id"])
    document_id = str(completed.result["document_id"])
    vectors_before = _lance_rows(corpus.paths.lancedb, "passage_embeddings")
    corpus.ingestion.processor.passage_vector_store = FailingPassageIndex(
        LanceDbPassageVectorStore(corpus.paths.lancedb)
    )
    source.write_text("Unpublishedcandidate approved for roads.\n", encoding="utf-8")

    failed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))

    assert failed.status == "failed"
    assert "injected passage index failure" in str(failed.error)
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(1,)]
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(1,)]
    assert corpus.rows(
        "SELECT source_revisions.document_id FROM sources "
        "JOIN source_revisions ON source_revisions.id = sources.current_revision_id"
    ) == [(document_id,)]
    assert _lance_rows(corpus.paths.lancedb, "passage_embeddings") == vectors_before
    assert len(_keyword_results(corpus.paths.database, "Originalevidence")) == 1
    assert _keyword_results(corpus.paths.database, "Unpublishedcandidate") == []


def test_reported_text_media_type_applies_ten_mib_url_acquisition_cap(
    tmp_path: Path,
) -> None:
    response = HttpResponse(
        status_code=200,
        headers={
            "content-type": "text/plain",
            "content-length": str(TEXT_MAX_SOURCE_BYTES + 1),
        },
        chunks=(),
    )
    corpus = _corpus(
        tmp_path / ".newsrag",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=_public_resolver,
            transport=HttpTransport([response]),
        ),
    )

    failed = corpus.ingest("https://example.gov/oversized")

    assert TEXT_MAX_SOURCE_BYTES == 10 * 1024 * 1024
    assert MAX_TEXT_CHARS == 10 * 1024 * 1024
    assert MAX_TEXT_LINES == 100_000
    assert failed.status == "failed"
    assert f"exceeds {TEXT_MAX_SOURCE_BYTES} compressed bytes" in str(failed.error)
    assert corpus.rows("SELECT COUNT(*) FROM sources") == [(0,)]
    assert response.closed


@pytest.mark.parametrize(
    "limit_name,content,error",
    [
        ("bytes", b"123456789", "exceeds 8 bytes"),
        ("characters", b"123456789", "8-character limit"),
        ("lines", b"one\ntwo\nthree\nfour", "3-line limit"),
    ],
)
def test_pipeline_byte_character_and_line_limit_failures_are_atomic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    limit_name: str,
    content: bytes,
    error: str,
) -> None:
    if limit_name == "bytes":
        monkeypatch.setattr("newsrag.ingest.TEXT_MAX_SOURCE_BYTES", 8)
    elif limit_name == "characters":
        monkeypatch.setattr("newsrag.text_adapter.MAX_TEXT_CHARS", 8)
    else:
        monkeypatch.setattr("newsrag.text_adapter.MAX_TEXT_LINES", 3)
    corpus = _corpus(tmp_path / ".newsrag")
    source = tmp_path / "bounded.txt"
    source.write_bytes(content)

    failed = corpus.ingest(str(source))

    assert failed.status == "failed"
    assert error in str(failed.error)
    assert corpus.rows("SELECT COUNT(*) FROM documents") == [(0,)]
    assert _lance_rows(corpus.paths.lancedb, "chunk_embeddings") == []
    assert _lance_rows(corpus.paths.lancedb, "passage_embeddings") == []
    expected_acquired = 0 if limit_name == "bytes" else 1
    assert corpus.rows("SELECT COUNT(*) FROM source_artifacts") == [(expected_acquired,)]


def test_utf16_bom_flows_through_pipeline(tmp_path: Path) -> None:
    corpus = _corpus(tmp_path / ".newsrag")
    utf16 = tmp_path / "utf16.txt"
    utf16.write_bytes(codecs.BOM_UTF16_LE + "Council approved UTF16.\n".encode("utf-16-le"))

    completed = corpus.ingest(str(utf16))

    assert completed.status == "done", completed.error
    assert list_documents(corpus.paths.database)[0].metadata["text_encoding"] == "utf-16-le"
    assert corpus.rows("SELECT normalized_text FROM source_units") == [("Council approved UTF16.",)]
