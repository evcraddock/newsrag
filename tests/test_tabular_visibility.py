from __future__ import annotations

from pathlib import Path

import pytest
from test_xlsx_adapter import extract, package_with_parts, text_cell

from newsrag.tabular import (
    Cell,
    Table,
    TableError,
    TableRegion,
    evidence_cells,
    interval_overlaps,
    validate_region,
)


@pytest.mark.parametrize(
    "start,end,expected",
    [
        (1, 1, False),
        (2, 2, True),
        (3, 3, True),
        (4, 4, False),
        (4, 6, True),
        (10, 10, True),
        (11, 20, False),
    ],
)
def test_ordered_interval_lookup(start: int, end: int, expected: bool) -> None:
    assert interval_overlaps([[2, 3], [6, 10]], start, end) is expected
    assert not interval_overlaps([], start, end)


def test_many_hidden_intervals_keep_source_coordinate_gaps() -> None:
    intervals = [[row, row] for row in range(2, 100_001, 2)]
    for row in range(99_900, 100_001):
        assert interval_overlaps(intervals, row, row) == (row % 2 == 0)


def test_corrupted_hidden_interval_order_cannot_bypass_evidence_exclusion() -> None:
    table = Table(
        "sheet-1",
        1,
        "xlsx",
        1,
        3,
        1,
        1,
        cells=(Cell(2, 1, value="hidden"),),
        metadata={"hidden_rows": [[3, 3], [2, 2]]},
    )
    with pytest.raises(TableError, match="Hidden coordinate intervals"):
        validate_region(table, TableRegion("sheet-1", 1, "cells", 2, 2, 1, 1))


def test_normalized_rows_project_merge_descriptors_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = 0

    def project(table: Table, cells: tuple[Cell, ...]) -> tuple[Cell, ...]:
        nonlocal calls
        calls += 1
        return evidence_cells(table, cells)

    monkeypatch.setattr("newsrag.xlsx_adapter.evidence_cells", project)
    path = package_with_parts(
        tmp_path,
        '<row r="1">'
        + text_cell("A1", "Anchor")
        + '</row><row r="2">'
        + text_cell("A2", "Other")
        + "</row>",
        after='<mergeCells><mergeCell ref="A1:B1"/></mergeCells>',
    )
    result = extract(path)
    assert calls == 1
    assert "merged anchor: cells A1:B1" in result.units[0].normalized_text
    assert "Other" in result.units[1].normalized_text
