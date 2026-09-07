from __future__ import annotations

import asyncio
import sqlite3
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, cast

import lancedb  # type: ignore[import-untyped]
import pytest

from newsrag.acquisition import AcquisitionRequest, StagedSourceArtifact
from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterResult,
)
from newsrag.config import EmbeddingConfig
from newsrag.daemon import DaemonRunner
from newsrag.embeddings import ChunkEmbedding, EmbeddingMetadata, QueryEmbedding
from newsrag.ingest import (
    INGEST_JOB_KIND,
    IngestionPipeline,
    LanceDbVectorStore,
    SourceUnitChunker,
    enqueue_ingest_source,
    list_chunk_vectors,
)
from newsrag.jobs import (
    Job,
    JobRetryError,
    claim_next_job,
    get_job,
    mark_job_failed,
    retry_failed_job,
)
from newsrag.packets import load_packet_source_provenance
from newsrag.refresh import REFRESH_JOB_KIND, RefreshPipeline, enqueue_refresh
from newsrag.reprocess import (
    REPROCESS_JOB_KIND,
    ReprocessingError,
    ReprocessingPipeline,
    enqueue_reprocessing,
)
from newsrag.search import LanceDbPassageVectorSearcher, LanceDbPassageVectorStore, SearchEngine
from newsrag.source_locations import resolve_source_range
from newsrag.storage import StoragePaths, initialize_storage


@dataclass
class Embeddings:
    metadata: EmbeddingMetadata = EmbeddingMetadata("fake", "model", "1")
    calls: int = 0
    fail: bool = False

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("injected embedding failure")
        return [
            ChunkEmbedding(text, (float(index + 1), float(index + 2)), self.metadata)
            for index, text in enumerate(texts)
        ]

    def embed_query(self, text: str) -> QueryEmbedding:
        return QueryEmbedding(text, (1.0, 2.0), self.metadata)


@dataclass(frozen=True)
class CopyOcr:
    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(source_path.read_bytes())


@dataclass
class BytesExtractor:
    extractor_name: str
    calls: list[Path] = field(default_factory=list)

    def extract_pages(self, pdf_path: Path) -> list[Any]:
        from newsrag.pdf_adapter import ExtractedPage

        self.calls.append(pdf_path)
        text = pdf_path.read_bytes().decode(errors="replace").split("\n", 1)[-1]
        return [ExtractedPage(1, text, self.extractor_name)]


@dataclass
class ExplodingAcquirer:
    calls: int = 0

    def acquire(self, request: AcquisitionRequest, staging_dir: Path) -> StagedSourceArtifact:
        del request, staging_dir
        self.calls += 1
        raise AssertionError("reprocessing must not reacquire the source")


