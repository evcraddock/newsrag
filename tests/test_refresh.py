from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from typer.testing import CliRunner

from newsrag.acquisition import AcquisitionError, AcquisitionRequest, StagedSourceArtifact
from newsrag.adapters import AdapterInput, AdapterResult, CanonicalSourceUnit, ExtractorIdentity
from newsrag.cli import app
from newsrag.config import EmbeddingConfig
from newsrag.daemon import DaemonRunner
from newsrag.embeddings import ChunkEmbedding, EmbeddingMetadata, QueryEmbedding
from newsrag.ingest import (
    INGEST_JOB_KIND,
    ChunkVectorRecord,
    IngestionPipeline,
    enqueue_ingest_source,
)
from newsrag.jobs import (
    Job,
    JobRetryError,
    claim_next_job,
    get_job,
    mark_job_failed,
    retry_failed_job,
)
from newsrag.refresh import REFRESH_JOB_KIND, RefreshError, RefreshPipeline, enqueue_refresh
from newsrag.revisions import get_current_revision
from newsrag.search import PassageVectorRecord
from newsrag.storage import StoragePaths, initialize_storage


@dataclass
class Embeddings:
    metadata: EmbeddingMetadata = EmbeddingMetadata("openai_compatible", "fake", "1")
    calls: int = 0
    fail: bool = False

    def embed_chunks(self, texts: Sequence[str]) -> list[ChunkEmbedding]:
        self.calls += 1
        if self.fail:
            raise RuntimeError("embedding service unavailable")
        return [ChunkEmbedding(text, (1.0, 0.0), self.metadata) for text in texts]

    def embed_query(self, text: str) -> QueryEmbedding:
        return QueryEmbedding(text, (1.0, 0.0), self.metadata)


@dataclass
class Vectors:
    documents: set[str] = field(default_factory=set)
    fail: bool = False

    def add_chunks(self, chunks: Sequence[ChunkVectorRecord]) -> None:
        self.documents.update(row.document_id for row in chunks)
        if self.fail:
            raise RuntimeError("vector write unavailable")

    def add_passages(self, passages: Sequence[PassageVectorRecord]) -> None:
        self.documents.update(row.document_id for row in passages)
        if self.fail:
            raise RuntimeError("vector write unavailable")

    def delete_document(self, document_id: str) -> None:
        self.documents.discard(document_id)


class PdfAdapter:
    media_types = ("application/pdf",)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        text = artifact.artifact_path.read_text()
        return AdapterResult(
            media_type="application/pdf",
            units=(
                CanonicalSourceUnit(
                    1, "page", {"page_number": 1}, "p. 1", text, {}, ExtractorIdentity("fake-pdf")
                ),
            ),
            extractor=ExtractorIdentity("fake-pdf"),
        )


@dataclass
class RemoteAcquirer:
    content: bytes
    calls: int = 0
    resolved: str = "https://example.test/notice.html"
    fail: bool = False
    media_type: str = "text/html"

    def acquire(self, request: AcquisitionRequest, staging_dir: Path) -> StagedSourceArtifact:
        self.calls += 1
        if self.fail:
            raise AcquisitionError("remote_status", "HTTP status 404")
        assert request.kind == "url"
        staged = staging_dir / f"remote-{self.calls}"
        staged.write_bytes(self.content)
        return StagedSourceArtifact(
            "url",
            request.reference,
            request.reference,
            self.resolved,
            staged,
            hashlib.sha256(self.content).hexdigest(),
            len(self.content),
            f"2026-09-05T12:00:{self.calls:02d}Z",
            self.media_type,
            {
                "submitted_url": request.reference,
                "resolved_url": self.resolved,
                "retrieved_at": f"2026-09-05T12:00:{self.calls:02d}Z",
            },
        )


def html(text: str, title: str = "Extracted title") -> bytes:
    return f"<!doctype html><html><head><title>{title}</title></head><body><p>{text}</p></body></html>".encode()


