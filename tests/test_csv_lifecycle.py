from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

import pytest
import test_text_ingestion as support

from newsrag.acquisition import SafeSourceArtifactAcquirer
from newsrag.ingest import PassageVectorStore, enqueue_ingest_source
from newsrag.jobs import retry_failed_job
from newsrag.packets import format_source_packet, load_packet_source_provenance
from newsrag.refresh import enqueue_refresh
from newsrag.reprocess import enqueue_reprocessing
from newsrag.search import PassageVectorRecord


@pytest.mark.parametrize(
    "media,hint,filename",
    [
        ("text/csv; charset=windows-1252", None, "export"),
        ("application/csv; charset=windows-1252", None, "export"),
        ("text/plain; charset=windows-1252", "csv", "export"),
        ("application/octet-stream", "csv", "export"),
        ("application/octet-stream", None, "export.CSV"),
    ],
)
def test_public_http_csv_registration_and_charset(
    tmp_path: Path, media: str, hint: str | None, filename: str
) -> None:
    value = "Café" if "charset" in media else "Cafe"
    content = f"Name\n{value}\n".encode("cp1252")
    response = support.HttpResponse(200, {"content-type": media}, (content,))
    transport = support.HttpTransport([response])
    corpus = support._corpus(
        tmp_path / "corpus",
        acquirer=SafeSourceArtifactAcquirer(resolver=support._public_resolver, transport=transport),
    )
    job = corpus.ingest(f"https://example.gov/{filename}", source_type=hint)
    assert job.status == "done", job.error
    assert support._keyword_results(corpus.paths.database, value)
    assert corpus.rows("SELECT reported_media_type FROM source_artifacts") == [(media,)]


def test_generic_without_evidence_does_not_guess_csv(tmp_path: Path) -> None:
    response = support.HttpResponse(
        200, {"content-type": "application/octet-stream"}, (b"Name\nRoads\n",)
    )
    corpus = support._corpus(
        tmp_path / "corpus",
        acquirer=SafeSourceArtifactAcquirer(
            resolver=support._public_resolver, transport=support.HttpTransport([response])
        ),
    )
    job = corpus.ingest("https://example.gov/export")
    assert job.status == "failed" and "Unsupported source type" in str(job.error)
    assert corpus.rows("SELECT * FROM documents") == []