@dataclass
class Corpus:
    paths: StoragePaths
    source_path: Path
    ingestion: IngestionPipeline
    reprocessing: ReprocessingPipeline
    runner: DaemonRunner
    embeddings: Embeddings
    document_id: str
    source_id: str
    artifact_id: str
    revision_id: str

    def run(self, job: Job) -> Job:
        assert asyncio.run(self.runner.run_cycle())
        return get_job(self.paths.database, job.id)

    def enqueue(self, *, pdf_extractor: str | None = None) -> Job:
        return enqueue_reprocessing(
            self.paths.database,
            [self.document_id],
            pdf_extractor=pdf_extractor,
        )[0]

    def rebuild(self, *, pdf_extractor: str | None = None) -> Job:
        return self.run(self.enqueue(pdf_extractor=pdf_extractor))

    def rows(self, sql: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        with sqlite3.connect(self.paths.database) as connection:
            return connection.execute(sql, parameters).fetchall()


def _all_lance_rows(path: Path, base_name: str) -> list[dict[str, Any]]:
    database = lancedb.connect(path)
    response = database.list_tables()
    names = sorted(
        name
        for name in response.tables
        if name == base_name or name.startswith(f"{base_name}__dim_")
    )
    return [row for name in names for row in database.open_table(name).to_arrow().to_pylist()]


@pytest.mark.parametrize("collision", ["chunk", "passage"])
def test_id_collision_never_compensates_retained_vectors(
    corpus: Corpus,
    monkeypatch: pytest.MonkeyPatch,
    collision: str,
) -> None:
    import newsrag.ingest as ingest_module

    before_generation = _generation(corpus)
    before_chunks = _all_lance_rows(corpus.paths.lancedb, "chunk_embeddings")
    before_passages = _all_lance_rows(corpus.paths.lancedb, "passage_embeddings")
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")
    if collision == "chunk":
        original_chunks = ingest_module._build_chunk_and_vector_rows
        existing_id = corpus.rows("SELECT id FROM chunks LIMIT 1")[0][0]

        def colliding_chunks(*args: Any, **kwargs: Any) -> Any:
            rows, vectors = original_chunks(*args, **kwargs)
            rows[0] = (existing_id, *rows[0][1:])
            vectors[0] = replace(vectors[0], chunk_id=existing_id)
            return rows, vectors

        monkeypatch.setattr(ingest_module, "_build_chunk_and_vector_rows", colliding_chunks)
    else:
        original_passages = ingest_module._build_passage_rows_from_chunks
        existing_id = corpus.rows("SELECT id FROM passages LIMIT 1")[0][0]

        def colliding_passages(*args: Any, **kwargs: Any) -> Any:
            rows = original_passages(*args, **kwargs)
            rows[0] = (existing_id, *rows[0][1:])
            return rows

        monkeypatch.setattr(ingest_module, "_build_passage_rows_from_chunks", colliding_passages)
    failed = corpus.rebuild()
    assert failed.status == "failed"
    assert "UNIQUE constraint failed" in str(failed.error)
    assert _generation(corpus) == before_generation
    assert _all_lance_rows(corpus.paths.lancedb, "chunk_embeddings") == before_chunks
    assert _all_lance_rows(corpus.paths.lancedb, "passage_embeddings") == before_passages


def _generation(corpus: Corpus) -> str:
    return str(
        corpus.rows(
            "SELECT current_processing_generation_id FROM documents WHERE id = ?",
            (corpus.document_id,),
        )[0][0]
    )


def _identity(corpus: Corpus) -> tuple[str, str, str, str]:
    return (
        corpus.document_id,
        corpus.source_id,
        corpus.artifact_id,
        corpus.revision_id,
    )


@pytest.fixture
def corpus(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Corpus:
    extractors: list[BytesExtractor] = []

    def extractor_for(mode: str) -> BytesExtractor:
        extractor = BytesExtractor(mode)
        extractors.append(extractor)
        return extractor

    monkeypatch.setattr("newsrag.pdf_adapter.build_pdf_text_extractor", extractor_for)
    paths = initialize_storage(tmp_path / "corpus")
    source_path = tmp_path / "packet.pdf"
    source_path.write_bytes(b"%PDF-1.4\noriginal preserved evidence")
    embeddings = Embeddings()
    ingestion = IngestionPipeline(
        storage_paths=paths,
        embedding_config=EmbeddingConfig(),
        ocr_runner=CopyOcr(),
        embedding_provider=embeddings,
        vector_store=LanceDbVectorStore(paths.lancedb),
        passage_vector_store=LanceDbPassageVectorStore(paths.lancedb),
    )
    reprocessing = ReprocessingPipeline(ingestion)
    runner = DaemonRunner(
        database_path=paths.database,
        handlers={
            INGEST_JOB_KIND: ingestion.handle_job,
            REPROCESS_JOB_KIND: reprocessing.handle_job,
        },
        poll_interval=0,
    )
    ingest_job = enqueue_ingest_source(paths.database, source=str(source_path)).jobs[0]
    assert asyncio.run(runner.run_cycle())
    result = get_job(paths.database, ingest_job.id).result
    assert result is not None
    document_id = str(result["document_id"])
    with sqlite3.connect(paths.database) as connection:
        source_id, artifact_id, revision_id = connection.execute(
            """
            SELECT source_artifacts.source_id, documents.artifact_id, source_revisions.id
            FROM documents
            JOIN source_artifacts ON source_artifacts.id = documents.artifact_id
            JOIN source_revisions ON source_revisions.document_id = documents.id
            WHERE documents.id = ?
            """,
            (document_id,),
        ).fetchone()
    return Corpus(
        paths,
        source_path,
        ingestion,
        reprocessing,
        runner,
        embeddings,
        document_id,
        str(source_id),
        str(artifact_id),
        str(revision_id),
    )


def test_matching_fingerprint_is_a_durable_noop(corpus: Corpus) -> None:
    generation = _generation(corpus)
    derived_before = corpus.rows(
        "SELECT id, processing_generation_id FROM source_units UNION ALL "
        "SELECT id, processing_generation_id FROM chunks UNION ALL "
        "SELECT id, processing_generation_id FROM passages ORDER BY id"
    )
    vectors_before = list_chunk_vectors(corpus.paths.lancedb)
    calls_before = corpus.embeddings.calls

    completed = corpus.rebuild()

    assert completed.status == "done", completed.error
    assert completed.result is not None
    assert completed.result["outcome"] == "unchanged"
    assert completed.result["processing_generation_id"] == generation
    assert _identity(corpus) == (
        str(completed.result["document_id"]),
        str(completed.result["source_id"]),
        str(completed.result["artifact_id"]),
        str(completed.result["revision_id"]),
    )
    assert _generation(corpus) == generation
    assert corpus.embeddings.calls == calls_before
    assert list_chunk_vectors(corpus.paths.lancedb) == vectors_before
    assert (
        corpus.rows(
            "SELECT id, processing_generation_id FROM source_units UNION ALL "
            "SELECT id, processing_generation_id FROM chunks UNION ALL "
            "SELECT id, processing_generation_id FROM passages ORDER BY id"
        )
        == derived_before
    )


@pytest.mark.parametrize("change", ["chunker", "provider", "model", "version", "index_version"])
def test_configuration_changes_rebuild_and_preserve_all_history(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    old_generation = _generation(corpus)
    old_units = set(corpus.rows("SELECT id FROM source_units"))
    old_chunks = set(corpus.rows("SELECT id FROM chunks"))
    old_fts = set(corpus.rows("SELECT chunk_id FROM chunks_fts"))
    old_passage_fts = set(corpus.rows("SELECT passage_id FROM passages_fts"))
    old_chunk_vectors = {str(row["chunk_id"]) for row in list_chunk_vectors(corpus.paths.lancedb)}
    old_passage_vectors = {
        str(row["passage_id"])
        for row in _all_lance_rows(corpus.paths.lancedb, "passage_embeddings")
    }
    if change == "chunker":
        corpus.ingestion.processor.chunker = SourceUnitChunker(max_chars=73)
    elif change == "provider":
        corpus.embeddings.metadata = EmbeddingMetadata("other-provider", "model", "1")
    elif change == "model":
        corpus.embeddings.metadata = EmbeddingMetadata("fake", "other-model", "1")
    elif change == "version":
        corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")
    else:
        monkeypatch.setattr("newsrag.processing_configuration.INDEX_VERSION", "2")

    completed = corpus.rebuild()

    assert completed.status == "done", completed.error
    assert completed.result is not None and completed.result["outcome"] == "reprocessed"
    assert _identity(corpus) == (
        str(completed.result["document_id"]),
        str(completed.result["source_id"]),
        str(completed.result["artifact_id"]),
        str(completed.result["revision_id"]),
    )
    new_generation = _generation(corpus)
    assert new_generation != old_generation
    assert completed.result["previous_processing_generation_id"] == old_generation
    assert completed.result["processing_generation_id"] == new_generation
    assert len(corpus.rows("SELECT id FROM processing_generations")) == 2
    assert old_units < set(corpus.rows("SELECT id FROM source_units"))
    assert old_chunks < set(corpus.rows("SELECT id FROM chunks"))
    assert old_fts < set(corpus.rows("SELECT chunk_id FROM chunks_fts"))
    assert old_passage_fts < set(corpus.rows("SELECT passage_id FROM passages_fts"))
    assert old_chunk_vectors < {
        str(row["chunk_id"]) for row in list_chunk_vectors(corpus.paths.lancedb)
    }
    assert old_passage_vectors < {
        str(row["passage_id"])
        for row in _all_lance_rows(corpus.paths.lancedb, "passage_embeddings")
    }
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(1,)]


@pytest.mark.parametrize("source_change", ["changed", "missing"])
def test_rebuild_uses_preserved_artifact_without_reacquisition(
    corpus: Corpus, source_change: str
) -> None:
    acquirer = ExplodingAcquirer()
    corpus.ingestion.acquirer = acquirer
    if source_change == "changed":
        corpus.source_path.write_bytes(b"%PDF-1.4\nreplacement live bytes")
    else:
        corpus.source_path.unlink()
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")

    completed = corpus.rebuild()

    assert completed.status == "done", completed.error
    assert acquirer.calls == 0
    current = _generation(corpus)
    current_text = corpus.rows(
        "SELECT text FROM chunks WHERE processing_generation_id = ?", (current,)
    )
    assert current_text == [("original preserved evidence",)]


def test_saved_artifact_integrity_failure_never_changes_current_generation(
    corpus: Corpus,
) -> None:
    generation = _generation(corpus)
    stored_path = Path(
        corpus.rows("SELECT stored_path FROM source_artifacts WHERE id = ?", (corpus.artifact_id,))[
            0
        ][0]
    )
    stored_path.write_bytes(b"corrupted saved bytes")
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")

    failed = corpus.rebuild()

    assert failed.status == "failed"
    assert "artifact_integrity" in str(failed.error)
    assert "hash/size mismatch" in str(failed.error)
    assert _generation(corpus) == generation
    assert len(corpus.rows("SELECT id FROM processing_generations")) == 1


def test_enqueue_validation_and_batches_are_all_or_nothing(corpus: Corpus) -> None:
    before = corpus.rows("SELECT id FROM jobs WHERE kind = 'reprocess-document'")
    invalid_requests: list[Sequence[str]] = [
        [],
        [corpus.document_id] * 21,
        [""],
        ["   "],
        ["missing"],
        corpus.document_id,
    ]
    for request in invalid_requests:
        with pytest.raises(ReprocessingError):
            enqueue_reprocessing(corpus.paths.database, request)
        assert corpus.rows("SELECT id FROM jobs WHERE kind = 'reprocess-document'") == before

    with pytest.raises(ReprocessingError, match="Unknown"):
        enqueue_reprocessing(corpus.paths.database, [corpus.document_id, "missing"])
    assert corpus.rows("SELECT id FROM jobs WHERE kind = 'reprocess-document'") == before
    with pytest.raises(ReprocessingError, match="Unknown PDF extractor"):
        enqueue_reprocessing(corpus.paths.database, [corpus.document_id], pdf_extractor="bad")
    assert corpus.rows("SELECT id FROM jobs WHERE kind = 'reprocess-document'") == before


def test_duplicate_ids_and_concurrent_enqueue_converge_on_one_active_job(
    corpus: Corpus,
) -> None:
    duplicate = enqueue_reprocessing(
        corpus.paths.database, [corpus.document_id, corpus.document_id]
    )
    assert len(duplicate) == 1
    first = duplicate[0]

    def enqueue_one(_: int) -> str:
        return enqueue_reprocessing(corpus.paths.database, [corpus.document_id])[0].id

    with ThreadPoolExecutor(max_workers=4) as executor:
        ids = list(executor.map(enqueue_one, range(8)))

    assert set(ids) == {first.id}
    assert corpus.rows(
        "SELECT COUNT(*) FROM jobs WHERE kind = 'reprocess-document' "
        "AND status IN ('pending', 'running')"
    ) == [(1,)]


def test_concurrent_enqueue_and_retry_leave_exactly_one_active_job(corpus: Corpus) -> None:
    failed = corpus.enqueue()
    mark_job_failed(corpus.paths.database, failed.id, error="injected")

    def enqueue() -> tuple[str, str]:
        try:
            return "ok", enqueue_reprocessing(corpus.paths.database, [corpus.document_id])[0].id
        except Exception as exc:
            return "error", str(exc)

    def retry() -> tuple[str, str]:
        try:
            return "ok", retry_failed_job(corpus.paths.database, failed.id).id
        except JobRetryError as exc:
            return "error", str(exc)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda function: function(), (enqueue, retry)))

    active = corpus.rows(
        "SELECT id FROM jobs WHERE kind = 'reprocess-document' AND status IN ('pending', 'running')"
    )
    assert len(active) == 1
    assert any(status == "ok" for status, _ in results)
    assert all(
        status == "ok" or "already pending or running" in detail for status, detail in results
    )