@dataclass
class Corpus:
    paths: StoragePaths
    path: Path
    ingestion: IngestionPipeline
    refresh: RefreshPipeline
    runner: DaemonRunner
    embeddings: Embeddings
    vectors: Vectors
    source_id: str
    original_document_id: str

    def run(self, job: Job) -> Job:
        assert asyncio.run(self.runner.run_cycle())
        return get_job(self.paths.database, job.id)

    def update(self, content: bytes) -> Job:
        self.path.write_bytes(content)
        return self.run(enqueue_refresh(self.paths.database, self.source_id))

    def rows(self, sql: str) -> list[tuple[object, ...]]:
        with sqlite3.connect(self.paths.database) as connection:
            return connection.execute(sql).fetchall()


@pytest.fixture
def corpus(tmp_path: Path) -> Corpus:
    paths = initialize_storage(tmp_path / "corpus")
    path = tmp_path / "notice.html"
    path.write_bytes(html("original budget"))
    embeddings = Embeddings()
    vectors = Vectors()
    ingestion = IngestionPipeline(
        storage_paths=paths,
        embedding_config=EmbeddingConfig(),
        embedding_provider=embeddings,
        vector_store=vectors,
        passage_vector_store=vectors,
        adapter=PdfAdapter(),
    )
    refresh = RefreshPipeline(ingestion)
    runner = DaemonRunner(
        database_path=paths.database,
        handlers={
            INGEST_JOB_KIND: ingestion.handle_job,
            REFRESH_JOB_KIND: refresh.handle_job,
        },
    )
    job = enqueue_ingest_source(
        paths.database, source=str(path), metadata={"body": "Council"}
    ).jobs[0]
    assert asyncio.run(runner.run_cycle())
    result = get_job(paths.database, job.id).result
    assert result is not None
    return Corpus(
        paths,
        path,
        ingestion,
        refresh,
        runner,
        embeddings,
        vectors,
        str(result["source_id"]),
        str(result["document_id"]),
    )


def test_refresh_unchanged_is_a_durable_noop(corpus: Corpus) -> None:
    before = corpus.rows("SELECT id, metadata_json FROM documents")
    calls = corpus.embeddings.calls
    job = corpus.run(enqueue_refresh(corpus.paths.database, corpus.source_id))
    assert job.status == "done"
    assert job.result is not None and job.result["outcome"] == "unchanged"
    assert corpus.rows("SELECT id, metadata_json FROM documents") == before
    assert corpus.embeddings.calls == calls


def test_refresh_new_revision_preserves_history_and_reactivates_without_reindexing(
    corpus: Corpus,
) -> None:
    original = corpus.rows("SELECT * FROM documents")
    original_units = corpus.rows("SELECT * FROM source_units")
    changed = corpus.update(html("revised budget", "Revised title"))
    assert changed.status == "done", changed.error
    assert changed.result is not None and changed.result["outcome"] == "revision_created"
    current = get_current_revision(corpus.paths.database, corpus.source_id)
    assert current is not None and current.revision_number == 2
    assert corpus.rows("SELECT * FROM documents ORDER BY created_at, rowid")[0] == original[0]
    assert corpus.rows("SELECT * FROM source_units ORDER BY rowid")[0] == original_units[0]
    assert corpus.rows("SELECT title FROM documents ORDER BY rowid") == [
        ("Extracted title",),
        ("Revised title",),
    ]
    for (metadata,) in corpus.rows("SELECT metadata_json FROM documents"):
        assert json.loads(str(metadata))["body"] == "Council"
    calls = corpus.embeddings.calls
    reverted = corpus.update(html("original budget"))
    assert reverted.result is not None and reverted.result["outcome"] == "revision_reactivated"
    assert reverted.result["document_id"] == corpus.original_document_id
    assert len(corpus.rows("SELECT id FROM source_revisions")) == 2
    assert corpus.rows("SELECT publication_generation FROM sources") == [(3,)]
    assert corpus.embeddings.calls == calls
    corpus.path.write_bytes(html("revised budget", "Revised title"))
    historical_ingest = enqueue_ingest_source(corpus.paths.database, source=str(corpus.path)).jobs[
        0
    ]
    ignored = corpus.run(historical_ingest)
    assert ignored.result is not None and ignored.result["outcome"] == "duplicate_ignored"
    current = get_current_revision(corpus.paths.database, corpus.source_id)
    assert current is not None and current.document_id == corpus.original_document_id


