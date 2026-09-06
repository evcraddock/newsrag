from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import test_text_ingestion as support
from test_xlsx_ingestion import _workbook
from typer.testing import CliRunner

from newsrag.acquisition import SafeSourceArtifactAcquirer
from newsrag.cli import app
from newsrag.ingest import PassageVectorStore, enqueue_ingest_source
from newsrag.jobs import retry_failed_job
from newsrag.packets import format_source_packet, load_packet_source_provenance
from newsrag.refresh import enqueue_refresh
from newsrag.reprocess import ReprocessingError, enqueue_reprocessing
from newsrag.search import PassageVectorRecord
from newsrag.sources import XLSX_MAX_SOURCE_BYTES, XLSX_MEDIA_TYPE


def _latest_descriptor(corpus: support.Corpus) -> dict[str, object]:
    return dict(
        json.loads(
            corpus.rows("SELECT descriptor_json FROM source_tables ORDER BY rowid DESC LIMIT 1")[0][
                0
            ]
        )
    )


def test_reprocess_saved_bytes_clears_map_and_preserves_old_packet(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = _workbook(tmp_path / "budget.xlsx")
    initial = corpus.run(
        enqueue_ingest_source(
            corpus.paths.database, source=str(source), xlsx_options={"header_rows": {"Sheet1": 1}}
        ).jobs[0]
    )
    assert initial.status == "done" and initial.result is not None, initial.error
    document_id = str(initial.result["document_id"])
    identity = corpus.rows("SELECT artifact_id, source_hash FROM documents")
    old_rows = corpus.rows("SELECT id, location_json FROM source_units")
    old_selectors = corpus.rows("SELECT passage_id, focus_json FROM table_passages")
    old_results = support._keyword_results(corpus.paths.database, "Roads")
    packet = format_source_packet(
        query="Roads",
        results=old_results,
        source_provenance=load_packet_source_provenance(corpus.paths.database, old_results),
    )
    source.unlink()
    unchanged = corpus.run(enqueue_reprocessing(corpus.paths.database, [document_id])[0])
    assert unchanged.result is not None and unchanged.result["outcome"] == "unchanged"
    cli = CliRunner().invoke(
        app,
        [
            "--config-path",
            str(tmp_path / "missing.yaml"),
            "--data-dir",
            str(corpus.paths.data_dir),
            "reprocess",
            document_id,
            "--xlsx-header-rows",
            "{}",
        ],
    )
    assert cli.exit_code == 0, cli.stdout
    assert all(job.status == "done" for job in corpus.drain())
    assert _latest_descriptor(corpus)["header_row"] is None
    assert len(corpus.rows("SELECT * FROM processing_generations")) == 2
    assert corpus.rows("SELECT artifact_id, source_hash FROM documents") == identity
    assert all(row in corpus.rows("SELECT id, location_json FROM source_units") for row in old_rows)
    assert all(
        row in corpus.rows("SELECT passage_id, focus_json FROM table_passages")
        for row in old_selectors
    )
    assert (
        format_source_packet(
            query="Roads",
            results=old_results,
            source_provenance=load_packet_source_provenance(corpus.paths.database, old_results),
        )
        == packet
    )
    unchanged = corpus.run(enqueue_reprocessing(corpus.paths.database, [document_id])[0])
    assert unchanged.result is not None and unchanged.result["outcome"] == "unchanged"
    assert not source.exists()


def test_refresh_uses_current_generation_recipe_duplicate_history_and_reactivation(
    tmp_path: Path,
) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = _workbook(tmp_path / "budget.xlsx")
    original = source.read_bytes()
    initial = corpus.ingest(str(source))
    assert initial.status == "done" and initial.result is not None, initial.error
    source_id, document_id = str(initial.result["source_id"]), str(initial.result["document_id"])
    reprocessed = corpus.run(
        enqueue_reprocessing(
            corpus.paths.database, [document_id], xlsx_options={"header_rows": {"Sheet1": 1}}
        )[0]
    )
    assert reprocessed.status == "done", reprocessed.error
    duplicate = corpus.run(
        enqueue_ingest_source(
            corpus.paths.database, source=str(source), xlsx_options={"header_rows": {}}
        ).jobs[0]
    )
    assert duplicate.result is not None and duplicate.result["outcome"] == "duplicate_ignored"
    _workbook(source, "Revisedroads")
    staged = corpus.ingest(str(source))
    assert (
        staged.result is not None and staged.result["outcome"] == "change_detected_artifact_saved"
    )
    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert refreshed.status == "done", refreshed.error
    assert _latest_descriptor(corpus)["header_row"] == 1
    assert refreshed.payload["base"]["options"]["xlsx"] == {"header_rows": {"Sheet1": 1}}
    assert support._keyword_results(corpus.paths.database, "Revisedroads")
    assert support._keyword_results(corpus.paths.database, "Roads", include_history=True)
    noop = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert noop.result is not None and noop.result["outcome"] == "unchanged"
    source.write_bytes(original)
    reactivated = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert (
        reactivated.result is not None and reactivated.result["outcome"] == "revision_reactivated"
    )
    assert reactivated.result["document_id"] == document_id
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(2,)]


