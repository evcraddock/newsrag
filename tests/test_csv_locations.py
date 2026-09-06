from __future__ import annotations

import html
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest
import test_text_ingestion as support

from newsrag.packets import format_source_packet, load_packet_source_provenance
from newsrag.source_locations import (
    SourceLocationError,
    resolve_source_range,
    validate_evidence_quote,
)
from newsrag.tabular import TableError, TableRegion
from newsrag.tabular_storage import resolve_table_region


def test_search_focus_header_and_neighbors_remain_distinct(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "expenses.csv"
    source.write_text("Department,Amount\nRoads,001200\nParks,5\n")
    job = corpus.ingest(str(source))
    assert job.status == "done", job.error
    roads = support._keyword_results(corpus.paths.database, "Roads")
    assert len(roads) == 1
    result = roads[0]
    assert result.keyword_match_role == "focus"
    assert result.citation.endswith("cells A2:B2")
    assert result.table_evidence is not None
    assert result.table_evidence.focus.region.row_start == 2
    assert [item.role for item in result.table_evidence.context] == ["header", "following"]
    headers = support._keyword_results(corpus.paths.database, "Department")
    assert len(headers) == 2 and all(
        item.keyword_match_role == "header-context" for item in headers
    )
    assert not support._keyword_results(corpus.paths.database, "A2")
    assert not support._keyword_results(corpus.paths.database, "string")
    with sqlite3.connect(corpus.paths.database) as connection:
        resolved = resolve_source_range(
            connection,
            document_id=result.document_id,
            source_unit_start_id=result.source_unit_start_id,
            passage_id=result.passage_id,
        )
        validate_evidence_quote(resolved, 'B2=string:"001200"')
        for quote in ("Parks", "Department", "001200", 'B2=string:"5"', 'A2=string:"001200"'):
            with pytest.raises(SourceLocationError):
                validate_evidence_quote(resolved, quote)
        assert result.processing_generation_id is not None
        region = TableRegion("sheet-1", 1, "cells", 2, 2, 2, 2)
        narrow = resolve_source_range(
            connection,
            document_id=result.document_id,
            source_unit_start_id=result.source_unit_start_id,
            table_region=region,
        )
        validate_evidence_quote(narrow, "001200")
        with pytest.raises(SourceLocationError):
            validate_evidence_quote(narrow, "1200")
        with pytest.raises(TableError):
            resolve_table_region(
                connection, document_id=result.document_id, generation_id="wrong", region=region
            )
        with pytest.raises(SourceLocationError):
            resolve_source_range(
                connection,
                document_id=result.document_id,
                source_unit_start_id=result.source_unit_start_id,
            )


def test_packet_preserves_literal_values_and_inert_context(tmp_path: Path) -> None:
    corpus = support._corpus(tmp_path / "corpus")
    source = tmp_path / "expenses.csv"
    source.write_text(
        'Label,Note\nRoads,"  =HYPERLINK(""https://example.invalid"",""<img>"")  "\nParks,neighbor\n'
    )
    assert corpus.ingest(str(source)).status == "done"
    results = support._keyword_results(corpus.paths.database, "Roads")
    packet = format_source_packet(
        query="Roads",
        results=results,
        source_provenance=load_packet_source_provenance(corpus.paths.database, results),
    )
    assert "<img>" not in packet and "https://" not in packet
    decoded = html.unescape(packet)
    assert "  =HYPERLINK" in decoded
    assert "following (cells A3:B3)" in decoded
    assert "source type: csv" in decoded
    assert "page 2" not in decoded
    assert results[0].processing_generation_id is not None
    assert results[0].processing_generation_id in decoded
    assert results[0].table_evidence is not None
    with sqlite3.connect(corpus.paths.database) as connection:
        focus = results[0].table_evidence.focus
        region = replace(focus.region, row_start=3, row_end=3)
        with pytest.raises(TableError, match="endpoints"):
            resolve_table_region(
                connection,
                document_id=focus.document_id,
                generation_id=focus.processing_generation_id,
                region=region,
                source_unit_start_id=focus.source_unit_start_id,
            )
