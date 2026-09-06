from __future__ import annotations

import json
from pathlib import Path

import pytest
import test_text_ingestion as support

from newsrag.ingest import IngestError, enqueue_ingest_source, prepare_ingest_source
from newsrag.manifests import ManifestError, load_manifest
from newsrag.reprocess import ReprocessingError, enqueue_reprocessing


def test_csv_publication_owns_cells_stripes_and_context(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "expenses.CSV"
    source.write_text("Department,Amount\nRoads,001200\nParks,5\n")
    job = corpus.ingest(str(source))
    assert job.status == "done", job.error
    assert len(corpus.rows("SELECT * FROM source_tables")) == 1
    assert len(corpus.rows("SELECT * FROM table_cells")) == 6
    passages = corpus.rows(
        "SELECT focus_json, context_json FROM table_passages ORDER BY focus_text"
    )
    assert len(passages) == 2
    assert json.loads(passages[0][0])["row_start"] == 2
    assert [
        value[0]
        for value in corpus.rows(
            "SELECT cell_json FROM table_cells WHERE row_number=2 AND column_number=2"
        )
    ] == [
        '{"column":2,"kind":"string","metadata":{},"presence":"stored","raw":"001200","row":2,"value":"001200","visible":true}'
    ]
    assert len(support._lance_rows(corpus.paths.lancedb, "passage_embeddings")) == 2


def test_recipe_validation_directory_and_manifest_are_atomic(tmp_path: Path) -> None:
    source = tmp_path / "expenses.csv"
    source.write_text("a;b\nx;y\n")
    with pytest.raises(IngestError, match="directory"):
        prepare_ingest_source(source=str(tmp_path), csv_options={"delimiter": "semicolon"})
    with pytest.raises(IngestError, match="conflict"):
        prepare_ingest_source(source=str(source), source_type="text", csv_options={})
    manifest = tmp_path / "sources.yaml"
    manifest.write_text(
        f"documents:\n  - source: {source}\n    csv:\n      delimiter: semicolon\n  - source: {tmp_path / 'other.csv'}\n    csv:\n      unknown: true\n"
    )
    with pytest.raises(ManifestError, match="only"):
        load_manifest(manifest)


def test_reprocessing_options_preserve_artifact_and_old_selectors(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "expenses.csv"
    source.write_text("Department,Amount\nRoads,001200\nParks,5\n")
    job = corpus.ingest(str(source))
    assert job.status == "done", job.error
    assert job.result is not None
    document_id = str(job.result["document_id"])
    before = corpus.rows("SELECT passage_id, focus_json FROM table_passages")
    artifact = corpus.rows("SELECT artifact_id FROM documents")
    source.unlink()
    changed = corpus.run(
        enqueue_reprocessing(
            corpus.paths.database, [document_id], csv_options={"header": "absent"}
        )[0]
    )
    assert changed.status == "done", changed.error
    assert len(corpus.rows("SELECT * FROM source_tables")) == 2
    assert len(corpus.rows("SELECT * FROM table_passages")) == 5
    assert corpus.rows("SELECT artifact_id FROM documents") == artifact
    assert all(
        row in corpus.rows("SELECT passage_id, focus_json FROM table_passages") for row in before
    )
    unchanged = corpus.run(enqueue_reprocessing(corpus.paths.database, [document_id])[0])
    assert unchanged.status == "done", unchanged.error
    assert unchanged.result is not None and unchanged.result["outcome"] == "unchanged"


def test_mixed_reprocessing_rejects_csv_overrides_for_whole_batch(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    csv = tmp_path / "source.csv"
    csv.write_text("h\nv\n")
    text = tmp_path / "source.txt"
    text.write_text("plain text")
    ids = []
    for source in (csv, text):
        job = corpus.ingest(str(source))
        assert job.status == "done", job.error
        assert job.result is not None
        ids.append(str(job.result["document_id"]))
    with pytest.raises(ReprocessingError, match="only to CSV"):
        enqueue_reprocessing(corpus.paths.database, ids, csv_options={"header": "absent"})
    assert corpus.rows("SELECT * FROM jobs WHERE kind = 'reprocess-document'") == []


def test_options_select_csv_without_filename_evidence(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "export"
    source.write_text("a;b\nx;y\n")
    job = enqueue_ingest_source(
        corpus.paths.database, source=str(source), csv_options={"delimiter": "semicolon"}
    ).jobs[0]
    completed = corpus.run(job)
    assert completed.status == "done", completed.error