def test_active_job_with_different_options_is_rejected(corpus: Corpus) -> None:
    pending = corpus.enqueue()
    assert corpus.enqueue().id == pending.id
    with pytest.raises(ReprocessingError, match="different options"):
        enqueue_reprocessing(corpus.paths.database, [corpus.document_id], pdf_extractor="pymupdf")


def test_mixed_pdf_html_batch_rebuilds_both_documents(corpus: Corpus, tmp_path: Path) -> None:
    html_path = tmp_path / "notice.html"
    html_path.write_text(
        "<!doctype html><html><body><p>zoning hearing evidence</p></body></html>",
        encoding="utf-8",
    )
    html_ingest = enqueue_ingest_source(corpus.paths.database, source=str(html_path)).jobs[0]
    assert corpus.run(html_ingest).status == "done"
    html_document_id = str(get_job(corpus.paths.database, html_ingest.id).result["document_id"])  # type: ignore[index]
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")

    jobs = enqueue_reprocessing(corpus.paths.database, [corpus.document_id, html_document_id])
    for _ in jobs:
        assert asyncio.run(corpus.runner.run_cycle())
    completed = [get_job(corpus.paths.database, job.id) for job in jobs]

    assert {job.result["document_id"] for job in completed if job.result is not None} == {
        corpus.document_id,
        html_document_id,
    }
    assert all(
        job.result is not None and job.result["outcome"] == "reprocessed" for job in completed
    )


