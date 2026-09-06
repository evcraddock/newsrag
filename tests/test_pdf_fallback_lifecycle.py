from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import pytest
import test_text_ingestion as support
from test_pdf_fallback import FailedOcr, make_pdf

from newsrag.adapters import AdapterError
from newsrag.config import EmbeddingConfig
from newsrag.daemon import DaemonRunner
from newsrag.ingest import INGEST_JOB_KIND, IngestionPipeline, enqueue_ingest_source, list_documents
from newsrag.jobs import retry_failed_job
from newsrag.packets import format_source_packet, load_packet_source_provenance
from newsrag.pdf_adapter import ExtractedPage, PyMuPdfTextExtractor
from newsrag.refresh import REFRESH_JOB_KIND, RefreshPipeline
from newsrag.reprocess import REPROCESS_JOB_KIND, ReprocessingPipeline, enqueue_reprocessing
from newsrag.storage import initialize_storage


@dataclass
class ToggleOcr(FailedOcr):
    succeed: bool = False

    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        if self.succeed:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_bytes(source_path.read_bytes())
        else:
            super().normalize_pdf(source_path, output_path)


def corpus_for(tmp_path: Path, ocr: FailedOcr) -> support.Corpus:
    paths = initialize_storage(tmp_path / "corpus")
    embeddings = support.FakeEmbeddingProvider()
    ingestion = IngestionPipeline(
        storage_paths=paths,
        embedding_config=EmbeddingConfig(),
        embedding_provider=embeddings,
        ocr_runner=ocr,
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
    return support.Corpus(paths, embeddings, ingestion, refresh, reprocessing, runner)


@pytest.mark.parametrize("mode", ["auto", "pdfplumber", "auto-primary-failure"])
def test_daemon_publishes_searchable_original_text_with_accurate_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    corpus = corpus_for(tmp_path, FailedOcr())
    source = make_pdf(tmp_path / "packet.pdf")
    if mode == "auto-primary-failure":

        def broken_primary(self: PyMuPdfTextExtractor, path: Path) -> list[ExtractedPage]:
            raise AdapterError("primary extraction failed")

        monkeypatch.setattr(PyMuPdfTextExtractor, "extract_pages", broken_primary)
    jobs = enqueue_ingest_source(
        corpus.paths.database,
        source=str(source),
        pdf_extractor="auto" if mode == "auto-primary-failure" else mode,
    ).jobs
    completed = corpus.run(jobs[0])
    assert completed.status == "done"
    document = list_documents(corpus.paths.database)[0]
    assert document.normalized_path is None
    assert corpus.rows("SELECT extractor FROM pages") == [
        ("pymupdf" if mode == "auto" else "pdfplumber",)
    ]
    assert json.loads(corpus.rows("SELECT structure_json FROM source_units")[0][0]) == {
        "text_source": "original",
        "ocr_exit_code": 4,
    }
    configuration = json.loads(
        corpus.rows("SELECT configuration_json FROM processing_generations")[0][0]
    )
    assert configuration["adapter"]["settings"]["format_version"] == "2"
    results = support._keyword_results(corpus.paths.database, "drainage")
    assert results and results[0].citation.endswith("p. 1")
    provenance = load_packet_source_provenance(corpus.paths.database, results)
    packet = format_source_packet(query="drainage", results=results, source_provenance=provenance)
    assert "Council approved" in packet
    assert "normalized artifact:" not in packet
    assert "Partial OCR output" not in packet
    assert corpus.ingest(str(source)).status == "done"
    assert len(list_documents(corpus.paths.database)) == 1


@pytest.mark.parametrize("source_kind", ["blank", "malformed"])
def test_failed_fallback_publishes_no_partial_document_or_evidence(
    tmp_path: Path, source_kind: str
) -> None:
    corpus = corpus_for(tmp_path, FailedOcr())
    good = make_pdf(tmp_path / "good.pdf", "Existing searchable council evidence.")
    assert corpus.ingest(str(good)).status == "done"
    before = corpus.rows("SELECT id FROM documents")
    source = make_pdf(tmp_path / "bad.pdf", "")
    if source_kind == "malformed":
        source.write_bytes(b"%PDF-1.4\nnot a readable document")
    failed = corpus.ingest(str(source))
    assert failed.status == "failed"
    assert "exit status 4" in str(failed.error)
    assert "original" in str(failed.error) and "retry" in str(failed.error)
    assert corpus.rows("SELECT id FROM documents") == before
    assert len(corpus.rows("SELECT id FROM source_units")) == 1
    assert len(corpus.rows("SELECT id FROM processing_generations")) == 1
    assert support._keyword_results(corpus.paths.database, "Existing")
    assert not support._keyword_results(corpus.paths.database, "Partial")


def test_retry_recovers_and_reuses_the_original_artifact(tmp_path: Path) -> None:
    ocr = FailedOcr(returncode=5)
    corpus = corpus_for(tmp_path, ocr)
    source = make_pdf(tmp_path / "retry.pdf")
    failed = corpus.ingest(str(source))
    assert failed.status == "failed"
    original_artifacts = corpus.rows("SELECT id FROM source_artifacts")
    ocr.returncode = 4
    retried = corpus.run(retry_failed_job(corpus.paths.database, failed.id))
    assert retried.status == "done"
    assert corpus.rows("SELECT id FROM source_artifacts") == original_artifacts
    assert support._keyword_results(corpus.paths.database, "drainage")
    assert list_documents(corpus.paths.database)[0].normalized_path is None


def test_reprocessing_fallback_preserves_old_normalized_artifact_and_failed_attempt_preserves_active_evidence(
    tmp_path: Path,
) -> None:
    ocr = ToggleOcr(succeed=True)
    corpus = corpus_for(tmp_path, ocr)
    source = make_pdf(tmp_path / "history.pdf")
    assert corpus.ingest(str(source)).status == "done"
    document = list_documents(corpus.paths.database)[0]
    assert document.normalized_path is not None
    previous_pdf = Path(document.normalized_path)
    previous_bytes = previous_pdf.read_bytes()
    old_results = support._keyword_results(corpus.paths.database, "drainage")
    source.unlink()
    ocr.succeed = False
    reprocessed = corpus.run(
        enqueue_reprocessing(corpus.paths.database, [document.id], pdf_extractor="pdfplumber")[0]
    )
    assert reprocessed.status == "done"
    assert corpus.rows(
        "SELECT g.normalized_path FROM processing_generations g JOIN documents d ON d.current_processing_generation_id = g.id"
    ) == [(None,)]
    assert previous_pdf.read_bytes() == previous_bytes
    old_provenance = load_packet_source_provenance(corpus.paths.database, old_results)
    assert old_provenance[document.id].normalized_path == str(previous_pdf)
    before = corpus.rows("SELECT id, current_processing_generation_id FROM documents")
    ocr.returncode = 5
    failed = corpus.run(enqueue_reprocessing(corpus.paths.database, [document.id])[0])
    assert failed.status == "failed"
    assert corpus.rows("SELECT id, current_processing_generation_id FROM documents") == before
    assert previous_pdf.read_bytes() == previous_bytes
    assert support._keyword_results(corpus.paths.database, "drainage")