def test_refresh_inherits_active_recipe_and_retains_packet_snapshot(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "expenses.csv"
    source.write_text("Name;Amount\nRoads;001200\n")
    initial = corpus.run(
        enqueue_ingest_source(
            corpus.paths.database, source=str(source), csv_options={"delimiter": "semicolon"}
        ).jobs[0]
    )
    assert initial.status == "done" and initial.result is not None, initial.error
    document_id, source_id = str(initial.result["document_id"]), str(initial.result["source_id"])
    old_results = support._keyword_results(corpus.paths.database, "Roads")
    old_packet = format_source_packet(
        query="Roads",
        results=old_results,
        source_provenance=load_packet_source_provenance(corpus.paths.database, old_results),
    )
    reprocessed = corpus.run(
        enqueue_reprocessing(
            corpus.paths.database, [document_id], csv_options={"header": "absent"}
        )[0]
    )
    assert reprocessed.status == "done", reprocessed.error
    duplicate = corpus.run(
        enqueue_ingest_source(
            corpus.paths.database, source=str(source), csv_options={"delimiter": "comma"}
        ).jobs[0]
    )
    assert duplicate.result is not None and duplicate.result["outcome"] == "duplicate_ignored"
    source.write_text("Name;Amount\nRoads;001300\n")
    staged = corpus.ingest(str(source))
    assert (
        staged.result is not None and staged.result["outcome"] == "change_detected_artifact_saved"
    )
    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert refreshed.status == "done", refreshed.error
    descriptor = json.loads(
        corpus.rows("SELECT descriptor_json FROM source_tables ORDER BY rowid DESC LIMIT 1")[0][0]
    )
    assert descriptor["header_row"] is None
    assert descriptor["column_end"] == 2
    assert descriptor["metadata"]["requested_recipe"]["delimiter"] == "semicolon"
    assert old_packet == format_source_packet(
        query="Roads",
        results=old_results,
        source_provenance=load_packet_source_provenance(corpus.paths.database, old_results),
    )
    unchanged = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert unchanged.result is not None and unchanged.result["outcome"] == "unchanged"
    source.write_text("Name;Amount\nRoads;001200\n")
    reactivated = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert (
        reactivated.result is not None and reactivated.result["outcome"] == "revision_reactivated"
    )


def test_refresh_revalidates_new_charset_and_retries_saved_invalid_bytes(tmp_path: Path) -> None:
    first = b"Name\nCaf\xe9\n"
    second = "Name\nCafé Roads\n".encode()
    transport = support.HttpTransport(
        [
            support.HttpResponse(
                200, {"content-type": "text/plain; charset=windows-1252"}, (first,)
            ),
            support.HttpResponse(200, {"content-type": "text/plain; charset=utf-8"}, (second,)),
            support.HttpResponse(
                200, {"content-type": "text/plain; charset=utf-8"}, (b'Name\n"unclosed',)
            ),
        ]
    )
    corpus = support._corpus(
        tmp_path / "corpus",
        acquirer=SafeSourceArtifactAcquirer(resolver=support._public_resolver, transport=transport),
    )
    initial = corpus.ingest("https://example.gov/export", source_type="csv")
    assert initial.status == "done" and initial.result is not None, initial.error
    source_id = str(initial.result["source_id"])
    refreshed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert refreshed.status == "done", refreshed.error
    descriptor = json.loads(
        corpus.rows("SELECT descriptor_json FROM source_tables ORDER BY rowid DESC LIMIT 1")[0][0]
    )
    assert descriptor["metadata"]["interpretation"]["encoding"] == "utf-8"
    assert descriptor["metadata"]["requested_recipe"]["encoding"] == "auto"
    failed = corpus.run(enqueue_refresh(corpus.paths.database, source_id))
    assert failed.status == "failed" and "unclosed" in str(failed.error)
    retried = corpus.run(retry_failed_job(corpus.paths.database, failed.id))
    assert retried.status == "failed" and "unclosed" in str(retried.error)
    assert len(transport.requests) == 3
    assert len(corpus.rows("SELECT * FROM source_tables")) == 2
    assert support._keyword_results(corpus.paths.database, "Roads")


def test_publication_failure_rolls_back_tables_and_retry_retains_recipe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "expenses.csv"
    source.write_text("Name,Amount\nRoads,001200\n")
    initial = corpus.ingest(str(source))
    assert initial.status == "done" and initial.result is not None
    document_id = str(initial.result["document_id"])
    before = {
        name: corpus.rows(f"SELECT * FROM {name}")
        for name in ("source_tables", "table_cells", "table_passages", "processing_generations")
    }
    real_publish = corpus.ingestion.processor.passage_vector_store
    original_add = type(real_publish).add_passages

    def fail_after_vectors(
        self: PassageVectorStore, passages: Sequence[PassageVectorRecord]
    ) -> None:
        original_add(self, passages)
        raise RuntimeError("injected after CSV vectors")

    monkeypatch.setattr(type(real_publish), "add_passages", fail_after_vectors)
    failed = corpus.run(
        enqueue_reprocessing(
            corpus.paths.database, [document_id], csv_options={"header": "absent"}
        )[0]
    )
    assert failed.status == "failed" and "injected" in str(failed.error)
    assert before == {name: corpus.rows(f"SELECT * FROM {name}") for name in before}
    assert len(support._lance_rows(corpus.paths.lancedb, "passage_embeddings")) == 1
    monkeypatch.setattr(type(real_publish), "add_passages", original_add)
    retried = corpus.run(retry_failed_job(corpus.paths.database, failed.id))
    assert retried.status == "done", retried.error
    assert len(corpus.rows("SELECT * FROM source_tables")) == 2