def test_pdf_extractor_override_is_saved_and_used(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch
) -> None:
    requested_modes: list[str] = []

    def extractor_for(mode: str) -> BytesExtractor:
        requested_modes.append(mode)
        return BytesExtractor(mode)

    monkeypatch.setattr("newsrag.pdf_adapter.build_pdf_text_extractor", extractor_for)
    completed = corpus.rebuild(pdf_extractor="table")

    assert completed.status == "done", completed.error
    assert completed.result is not None and completed.result["outcome"] == "reprocessed"
    assert requested_modes == ["table"]
    generation = _generation(corpus)
    assert corpus.rows(
        "SELECT extractor FROM pages WHERE processing_generation_id = ?", (generation,)
    ) == [("table",)]
    configuration = get_job(corpus.paths.database, completed.id).payload["configuration"]
    assert configuration["options"]["pdf_extractor"] == "table"


class FailingChunkVectorStore:
    def __init__(self, delegate: LanceDbVectorStore) -> None:
        self.delegate = delegate

    def add_chunks(self, chunks: Sequence[Any]) -> None:
        self.delegate.add_chunks(chunks)
        raise RuntimeError("injected vector publication failure")

    def delete_chunks(self, chunk_ids: Sequence[str]) -> None:
        self.delegate.delete_chunks(chunk_ids)

    def delete_document(self, document_id: str) -> None:
        self.delegate.delete_document(document_id)