def test_refresh_inherits_explicit_title_but_not_old_derived_metadata(corpus: Corpus) -> None:
    # Existing fixture demonstrates extracted titles update; explicit values win.
    with sqlite3.connect(corpus.paths.database) as connection:
        connection.execute(
            "UPDATE documents SET user_metadata_json = ?",
            (json.dumps({"title": "User title", "body": "Council"}),),
        )
    changed = corpus.update(html("another proposal", "New extracted title"))
    assert changed.status == "done", changed.error
    assert corpus.rows("SELECT title FROM documents ORDER BY rowid")[-1] == ("User title",)


def test_refresh_reuses_staged_bytes_but_reacquires_source(corpus: Corpus) -> None:
    corpus.path.write_bytes(html("staged budget"))
    staged = corpus.run(
        enqueue_ingest_source(corpus.paths.database, source=str(corpus.path)).jobs[0]
    )
    assert (
        staged.result is not None and staged.result["outcome"] == "change_detected_artifact_saved"
    )
    count = len(corpus.rows("SELECT id FROM source_artifacts"))
    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, corpus.source_id))
    assert (
        refreshed.result is not None
        and refreshed.result["artifact_id"] == staged.result["artifact_id"]
    )
    assert len(corpus.rows("SELECT id FROM source_artifacts")) == count
    corpus.path.write_bytes(html("staged but now outdated"))
    corpus.run(enqueue_ingest_source(corpus.paths.database, source=str(corpus.path)).jobs[0])
    fresh = corpus.update(html("newest live budget"))
    assert fresh.status == "done", fresh.error
    assert any("newest live" in str(row[0]) for row in corpus.rows("SELECT text FROM passages"))


@pytest.mark.parametrize("failure", ["embedding", "vectors", "invalid", "missing"])
def test_failed_refresh_preserves_current_and_is_retryable(corpus: Corpus, failure: str) -> None:
    old_vectors = set(corpus.vectors.documents)
    corpus.path.write_bytes(html("new budget"))
    if failure == "embedding":
        corpus.embeddings.fail = True
    elif failure == "vectors":
        corpus.vectors.fail = True
    elif failure == "invalid":
        corpus.path.write_bytes(b"<html><body><script>no evidence</script></body></html>")
    else:
        corpus.path.unlink()
    failed = corpus.run(enqueue_refresh(corpus.paths.database, corpus.source_id))
    assert failed.status == "failed"
    assert failed.error
    current = get_current_revision(corpus.paths.database, corpus.source_id)
    assert current is not None and current.document_id == corpus.original_document_id
    assert len(corpus.rows("SELECT id FROM documents")) == 1
    assert corpus.vectors.documents == old_vectors
    if failure in {"embedding", "vectors"}:
        corpus.embeddings.fail = corpus.vectors.fail = False
        corpus.path.unlink()  # Retry must use the verified checkpoint, not the live source.
        retried = corpus.run(retry_failed_job(corpus.paths.database, failed.id))
        assert retried.status == "done", retried.error
        assert retried.result is not None and retried.result["outcome"] == "revision_created"
    elif failure == "missing":
        corpus.path.write_bytes(html("reappeared"))
        assert corpus.run(retry_failed_job(corpus.paths.database, failed.id)).status == "done"


