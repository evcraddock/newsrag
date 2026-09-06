"""Shared XLSX evidence contract tests, deliberately independent of the XLSX parser."""

from __future__ import annotations

import html
import sqlite3
from dataclasses import asdict, replace
from typing import Any

import pytest

from newsrag.enrichment import _resolved_to_evidence_context
from newsrag.facts import FactSource, extract_facts_from_sources
from newsrag.packets import PacketError, format_source_packet
from newsrag.search import SearchResult
from newsrag.source_locations import (
    SourceLocationError,
    resolve_source_range,
    validate_evidence_quote,
)
from newsrag.tabular import (
    Cell,
    Table,
    TableError,
    TableRegion,
    build_table_passages,
    render_cells,
    serialized,
    validate_region,
)
from newsrag.tabular_evidence import (
    TableEvidence,
    load_passage_table_evidence,
    resolve_contexts,
    validate_table_quote,
)
from newsrag.tabular_storage import (
    SCHEMA,
    persist_table_passage,
    persist_tables,
    resolve_table_region,
)


def region(row: int, end: int | None = None, left: int = 2, right: int = 3) -> TableRegion:
    return TableRegion("sheet-2", 2, "cells", row, end if end is not None else row, left, right)


def native(name: str, extent: TableRegion, *, header: int = 1) -> dict[str, Any]:
    return {
        "id": name,
        "name": name,
        "display_name": name,
        "region": extent.to_dict(),
        "header_row_count": header,
        "totals_row_count": 0,
        "column_names": [
            f"declared-{column}" for column in range(extent.column_start, extent.column_end + 1)
        ],
    }


def plane(**changes: Any) -> Table:
    table = Table(
        "sheet-2",
        2,
        "xlsx",
        4,
        9,
        2,
        6,
        cells=tuple(
            Cell(row, column, f"value-{row}-{column}")
            for row in range(4, 10)
            for column in range(2, 7)
        ),
        sheet_name='FY 2026 <img> "original"',
        metadata={
            "native_regions": [native("left", region(4, 6)), native("right", region(5, 9, 4, 5))],
            "hidden_rows": [],
            "hidden_columns": [],
            "sheet_state": "visible",
            "date_system": "1904",
        },
    )
    return replace(table, **changes)


def cached(
    row: int,
    column: int,
    *,
    kind: str = "shared",
    value: str | None = "001200.00",
    cache: bool = True,
) -> Cell:
    return Cell(
        row,
        column,
        value,
        kind="number" if value is not None else "unavailable",
        raw=value,
        metadata={
            "formula": {
                "kind": kind,
                "expression": 'WEBSERVICE("https://secret.invalid")' if row == 5 else None,
                "group_id": "group-1" if kind != "normal" else None,
                "anchor": {"row": 5, "column": column},
                "range": region(5, 6, column, column).to_dict(),
                "cache_present": cache,
                "cache_kind": "number" if cache else "unavailable",
            },
        },
    )