def test_refresh_generic_transport_retains_type_limit_and_saved_candidate_on_retry(
    tmp_path: Path,
) -> None:
    first = _workbook(tmp_path / "first.xlsx").read_bytes()
    responses = [
        support.HttpResponse(200, {"content-type": XLSX_MEDIA_TYPE}, (first,)),
        support.HttpResponse(200, {"content-type": "application/zip"}, (b"broken workbook",)),
        support.HttpResponse(
            200,
            {
                "content-type": "application/octet-stream",
                "content-length": str(XLSX_MAX_SOURCE_BYTES + 1),
            },
            (b"not read",),
        ),
    ]
    transport = support.HttpTransport(responses)
    corpus = support._corpus(
        tmp_path / "corpus",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=support._public_resolver,
            transport=transport,
        ),
    )
    initial = corpus.ingest("https://example.gov/export")
    assert initial.status == "done" and initial.result is not None, initial.error
    source_id = str(initial.result["source_id"])
    failed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert failed.status == "failed"
    retried = corpus.run(retry_failed_job(corpus.paths.database, failed.id))
    assert retried.status == "failed" and len(transport.requests) == 2
    assert retried.payload["candidate"] == failed.payload["candidate"]
    assert retried.payload["base"]["options"]["xlsx"] == {"header_rows": {}}
    oversized = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert oversized.status == "failed" and str(XLSX_MAX_SOURCE_BYTES) in str(oversized.error)
    assert corpus.rows("SELECT COUNT(*) FROM source_revisions") == [(1,)]
    assert support._keyword_results(corpus.paths.database, "Roads")