@pytest.mark.parametrize("failure", ["chunking", "passage_embeddings", "fts", "interrupted"])
def test_processing_stage_failures_never_publish_a_partial_revision(
    corpus: Corpus, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    before = corpus.rows("SELECT id FROM documents")

    class Interrupted(BaseException):
        pass

    if failure == "chunking":

        def fail_chunking(self: object, units: object) -> list[object]:
            raise RuntimeError("injected chunking failure")

        monkeypatch.setattr(
            corpus.ingestion.processor.chunker.__class__, "chunk_units", fail_chunking
        )
    elif failure == "passage_embeddings":
        embed = corpus.embeddings.embed_chunks
        call_count = 0

        def fail_passages(texts: Sequence[str]) -> list[ChunkEmbedding]:
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise RuntimeError("injected passage embedding failure")
            return embed(texts)

        monkeypatch.setattr(corpus.embeddings, "embed_chunks", fail_passages)
    elif failure == "fts":
        # Abort exactly the FTS insert without replacing real SQLite transactions.
        from typing import Any

        connect = sqlite3.connect

        class FailingConnection(sqlite3.Connection):
            def executemany(self, sql: str, parameters: Any) -> sqlite3.Cursor:
                if "INSERT INTO passages_fts" in sql:
                    raise sqlite3.OperationalError("injected FTS failure")
                return super().executemany(sql, parameters)

        def failing_connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
            connection: sqlite3.Connection = connect(
                *args, **{**kwargs, "factory": FailingConnection}
            )
            return connection

        monkeypatch.setattr("newsrag.ingest.sqlite3.connect", failing_connect)
    else:

        def interrupted_vectors(passages: Sequence[PassageVectorRecord]) -> None:
            raise Interrupted("worker terminated before commit")

        monkeypatch.setattr(corpus.vectors, "add_passages", interrupted_vectors)

    if failure == "interrupted":
        with pytest.raises(Interrupted):
            corpus.update(html("uncommitted revision"))
    else:
        failed = corpus.update(html("uncommitted revision"))
        assert failed.status == "failed" and failed.error
        expected_stage = "publication" if failure == "fts" else failure
        assert expected_stage in failed.error
    assert corpus.rows("SELECT id FROM documents") == before
    current = get_current_revision(corpus.paths.database, corpus.source_id)
    assert current is not None and current.document_id == corpus.original_document_id


def test_retry_rejects_stale_generation_and_corrupt_artifact(corpus: Corpus) -> None:
    corpus.embeddings.fail = True
    failed = corpus.update(html("failed candidate"))
    assert failed.status == "failed"
    corpus.embeddings.fail = False
    assert corpus.update(html("fresh successful candidate")).status == "done"
    conflict = corpus.run(retry_failed_job(corpus.paths.database, failed.id))
    assert conflict.status == "failed" and "refresh_conflict" in str(conflict.error)
    corpus.embeddings.fail = True
    failed = corpus.update(html("next failed candidate"))
    candidate_id = failed.payload["candidate"]["artifact_id"]
    with sqlite3.connect(corpus.paths.database) as connection:
        stored = connection.execute(
            "SELECT stored_path FROM source_artifacts WHERE id = ?", (candidate_id,)
        ).fetchone()[0]
    Path(stored).write_bytes(b"corrupt")
    corpus.embeddings.fail = False
    corrupted = corpus.run(retry_failed_job(corpus.paths.database, failed.id))
    assert corrupted.status == "failed" and "artifact_integrity" in str(corrupted.error)


def test_duplicate_refresh_requests_and_retry_are_mutually_exclusive(corpus: Corpus) -> None:
    first = enqueue_refresh(corpus.paths.database, corpus.source_id)
    second = enqueue_refresh(corpus.paths.database, corpus.source_id)
    assert first.id == second.id
    mark_job_failed(corpus.paths.database, first.id, error="injected")
    active = enqueue_refresh(corpus.paths.database, corpus.source_id)
    with pytest.raises(JobRetryError, match="already pending or running"):
        retry_failed_job(corpus.paths.database, first.id)
    assert corpus.run(active).status == "done"


def test_cross_source_duplicates_do_not_link_or_change_requested_source(corpus: Corpus) -> None:
    other = corpus.path.with_name("other.html")
    other.write_bytes(html("other source content"))
    published = corpus.run(enqueue_ingest_source(corpus.paths.database, source=str(other)).jobs[0])
    duplicate = corpus.update(other.read_bytes())
    assert duplicate.result is not None and duplicate.result["outcome"] == "duplicate_ignored"
    assert duplicate.result["document_id"] == published.result["document_id"]  # type: ignore[index]
    assert duplicate.result["requested_source_id"] == corpus.source_id
    assert duplicate.payload == {}
    current = get_current_revision(corpus.paths.database, corpus.source_id)
    assert current is not None and current.document_id == corpus.original_document_id
    assert len(corpus.rows("SELECT id FROM sources")) == 2
    assert len(corpus.rows("SELECT id FROM source_revisions")) == 2


def test_cross_source_unpublished_bytes_fail_explicitly(corpus: Corpus) -> None:
    other = corpus.path.with_name("other.html")
    other.write_bytes(html("unpublished content"))
    corpus.embeddings.fail = True
    failed = corpus.run(enqueue_ingest_source(corpus.paths.database, source=str(other)).jobs[0])
    assert failed.status == "failed"
    corpus.embeddings.fail = False
    conflict = corpus.update(other.read_bytes())
    assert conflict.status == "failed" and "artifact_source_conflict" in str(conflict.error)


def test_unknown_source_is_rejected_before_enqueue(corpus: Corpus) -> None:
    before = corpus.rows("SELECT id FROM jobs")
    with pytest.raises(RefreshError, match="Unknown source"):
        enqueue_refresh(corpus.paths.database, "missing")
    assert corpus.rows("SELECT id FROM jobs") == before


def test_refresh_can_change_supported_format(corpus: Corpus) -> None:
    changed = corpus.update(b"%PDF-1.4\nPDF revision content")
    assert changed.status == "done", changed.error
    assert len(corpus.rows("SELECT id FROM documents")) == 2
    assert corpus.rows("SELECT media_type FROM source_artifacts ORDER BY rowid")[-1] == (
        "application/pdf",
    )


def test_remote_refresh_observes_redirect_without_mutating_old_provenance(corpus: Corpus) -> None:
    remote = RemoteAcquirer(html("remote original"))
    corpus.ingestion.acquirer = remote
    original = corpus.run(
        enqueue_ingest_source(
            corpus.paths.database, source="https://example.test/notice.html"
        ).jobs[0]
    )
    assert original.result is not None
    source_id = str(original.result["source_id"])
    provenance = corpus.rows("SELECT id, provenance_json FROM source_artifacts")
    remote.resolved = "https://example.test/moved.html"
    observed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert observed.result is not None and observed.result["outcome"] == "unchanged"
    assert observed.result["observation"]["resolved_url"] == remote.resolved
    assert corpus.rows("SELECT id, provenance_json FROM source_artifacts") == provenance
    remote.content = html("remote new bytes")
    changed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert changed.status == "done", changed.error
    assert remote.calls == 3
    remote.fail = True
    failed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert failed.status == "failed" and "404" in str(failed.error)


def test_interrupted_refresh_is_recovered_only_without_a_live_worker(corpus: Corpus) -> None:
    job = enqueue_refresh(corpus.paths.database, corpus.source_id)
    assert claim_next_job(corpus.paths.database) is not None
    # Simulate process death after claim: no process owns the worker lock.
    assert not asyncio.run(corpus.runner.run_cycle())
    interrupted = get_job(corpus.paths.database, job.id)
    assert interrupted.status == "failed" and "refresh_interrupted" in str(interrupted.error)
    assert corpus.run(retry_failed_job(corpus.paths.database, job.id)).status == "done"


def test_second_daemon_does_not_recover_or_take_over_live_refresh(corpus: Corpus) -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def paused(job: Job) -> dict[str, object]:
            started.set()
            await release.wait()
            return await corpus.refresh.handle_job(job)

        first = DaemonRunner(
            database_path=corpus.paths.database, handlers={REFRESH_JOB_KIND: paused}
        )
        second = DaemonRunner(
            database_path=corpus.paths.database, handlers={REFRESH_JOB_KIND: paused}
        )
        job = enqueue_refresh(corpus.paths.database, corpus.source_id)
        work = asyncio.create_task(first.run_cycle())
        await started.wait()
        try:
            assert not await second.run_cycle()
            assert get_job(corpus.paths.database, job.id).status == "running"
        finally:
            release.set()
            assert await work
        assert get_job(corpus.paths.database, job.id).status == "done"

    asyncio.run(exercise())


def test_cancelled_daemon_retains_ownership_until_processing_finishes(corpus: Corpus) -> None:
    async def exercise() -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        async def paused(job: Job) -> dict[str, object]:
            started.set()
            await release.wait()
            return await corpus.refresh.handle_job(job)

        first = DaemonRunner(
            database_path=corpus.paths.database, handlers={REFRESH_JOB_KIND: paused}
        )
        second = DaemonRunner(
            database_path=corpus.paths.database, handlers={REFRESH_JOB_KIND: paused}
        )
        job = enqueue_refresh(corpus.paths.database, corpus.source_id)
        work = asyncio.create_task(first.run_cycle())
        await started.wait()
        work.cancel()
        await asyncio.sleep(0)
        assert not await second.run_cycle()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert get_job(corpus.paths.database, job.id).status == "done"

    asyncio.run(exercise())


@pytest.mark.parametrize("saved", [True, False])
def test_retry_cli_explains_saved_candidate_or_reacquisition(corpus: Corpus, saved: bool) -> None:
    if saved:
        corpus.embeddings.fail = True
        failed = corpus.update(html("retry candidate"))
    else:
        corpus.path.unlink()
        failed = corpus.run(enqueue_refresh(corpus.paths.database, corpus.source_id))
    assert failed.status == "failed"
    output = CliRunner().invoke(
        app,
        [
            "--data-dir",
            str(corpus.paths.database.parent),
            "jobs",
            "retry",
            failed.id,
        ],
    )
    assert output.exit_code == 0, output.exception
    assert ("Retrying saved artifact" if saved else "retry will reacquire") in output.stdout


def test_refresh_registered_symlink_follows_a_new_stable_target(corpus: Corpus) -> None:
    target_a = corpus.path.with_name("target-a.html")
    target_b = corpus.path.with_name("target-b.html")
    alias = corpus.path.with_name("alias.html")
    target_a.write_bytes(html("alias first version"))
    target_b.write_bytes(html("alias second version"))
    alias.symlink_to(target_a)
    initial = corpus.run(enqueue_ingest_source(corpus.paths.database, source=str(alias)).jobs[0])
    assert initial.result is not None
    source_id = initial.result["source_id"]
    alias.unlink()
    alias.symlink_to(target_b)
    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert refreshed.status == "done", refreshed.error
    assert refreshed.result is not None and refreshed.result["outcome"] == "revision_created"
    assert refreshed.result["source_id"] == source_id
    assert refreshed.payload["candidate"]["observation"]["resolved_path"] == str(target_b.resolve())


def test_committed_receipt_survives_failure_acknowledgement_and_replay(corpus: Corpus) -> None:
    completed = corpus.update(html("committed revision"))
    assert completed.status == "done"
    count = len(corpus.rows("SELECT id FROM source_revisions"))
    after = mark_job_failed(corpus.paths.database, completed.id, error="crash after commit")
    assert after.status == "done" and after.result == completed.result
    assert corpus.refresh.process_job(completed) == completed.result
    assert len(corpus.rows("SELECT id FROM source_revisions")) == count