def stored(table: Table) -> sqlite3.Connection:
    """Minimal owned canonical storage without adapters, corpus services, or parser imports."""
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE documents(id TEXT PRIMARY KEY);
        CREATE TABLE processing_generations(id TEXT PRIMARY KEY, document_id TEXT);
        CREATE TABLE source_units(id TEXT PRIMARY KEY, document_id TEXT, processing_generation_id TEXT,
            location_type TEXT, location_json TEXT, ordinal INTEGER);
        CREATE TABLE chunks(id TEXT PRIMARY KEY, document_id TEXT, processing_generation_id TEXT);
        CREATE TABLE passages(id TEXT PRIMARY KEY, chunk_id TEXT, document_id TEXT,
            processing_generation_id TEXT, source_unit_start_id TEXT, source_unit_end_id TEXT);
        INSERT INTO documents VALUES('document');
        INSERT INTO processing_generations VALUES('generation', 'document');
    """)
    for statement in SCHEMA:
        connection.execute(statement)
    assert table.row_start is not None and table.row_end is not None
    ids = {}
    for ordinal, row in enumerate(range(table.row_start, table.row_end + 1), 1):
        ids[ordinal] = f"unit-{row}"
        location = {
            **region(row, left=table.column_start or 1, right=table.column_end or 1).to_dict(),
            "location_type": "table_row",
        }
        location.pop("region_kind")
        connection.execute(
            "INSERT INTO source_units VALUES(?, 'document', 'generation', 'table_row', ?, ?)",
            (ids[ordinal], serialized(location), ordinal),
        )
    persist_tables(
        connection,
        document_id="document",
        generation_id="generation",
        tables=(table,),
        source_unit_ids=ids,
    )
    for index, passage in enumerate(build_table_passages((table,))):
        connection.execute(
            "INSERT INTO chunks VALUES(?, 'document', 'generation')", (f"chunk-{index}",)
        )
        connection.execute(
            "INSERT INTO passages VALUES(?, ?, 'document', 'generation', ?, ?)",
            (
                f"passage-{index}",
                f"chunk-{index}",
                f"unit-{passage.focus.row_start}",
                f"unit-{passage.focus.row_end}",
            ),
        )
        persist_table_passage(
            connection,
            document_id="document",
            generation_id="generation",
            passage_id=f"passage-{index}",
            chunk_id=f"chunk-{index}",
            passage=passage,
        )
    return connection


def test_native_headers_split_stripes_and_do_not_cross_boundaries() -> None:
    passages = build_table_passages((plane(),))
    sixth = [passage for passage in passages if passage.focus.row_start == 6]
    assert [(p.focus.column_start, p.focus.column_end) for p in sixth] == [(2, 3), (4, 5), (6, 6)]
    assert [[(c.role, c.region.row_start) for c in p.context] for p in sixth] == [
        [("header", 4), ("preceding", 5)],
        [("header", 5), ("following", 7)],
        [("preceding", 5), ("following", 7)],
    ]
    assert not any(p.focus.row_start == 4 and p.focus.column_start in {2, 3} for p in passages)
    assert not any(p.focus.row_start == 5 and p.focus.column_start in {4, 5} for p in passages)
    assert "declared-" not in "\n".join(p.text for p in passages)


def test_sheet_header_override_takes_precedence_without_erasing_native_boundaries() -> None:
    passages = build_table_passages((plane(header_row=4),))
    assert all(p.focus.row_start != 4 for p in passages)
    sixth = [p for p in passages if p.focus.row_start == 6]
    assert [(p.focus.column_start, p.focus.column_end) for p in sixth] == [(2, 3), (4, 5), (6, 6)]
    assert all(p.context[0].role == "header" and p.context[0].region.row_start == 4 for p in sixth)


def test_hidden_gaps_never_leak_or_skip_to_a_convenient_neighbor() -> None:
    original = plane()
    hidden = replace(
        original,
        cells=tuple(
            replace(c, value="HIDDEN-SECRET", visible=False) if c.row == 7 or c.column == 3 else c
            for c in original.cells
        ),
        metadata={**original.metadata, "hidden_rows": [[7, 7]], "hidden_columns": [[3, 3]]},
    )
    passages = build_table_passages((hidden,))
    assert all(
        p.focus.row_start != 7 and not p.focus.column_start <= 3 <= p.focus.column_end
        for p in passages
    )
    assert all("HIDDEN-SECRET" not in p.text for p in passages)
    assert all(
        not any(c.role == "following" for c in p.context)
        for p in passages
        if p.focus.row_start == 6
    )
    assert all(
        not any(c.role == "preceding" for c in p.context)
        for p in passages
        if p.focus.row_start == 8
    )
    assert "HIDDEN-SECRET" not in render_cells(hidden.cells, table=hidden)
    with pytest.raises(TableError, match="hidden"):
        validate_region(hidden, region(7))
    # Descriptor exclusion remains authoritative even with inconsistent per-cell flags.
    with pytest.raises(TableError, match="hidden"):
        validate_region(replace(hidden, cells=original.cells), region(7))
    with stored(hidden) as connection:
        focus = resolve_table_region(
            connection,
            document_id="document",
            generation_id="generation",
            region=region(6, left=2, right=2),
        )
        assert "rows=1; columns=1" in focus.annotations[0]
        assert "selected region affected=no" in focus.annotations[0]
        assert (
            "HIDDEN-SECRET"
            not in connection.execute(
                "SELECT group_concat(focus || header) FROM table_values_fts"
            ).fetchone()[0]
        )


@pytest.mark.parametrize("state", ["hidden", "veryHidden"])
def test_hidden_sheet_state_rejects_new_evidence(state: str) -> None:
    original = plane()
    hidden = replace(original, metadata={**original.metadata, "sheet_state": state})
    with pytest.raises(TableError, match="no searchable"):
        build_table_passages((hidden,))
    with pytest.raises(TableError, match="hidden"):
        validate_region(hidden, region(5))
    assert render_cells(hidden.cells, table=hidden) == ""


@pytest.mark.parametrize(
    "kind,value", [("blank", None), ("error", "#DIV/0!"), ("unavailable", None), ("string", "")]
)
def test_empty_error_and_unavailable_stripes_do_not_create_hits(
    kind: str, value: str | None
) -> None:
    table = Table(
        "sheet-2",
        2,
        "xlsx",
        5,
        5,
        1,
        130,
        cells=tuple(
            Cell(5, c, "literal" if c == 1 else value, kind="string" if c == 1 else kind)
            for c in range(1, 131)
        ),
    )
    passages = build_table_passages((table,))
    assert [(p.focus.column_start, p.focus.column_end) for p in passages] == [(1, 64)]
    assert passages[0].keyword_text == "literal"


@pytest.mark.parametrize("kind", ["normal", "shared", "array"])
def test_cache_qualification_and_safe_group_provenance_round_trip(kind: str) -> None:
    original = plane()
    table = replace(
        original,
        cells=tuple(
            cached(
                c.row,
                c.column,
                kind=kind,
                value="001200.00" if c.row == 5 else None,
                cache=c.row == 5,
            )
            if c.column == 2 and c.row in {5, 6}
            else c
            for c in original.cells
        ),
        metadata={
            **original.metadata,
            "formula_groups": [
                {
                    "kind": kind,
                    "group_id": "group-1",
                    "anchor": {"row": 5, "column": 2},
                    "expression": "PRIVATE-ANCHOR-EXPRESSION",
                    "region": region(5, 6, 2, 2).to_dict(),
                }
            ],
        },
    )
    passages = build_table_passages((table,))
    fifth = next(p for p in passages if p.focus.row_start == 5 and p.focus.column_start == 2)
    assert "formula-cache; freshness not verified" in fifth.text
    assert "formula-cache unavailable" in fifth.text
    assert "WEBSERVICE" not in fifth.text and "PRIVATE-ANCHOR" not in fifth.text
    with stored(table) as connection:
        focus = resolve_table_region(
            connection,
            document_id="document",
            generation_id="generation",
            region=region(5, left=2, right=2),
        )
        assert focus.cells[0].value == "001200.00"
        assert "B5: formula-cache; freshness not verified" in focus.qualifications
        assert "expression" not in focus.cells[0].metadata["formula"]
        assert focus.cells[0].metadata["formula"]["anchor"] == {"row": 5, "column": 2}
        assert (
            "WEBSERVICE"
            in connection.execute(
                "SELECT cell_json FROM table_cells WHERE row_number=5 AND column_number=2"
            ).fetchone()[0]
        )
        assert (
            "PRIVATE-ANCHOR"
            in connection.execute("SELECT descriptor_json FROM source_tables").fetchone()[0]
        )
        indexed = connection.execute(
            "SELECT group_concat(focus || header) FROM table_values_fts"
        ).fetchone()[0]
        assert "001200.00" in indexed and "WEBSERVICE" not in indexed and "freshness" not in indexed
        follower = resolve_table_region(
            connection,
            document_id="document",
            generation_id="generation",
            region=region(6, left=2, right=2),
        )
        assert follower.cells[0].value is None and "001200" not in follower.text
        resolved = resolve_source_range(
            connection,
            document_id="document",
            source_unit_start_id="unit-5",
            table_region=focus.region,
        )
        validate_evidence_quote(resolved, "001200.00")
        with pytest.raises(SourceLocationError):
            validate_evidence_quote(resolved, "1200")
        prompt = _resolved_to_evidence_context(resolved)
        assert "freshness not verified" in prompt.text and "WEBSERVICE" not in serialized(
            asdict(prompt)
        )
        facts = extract_facts_from_sources(
            [
                FactSource(
                    document_id="document",
                    source_unit_start_id="unit-5",
                    source_unit_end_id="unit-5",
                    text=focus.text,
                    table_evidence=TableEvidence(focus),
                )
            ]
        )
        assert len(facts) == 1 and facts[0].item_type == "table_values"
        assert "freshness not verified" in facts[0].summary


def merged_plane() -> Table:
    original = plane(header_row=4)
    return replace(
        original,
        cells=tuple(
            replace(c, value=None, kind="merged-covered", presence="merged-covered")
            if c.column == 3 and c.row in {4, 5}
            else c
            for c in original.cells
        ),
        metadata={**original.metadata, "merges": [region(4).to_dict(), region(5).to_dict()]},
    )


def test_merge_anchor_disclosure_without_header_or_value_expansion() -> None:
    table = merged_plane()
    passage = next(
        p
        for p in build_table_passages((table,))
        if p.focus.row_start == 5 and p.focus.column_start == 2
    )
    assert 'B5=string:"value-5-2" [merged anchor: cells B5:C5]' in passage.focus_text
    assert "C5=merged-covered:null" in passage.focus_text
    assert 'B4=string:"value-4-2" [merged anchor: cells B4:C4]' in passage.header_text
    assert "C4=merged-covered:null" in passage.header_text
    assert passage.keyword_text.count("value-4-2") == 1
    with stored(table) as connection:
        focus = resolve_table_region(
            connection,
            document_id="document",
            generation_id="generation",
            region=region(5, left=3, right=3),
        )
        assert (
            focus.text
            == "C5=merged-covered:null [covered by merge anchor: B5; no independent value]"
        )
        assert focus.cells[0].metadata["merge_anchor"] == {"row": 5, "column": 2}
        with pytest.raises(TableError):
            validate_table_quote(TableEvidence(focus), "value-5-2")
        anchor = resolve_table_region(
            connection,
            document_id="document",
            generation_id="generation",
            region=region(5, left=2, right=2),
        )
        contexts = resolve_contexts(
            connection, focus, [{"role": "merge-anchor", **anchor.reference()}]
        )
        assert "merged anchor: cells B5:C5" in TableEvidence(focus, contexts).text
        with pytest.raises(TableError, match="Duplicate"):
            resolve_contexts(
                connection, focus, [{"role": "merge-anchor", **anchor.reference()}] * 2
            )
        with pytest.raises(TableError, match="Merge-anchor"):
            resolve_contexts(connection, anchor, [{"role": "merge-anchor", **anchor.reference()}])


def test_hidden_merge_anchor_cannot_supply_context_or_repeat_a_value() -> None:
    original = merged_plane()
    table = replace(
        original,
        cells=tuple(
            replace(c, visible=False, value="HIDDEN-ANCHOR") if c.column == 2 else c
            for c in original.cells
        ),
        metadata={**original.metadata, "hidden_columns": [[2, 2]]},
    )
    with stored(table) as connection:
        focus = resolve_table_region(
            connection,
            document_id="document",
            generation_id="generation",
            region=region(5, left=3, right=3),
        )
        assert "HIDDEN-ANCHOR" not in focus.text
        with pytest.raises(TableError, match="hidden"):
            resolve_contexts(
                connection,
                focus,
                [
                    {
                        "role": "merge-anchor",
                        **region(5, left=2, right=2).to_dict(),
                        "source_unit_start_id": "unit-5",
                        "source_unit_end_id": "unit-5",
                    }
                ],
            )


def test_context_validation_native_roles_and_cross_sheet_generation_ownership() -> None:
    with stored(plane()) as connection:
        focus = resolve_table_region(
            connection, document_id="document", generation_id="generation", region=region(6)
        )

        def reference(row: int, role: str) -> dict[str, Any]:
            selection = resolve_table_region(
                connection, document_id="document", generation_id="generation", region=region(row)
            )
            return {"role": role, **selection.reference()}

        assert resolve_contexts(
            connection, focus, [reference(4, "header"), reference(5, "preceding")]
        )
        for invalid in (
            [reference(7, "following")],
            [reference(5, "header")],
            [reference(4, "preceding")],
            [reference(5, "preceding"), reference(4, "header")],
        ):
            with pytest.raises(TableError):
                resolve_contexts(connection, focus, invalid)
        for document, generation, selector in [
            ("wrong", "generation", focus.region),
            ("document", "wrong", focus.region),
            ("document", "generation", replace(focus.region, sheet_index=1)),
        ]:
            with pytest.raises(TableError):
                resolve_table_region(
                    connection, document_id=document, generation_id=generation, region=selector
                )
        with pytest.raises(TableError, match="endpoints"):
            resolve_table_region(
                connection,
                document_id="document",
                generation_id="generation",
                region=focus.region,
                source_unit_start_id="unit-5",
            )
        for (passage_id,) in connection.execute("SELECT id FROM passages"):
            evidence = load_passage_table_evidence(
                connection,
                passage_id=passage_id,
                document_id="document",
                generation_id="generation",
            )
            assert evidence is not None and 'sheet 2 "FY 2026' in evidence.focus.location_label


def test_optional_context_is_omitted_whole_and_explicit_evidence_is_bounded() -> None:
    passages = build_table_passages((plane(),), context_chars=1)
    assert all(not p.context for p in passages)
    assert any(
        "context character budget" in reason for p in passages for reason in p.omitted_context
    )
    with pytest.raises(TableError, match="cell budget"):
        validate_region(plane(), region(4, 9, 2, 6), max_cells=2)


def test_packets_use_inert_sheet_qualified_snapshots_not_later_cells() -> None:
    table = merged_plane()
    with stored(table) as connection:
        selection = resolve_table_region(
            connection, document_id="document", generation_id="generation", region=region(5)
        )
        evidence = TableEvidence(selection)
        result = SearchResult(
            document_id="document",
            passage_id="passage-0",
            text=evidence.text,
            score=1.0,
            keyword_score=1.0,
            vector_score=None,
            page_start=5,
            page_end=5,
            citation=f"budget.xlsx — {selection.location_label}",
            source_type="xlsx",
            processing_generation_id="generation",
            table_evidence=evidence,
            location_label=selection.location_label,
        )
        packet = format_source_packet(query="budget", results=[result])
        assert "<img>" not in packet and "page 5" not in packet
        assert "sheet 2" in html.unescape(packet) and "cells B5:C5" in html.unescape(packet)
        assert "merged anchor" in html.unescape(packet)
        connection.execute(
            "UPDATE table_cells SET cell_json=replace(cell_json, 'value-5-2', 'LATER-VALUE')"
        )
        assert format_source_packet(query="budget", results=[result]) == packet
        with pytest.raises(PacketError, match="ownership"):
            format_source_packet(
                query="budget", results=[replace(result, processing_generation_id="other")]
            )


@pytest.mark.parametrize(
    "metadata",
    [
        {"native_regions": [native("a", region(4, 6)), native("b", region(5, 7))]},
        {"native_regions": [native("a", replace(region(4), sheet_index=1))]},
        {"native_regions": [native("a", region(4), header=True)]},
        {"hidden_rows": [[7, 6]]},
        {"hidden_columns": [[True, 3]]},
    ],
)
def test_invalid_shared_descriptor_geometry_fails_closed(metadata: dict[str, Any]) -> None:
    with pytest.raises(TableError):
        plane(metadata=metadata).validate()