@pytest.mark.parametrize("failure", ["extraction", "chunking", "embedding", "fts", "vector"])
def test_stage_failures_leave_previous_generation_and_vectors_intact(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    old_generation = _generation(corpus)
    old_rows = {
        table: corpus.rows(f"SELECT * FROM {table} ORDER BY id")
        for table in ("source_units", "pages", "chunks", "passages", "processing_generations")
    }
    old_chunk_vectors = list_chunk_vectors(corpus.paths.lancedb)
    old_passage_vectors = _all_lance_rows(corpus.paths.lancedb, "passage_embeddings")
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")

    if failure == "extraction":

        def fail_extract(self: object, artifact: AdapterInput) -> AdapterResult:
            del self, artifact
            raise AdapterError("injected extraction failure")

        selected = corpus.ingestion.adapter_registry.select(
            artifact_path=Path(
                corpus.rows(
                    "SELECT stored_path FROM source_artifacts WHERE id = ?", (corpus.artifact_id,)
                )[0][0]
            ),
            source_type_hint=None,
            reported_media_type="application/pdf",
            filename="",
        )
        monkeypatch.setattr(type(selected.adapter), "extract", fail_extract)
    elif failure == "chunking":

        def fail_chunks(self: object, units: object) -> list[object]:
            del self, units
            raise RuntimeError("injected chunking failure")

        monkeypatch.setattr(type(corpus.ingestion.processor.chunker), "chunk_units", fail_chunks)
    elif failure == "embedding":
        corpus.embeddings.fail = True
    elif failure == "fts":
        connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def executemany(self, sql: str, parameters: Any) -> sqlite3.Cursor:
                if "INSERT INTO passages_fts" in sql:
                    raise sqlite3.OperationalError("injected FTS failure")
                return super().executemany(sql, parameters)

        def failing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            return cast(
                sqlite3.Connection,
                connect(*args, **{**kwargs, "factory": FailingConnection}),
            )

        monkeypatch.setattr("newsrag.ingest.sqlite3.connect", failing_connect)
    else:
        corpus.ingestion.processor.vector_store = FailingChunkVectorStore(
            LanceDbVectorStore(corpus.paths.lancedb)
        )

    failed = corpus.rebuild()

    assert failed.status == "failed"
    expected_stage = {
        "extraction": "adapter_extraction",
        "chunking": "chunking",
        "embedding": "chunk_embeddings",
        "fts": "publication",
        "vector": "publication",
    }[failure]
    assert expected_stage in str(failed.error)
    assert _generation(corpus) == old_generation
    assert {
        table: corpus.rows(f"SELECT * FROM {table} ORDER BY id") for table in old_rows
    } == old_rows
    assert list_chunk_vectors(corpus.paths.lancedb) == old_chunk_vectors
    assert _all_lance_rows(corpus.paths.lancedb, "passage_embeddings") == old_passage_vectors


def test_retry_uses_exact_saved_artifact_generation_and_configuration(corpus: Corpus) -> None:
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")
    corpus.embeddings.fail = True
    failed = corpus.rebuild()
    assert failed.status == "failed"
    saved = failed.payload.copy()
    corpus.embeddings.fail = False
    corpus.source_path.unlink()

    completed = corpus.run(retry_failed_job(corpus.paths.database, failed.id))

    assert completed.status == "done", completed.error
    assert completed.result is not None and completed.result["outcome"] == "reprocessed"
    assert completed.payload["artifact_id"] == saved["artifact_id"]
    assert completed.payload["generation_id"] == saved["generation_id"]
    assert completed.payload["configuration"] == saved["configuration"]
    assert completed.payload["fingerprint"] == saved["fingerprint"]


def test_retry_rejects_processing_configuration_drift(corpus: Corpus) -> None:
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")
    corpus.embeddings.fail = True
    failed = corpus.rebuild()
    assert failed.status == "failed"
    corpus.embeddings.fail = False
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "3")

    rejected = corpus.run(retry_failed_job(corpus.paths.database, failed.id))

    assert rejected.status == "failed"
    assert "reprocess_configuration_conflict" in str(rejected.error)


