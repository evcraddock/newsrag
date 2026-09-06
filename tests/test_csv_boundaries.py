from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import test_text_ingestion as support
from test_csv_adapter import extract
from test_tabular import table

from newsrag.adapters import AdapterError
from newsrag.csv_adapter import _records
from newsrag.ingest import IngestError, prepare_ingest_source
from newsrag.reprocess import enqueue_reprocessing
from newsrag.search import (
    _load_eligible_document_snapshot,
    merge_search_candidates,
    search_keyword_candidates,
)
from newsrag.source_locations import SourceLocationError, resolve_source_range
from newsrag.storage import StorageError, initialize_storage
from newsrag.tabular import Cell, TableError, build_table_passages


@pytest.mark.parametrize(
    "data,error",
    [
        (b"x" * (10 * 1024 * 1024 + 1), "byte"),
        (b"h\n" + b"x\n" * 100_000, "line"),
        ((b",".join([b"x"] * 256) + b"\n") * 3907, "rectangular"),
        (b"h\n" + b"\x1b[31mred", "control"),
        (b"h\n" + b"\xff\xfe", "valid utf-8"),
    ],
)
def test_csv_raw_physical_geometry_and_control_limits(
    tmp_path: Path, data: bytes, error: str
) -> None:
    with pytest.raises(AdapterError, match=error):
        extract(tmp_path, data)


def test_logical_record_limit_is_independent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("newsrag.csv_adapter.MAX_RECORDS", 2)
    with pytest.raises(AdapterError, match="logical-record"):
        list(_records('h\n"embedded\nnewline"\nx', ",", physical_lines=4))


@pytest.mark.parametrize(
    "constant,value",
    [
        ("MAX_METADATA_BYTES", 20),
        ("MAX_TOTAL_VALUE_CHARS", 3),
        ("MAX_INDEX_BYTES", 20),
        ("MAX_PASSAGES", 1),
    ],
)
def test_generated_budgets_fail_not_truncate(
    monkeypatch: pytest.MonkeyPatch, constant: str, value: int
) -> None:
    monkeypatch.setattr(f"newsrag.tabular.{constant}", value)
    with pytest.raises(TableError, match="budget"):
        build_table_passages((table(),))


def test_owned_selector_bundle_has_an_aggregate_metadata_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from newsrag.ingest import _build_source_unit_rows
    from newsrag.tabular_storage import validate_bundle_metadata

    result = extract(tmp_path, b"Header\nRoads\nParks\n")
    _, units, _ = _build_source_unit_rows("document-test", "artifact-test", result.units)
    passages = build_table_passages(result.tables)
    monkeypatch.setattr("newsrag.tabular.MAX_METADATA_BYTES", 1000)
    with pytest.raises(TableError, match="serialized metadata"):
        validate_bundle_metadata(
            document_id="document-test",
            generation_id="generation-test",
            tables=result.tables,
            source_units=units,
            passages=passages,
        )


def test_shared_typed_values_and_merge_coverage_are_validated() -> None:
    for kind, value in (
        ("number", "NaN"),
        ("number", "1,200"),
        ("date", "not-a-date"),
        ("boolean", "true"),
    ):
        with pytest.raises(TableError):
            Cell(1, 1, value=value, kind=kind).validate()
    Cell(1, 1, value="001200.00", kind="number").validate()
    merged = replace(
        table(),
        source_type="xlsx",
        metadata={
            "merges": [
                {
                    "table_id": "sheet-1",
                    "sheet_index": 1,
                    "region_kind": "cells",
                    "row_start": 2,
                    "row_end": 2,
                    "column_start": 1,
                    "column_end": 2,
                }
            ]
        },
    )
    with pytest.raises(TableError, match="coverage"):
        merged.validate()
    merged = replace(
        merged,
        cells=tuple(
            replace(cell, kind="merged-covered", value=None, presence="merged-covered")
            if (cell.row, cell.column) == (2, 2)
            else cell
            for cell in merged.cells
        ),
    )
    merged.validate()


def test_cli_recipe_flags_cannot_conflict_with_pdf_even_auto(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="conflict"):
        prepare_ingest_source(source=str(tmp_path / "data.csv"), pdf_extractor="auto")
    with pytest.raises(IngestError, match="conflict"):
        prepare_ingest_source(source=str(tmp_path / "data"), csv_options={}, pdf_extractor="auto")


def test_migration_and_resolution_reject_corrupt_context_ownership(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "data.csv"
    source.write_text("h\nRoads\nParks\n")
    assert corpus.ingest(str(source)).status == "done"
    result = support._keyword_results(corpus.paths.database, "Roads")[0]
    with sqlite3.connect(corpus.paths.database) as connection:
        row = connection.execute(
            "SELECT context_json FROM table_passages WHERE passage_id=?", (result.passage_id,)
        ).fetchone()
        contexts = json.loads(row[0])
        contexts[0]["source_unit_start_id"] = "not-owned"
        connection.execute(
            "UPDATE table_passages SET context_json=? WHERE passage_id=?",
            (json.dumps(contexts), result.passage_id),
        )
    with pytest.raises(StorageError, match="context"):
        initialize_storage(corpus.paths.data_dir)
    with sqlite3.connect(corpus.paths.database) as connection, pytest.raises(SourceLocationError):
        resolve_source_range(
            connection,
            document_id=result.document_id,
            source_unit_start_id=None,
            passage_id=result.passage_id,
        )


def test_reader_snapshot_retains_cells_when_reprocessing_publishes(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "data.csv"
    source.write_text("Name,Amount\nRoads,001200\n")
    job = corpus.ingest(str(source))
    assert job.result is not None
    snapshot = _load_eligible_document_snapshot(corpus.paths.database, include_history=False)
    candidates = search_keyword_candidates(
        corpus.paths.database, "Roads", limit=10, _snapshot=snapshot
    )
    reprocessed = corpus.run(
        enqueue_reprocessing(
            corpus.paths.database,
            [str(job.result["document_id"])],
            csv_options={"delimiter": "pipe"},
        )[0]
    )
    assert reprocessed.status == "done", reprocessed.error
    results = merge_search_candidates(
        candidates,
        (),
        database_path=corpus.paths.database,
        limit=10,
        keyword_weight=1,
        vector_weight=0,
        _snapshot=snapshot,
    )
    assert len(results) == 1 and results[0].table_evidence is not None
    assert results[0].table_evidence.focus.region.column_end == 2
    assert results[0].processing_generation_id == candidates[0].processing_generation_id
    fresh = support._keyword_results(corpus.paths.database, "Roads")
    assert fresh[0].table_evidence is not None
    assert fresh[0].table_evidence.focus.region.column_end == 1