def test_mixed_reprocessing_validates_entire_batch_and_active_job_options(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = _workbook(tmp_path / "budget.xlsx")
    text = tmp_path / "notes.txt"
    text.write_text("Public meeting")
    ids = []
    for path in (source, text):
        job = corpus.ingest(str(path))
        assert job.status == "done" and job.result is not None, job.error
        ids.append(str(job.result["document_id"]))
    conflicts: tuple[dict[str, Any], ...] = (
        {"xlsx_options": {"header_rows": {}}},
        {"csv_options": {"header": "absent"}},
        {"pdf_extractor": "auto"},
        {"xlsx_options": {}, "csv_options": {}},
    )
    for options in conflicts:
        with pytest.raises(ReprocessingError):
            enqueue_reprocessing(corpus.paths.database, ids, **options)
        assert corpus.rows("SELECT * FROM jobs WHERE kind='reprocess-document'") == []
    jobs = enqueue_reprocessing(corpus.paths.database, ids)
    assert len(jobs) == 2
    with pytest.raises(ReprocessingError, match="different options"):
        enqueue_reprocessing(
            corpus.paths.database, [ids[0]], xlsx_options={"header_rows": {"Sheet1": 1}}
        )
    assert len(enqueue_reprocessing(corpus.paths.database, ids)) == 2
    assert all(job.status == "done" for job in corpus.drain())


def test_publication_failure_compensates_only_attempt_and_retry_keeps_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = _workbook(tmp_path / "budget.xlsx")
    initial = corpus.ingest(str(source))
    assert initial.status == "done" and initial.result is not None
    document_id = str(initial.result["document_id"])
    before = {
        name: corpus.rows(f"SELECT * FROM {name}")
        for name in (
            "source_tables",
            "table_cells",
            "table_passages",
            "source_units",
            "processing_generations",
        )
    }
    vector_ids = {
        row["passage_id"] for row in support._lance_rows(corpus.paths.lancedb, "passage_embeddings")
    }
    store = corpus.ingestion.processor.passage_vector_store
    original_add = type(store).add_passages

    def fail_after_vectors(
        self: PassageVectorStore, passages: Sequence[PassageVectorRecord]
    ) -> None:
        original_add(self, passages)
        raise RuntimeError("injected XLSX publication failure")

    monkeypatch.setattr(type(store), "add_passages", fail_after_vectors)
    failed = corpus.run(
        enqueue_reprocessing(
            corpus.paths.database, [document_id], xlsx_options={"header_rows": {"Sheet1": 1}}
        )[0]
    )
    assert failed.status == "failed" and "injected" in str(failed.error)
    assert before == {name: corpus.rows(f"SELECT * FROM {name}") for name in before}
    assert {
        row["passage_id"] for row in support._lance_rows(corpus.paths.lancedb, "passage_embeddings")
    } == vector_ids
    monkeypatch.setattr(type(store), "add_passages", original_add)
    retried = corpus.run(retry_failed_job(corpus.paths.database, failed.id))
    assert retried.status == "done", retried.error
    assert _latest_descriptor(corpus)["header_row"] == 1
    assert len(corpus.rows("SELECT * FROM processing_generations")) == 2


def test_saved_refresh_candidate_keeps_captured_recipe_after_base_reprocessing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = _workbook(tmp_path / "budget.xlsx")
    initial = corpus.run(
        enqueue_ingest_source(
            corpus.paths.database, source=str(source), xlsx_options={"header_rows": {"Sheet1": 1}}
        ).jobs[0]
    )
    assert initial.status == "done" and initial.result is not None, initial.error
    document_id, source_id = str(initial.result["document_id"]), str(initial.result["source_id"])
    _workbook(source, "Capturedroads")
    store = corpus.ingestion.processor.passage_vector_store
    original_add = type(store).add_passages

    def fail_after_vectors(
        self: PassageVectorStore, passages: Sequence[PassageVectorRecord]
    ) -> None:
        original_add(self, passages)
        raise RuntimeError("injected captured refresh failure")

    monkeypatch.setattr(type(store), "add_passages", fail_after_vectors)
    failed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert failed.status == "failed" and "injected" in str(failed.error)
    monkeypatch.setattr(type(store), "add_passages", original_add)
    changed = corpus.run(
        enqueue_reprocessing(
            corpus.paths.database, [document_id], xlsx_options={"header_rows": {}}
        )[0]
    )
    assert changed.status == "done", changed.error
    assert _latest_descriptor(corpus)["header_row"] is None
    # Neither new source bytes nor the new active recipe may replace the saved candidate.
    _workbook(source, "Uncapturedroads")
    retried = corpus.run(retry_failed_job(corpus.paths.database, failed.id))
    assert retried.status == "done", retried.error
    assert _latest_descriptor(corpus)["header_row"] == 1
    assert support._keyword_results(corpus.paths.database, "Capturedroads")
    assert support._keyword_results(corpus.paths.database, "Uncapturedroads") == []
    assert retried.payload["candidate"] == failed.payload["candidate"]