def test_retry_rejects_stale_base_after_another_generation_commits(corpus: Corpus) -> None:
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")
    corpus.embeddings.fail = True
    stale = corpus.rebuild()
    assert stale.status == "failed"
    corpus.embeddings.fail = False
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "3")
    newer = corpus.rebuild()
    assert newer.status == "done", newer.error
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")

    rejected = corpus.run(retry_failed_job(corpus.paths.database, stale.id))

    assert rejected.status == "failed"
    assert "reprocess_conflict: generation changed" in str(rejected.error)
    assert _generation(corpus) == newer.result["processing_generation_id"]  # type: ignore[index]


def test_pdf_normalized_outputs_are_isolated_by_generation(corpus: Corpus) -> None:
    old_generation = _generation(corpus)
    old_path = Path(
        corpus.rows(
            "SELECT normalized_path FROM processing_generations WHERE id = ?", (old_generation,)
        )[0][0]
    )
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")

    completed = corpus.rebuild()

    assert completed.status == "done", completed.error
    new_generation = _generation(corpus)
    new_path = Path(
        corpus.rows(
            "SELECT normalized_path FROM processing_generations WHERE id = ?", (new_generation,)
        )[0][0]
    )
    assert old_path != new_path
    assert old_path.parent == corpus.paths.derived_artifacts / old_generation
    assert new_path.parent == corpus.paths.derived_artifacts / new_generation
    assert not (corpus.paths.data_dir / "ocr-pdfs").exists()
    assert old_path.read_bytes() == new_path.read_bytes()


def test_legacy_transition_preserves_pdf_citations_and_saved_artifact_reprocessing(
    corpus: Corpus,
) -> None:
    engine = SearchEngine(
        database_path=corpus.paths.database,
        vector_searcher=LanceDbPassageVectorSearcher(corpus.paths.lancedb),
        vector_store=LanceDbPassageVectorStore(corpus.paths.lancedb),
        embedding_provider=corpus.embeddings,
    )
    historical = engine.search("preserved evidence")
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")
    assert corpus.rebuild().status == "done"
    current = engine.search("preserved evidence")
    assert historical and current
    assert historical[0].processing_generation_id != current[0].processing_generation_id
    # Represent a pre-upgrade corpus with real published PDF evidence and old packet paths.
    legacy_paths = []
    with sqlite3.connect(corpus.paths.database) as connection:
        for generation, reference in connection.execute(
            "SELECT id, normalized_path FROM processing_generations"
        ).fetchall():
            path = Path(reference)
            legacy = corpus.paths.data_dir / "ocr-pdfs" / generation / path.name
            legacy.parent.mkdir(parents=True)
            path.rename(legacy)
            legacy_paths.append(legacy)
            connection.execute(
                "UPDATE processing_generations SET normalized_path = ? WHERE id = ?",
                (str(legacy), generation),
            )
            connection.execute(
                "UPDATE documents SET normalized_path = ? WHERE normalized_path = ?",
                (str(legacy), reference),
            )
    tables = (
        "sources",
        "source_artifacts",
        "source_revisions",
        "source_units",
        "pages",
        "chunks",
        "passages",
        "chunks_fts",
        "passages_fts",
        "embedding_records",
    )
    before = {table: corpus.rows(f"SELECT * FROM {table}") for table in tables}
    original = Path(corpus.rows("SELECT stored_path FROM source_artifacts")[0][0])
    original_bytes = original.read_bytes()
    old_provenance = load_packet_source_provenance(corpus.paths.database, historical)[
        corpus.document_id
    ]

    initialize_storage(corpus.paths.data_dir)

    assert before == {table: corpus.rows(f"SELECT * FROM {table}") for table in tables}
    assert engine.search("preserved evidence") == current
    for results in (historical, current):
        provenance = load_packet_source_provenance(corpus.paths.database, results)[
            corpus.document_id
        ]
        assert provenance.processing_generation_id == results[0].processing_generation_id
        assert provenance.normalized_path is not None
        assert Path(provenance.normalized_path).parent == corpus.paths.derived_artifacts / str(
            provenance.processing_generation_id
        )
        assert Path(provenance.normalized_path).read_bytes() == original_bytes
        with sqlite3.connect(corpus.paths.database) as connection:
            resolved = resolve_source_range(
                connection,
                document_id=corpus.document_id,
                source_unit_start_id=str(results[0].source_unit_start_id),
                source_unit_end_id=str(results[0].source_unit_end_id),
                processing_generation_id=results[0].processing_generation_id,
            )
        assert results[0].citation == f"{results[0].title} — {resolved.location_label}"
        assert resolved.text == results[0].text
    assert old_provenance.normalized_path is not None
    assert Path(old_provenance.normalized_path).read_bytes() == original_bytes
    assert all(path.read_bytes() == original_bytes for path in legacy_paths)
    assert original.parent == corpus.paths.source_artifacts
    assert original.read_bytes() == original_bytes
    corpus.source_path.unlink()
    corpus.ingestion.acquirer = ExplodingAcquirer()
    assert corpus.rebuild(pdf_extractor="pdfplumber").status == "done"
    assert original.read_bytes() == original_bytes


def test_pdf_refresh_writes_shared_generation_output_and_keeps_original(corpus: Corpus) -> None:
    original = Path(corpus.rows("SELECT stored_path FROM source_artifacts")[0][0])
    original_bytes = original.read_bytes()
    old_generation = _generation(corpus)
    corpus.source_path.write_bytes(b"%PDF-1.4\nrefreshed PDF evidence")
    corpus.runner.handlers[REFRESH_JOB_KIND] = RefreshPipeline(corpus.ingestion).handle_job
    completed = corpus.run(enqueue_refresh(corpus.paths.database, corpus.source_id))
    assert completed.status == "done", completed.error
    outputs = corpus.rows("SELECT id, normalized_path FROM processing_generations")
    assert len(outputs) == 2
    for generation, reference in outputs:
        output = Path(reference)
        assert output.parent == corpus.paths.derived_artifacts / generation
        expected = (
            original_bytes if generation == old_generation else corpus.source_path.read_bytes()
        )
        assert output.read_bytes() == expected
    assert original.read_bytes() == original_bytes
    assert not (corpus.paths.data_dir / "ocr-pdfs").exists()
    assert all(
        Path(row[0]).parent == corpus.paths.source_artifacts
        for row in corpus.rows("SELECT stored_path FROM source_artifacts")
    )


def test_committed_receipt_survives_late_failure_acknowledgement_and_replay(
    corpus: Corpus,
) -> None:
    corpus.embeddings.metadata = EmbeddingMetadata("fake", "model", "2")
    completed = corpus.rebuild()
    assert completed.status == "done", completed.error
    generations = corpus.rows("SELECT id FROM processing_generations ORDER BY id")

    after = mark_job_failed(
        corpus.paths.database, completed.id, error="late worker failure acknowledgement"
    )
    replayed = corpus.reprocessing.process_job(completed)

    assert after.status == "done"
    assert after.result == completed.result
    assert replayed == completed.result
    assert corpus.rows("SELECT id FROM processing_generations ORDER BY id") == generations


def test_interrupted_reprocessing_is_recovered_and_retryable(corpus: Corpus) -> None:
    job = corpus.enqueue()
    assert claim_next_job(corpus.paths.database) is not None

    assert not asyncio.run(corpus.runner.run_cycle())
    interrupted = get_job(corpus.paths.database, job.id)
    assert interrupted.status == "failed"
    assert "reprocess_interrupted" in str(interrupted.error)
    assert corpus.run(retry_failed_job(corpus.paths.database, job.id)).status == "done"


@pytest.mark.parametrize("cancel_first", [False, True])
def test_live_worker_lock_prevents_recovery_takeover_and_survives_cancellation(
    corpus: Corpus, cancel_first: bool
) -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def paused(job: Job) -> dict[str, object]:
            started.set()
            await release.wait()
            return await corpus.reprocessing.handle_job(job)

        first = DaemonRunner(
            database_path=corpus.paths.database,
            handlers={REPROCESS_JOB_KIND: paused},
            poll_interval=0,
        )
        second = DaemonRunner(
            database_path=corpus.paths.database,
            handlers={REPROCESS_JOB_KIND: paused},
            poll_interval=0,
        )
        job = corpus.enqueue()
        work = asyncio.create_task(first.run_cycle())
        await started.wait()
        if cancel_first:
            work.cancel()
            await asyncio.sleep(0)
        assert not await second.run_cycle()
        assert get_job(corpus.paths.database, job.id).status == "running"
        release.set()
        if cancel_first:
            with pytest.raises(asyncio.CancelledError):
                await work
        else:
            assert await work
        assert get_job(corpus.paths.database, job.id).status == "done"

    asyncio.run(exercise())
