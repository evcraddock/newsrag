from __future__ import annotations

from dataclasses import replace

import pytest

from newsrag.tabular import (
    Cell,
    Table,
    TableError,
    TableRegion,
    build_table_passages,
    render_cells,
    validate_region,
)


def table() -> Table:
    return Table(
        table_id="sheet-1",
        sheet_index=1,
        source_type="csv",
        row_start=1,
        row_end=4,
        column_start=1,
        column_end=2,
        header_row=1,
        cells=tuple(
            Cell(row, column, value=value)
            for row, values in enumerate(
                [("Department", "Amount"), ("Roads", "001200"), ("Parks", "5"), ("", "")], 1
            )
            for column, value in enumerate(values, 1)
        ),
    )


def test_passages_separate_literal_focus_and_context() -> None:
    passages = build_table_passages((table(),))
    assert len(passages) == 2
    first = passages[0]
    assert first.focus.row_start == first.focus.row_end == 2
    assert [(item.role, item.region.row_start) for item in first.context] == [
        ("header", 1),
        ("following", 3),
    ]
    assert first.focus_text == 'A2=string:"Roads"\nB2=string:"001200"'
    assert "Department" not in first.focus_text
    assert "Department" in first.header_text
    assert "Parks" not in first.keyword_text
    assert "Parks" in first.text


def test_stripes_do_not_split_cells_or_lose_empty_positions() -> None:
    wide = Table(
        "sheet-1",
        1,
        "csv",
        1,
        1,
        1,
        130,
        cells=tuple(Cell(1, column, value="v" if column == 1 else "") for column in range(1, 131)),
    )
    passages = build_table_passages((wide,))
    assert [(p.focus.column_start, p.focus.column_end) for p in passages] == [
        (1, 64),
        (65, 128),
        (129, 130),
    ]
    assert sum(p.focus.cell_count for p in passages) == 130


def test_csv_retains_empty_stripes_and_original_neighbor_context() -> None:
    wide = Table(
        "sheet-1",
        1,
        "csv",
        1,
        2,
        1,
        65,
        cells=tuple(
            Cell(row, column, value="literal" if column == 1 else "")
            for row in (1, 2)
            for column in range(1, 66)
        ),
    )
    passages = build_table_passages((wide,))
    assert len(passages) == 4
    empty_stripe = passages[1]
    assert empty_stripe.focus.column_start == 65
    assert empty_stripe.context[0].role == "following"
    assert empty_stripe.context[0].text == 'BM2=string:""'


def test_context_omitted_whole_with_reason() -> None:
    original = table()
    large = replace(
        original,
        cells=tuple(
            replace(cell, value="x" * 8000) if cell.row == 1 else cell for cell in original.cells
        ),
    )
    passages = build_table_passages((large,), context_chars=100)
    assert not passages[0].header_text
    assert passages[0].omitted_context == ("header: context character budget",)


def test_literal_rendering_is_unambiguous() -> None:
    assert render_cells((Cell(1, 1, value=' =SUM(1,2)\n"<img>" '),)) == (
        'A1=string:" =SUM(1,2)\\n\\"<img>\\" "'
    )
    assert render_cells((Cell(1, 2, presence="blank-record", kind="blank", value=None),)) == (
        "B1=blank:null"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"row_start": True},
        {"row_start": 0},
        {"row_end": 5},
        {"column_end": 3},
        {"table_id": "sheet-2"},
        {"sheet_index": 2},
        {"row_start": 3, "row_end": 2},
        {"region_kind": "table"},
    ],
)
def test_invalid_selectors_are_rejected(change: dict[str, object]) -> None:
    values: dict[str, object] = {
        "table_id": "sheet-1",
        "sheet_index": 1,
        "region_kind": "cells",
        "row_start": 2,
        "row_end": 2,
        "column_start": 1,
        "column_end": 2,
    }
    values.update(change)
    with pytest.raises(TableError):
        validate_region(table(), TableRegion.from_dict(values))


def test_selector_limits_and_hidden_cells_fail_closed() -> None:
    original = table()
    region = TableRegion("sheet-1", 1, "cells", 2, 2, 1, 2)
    hidden = replace(
        original,
        cells=tuple(
            replace(cell, visible=False) if cell.row == 2 else cell for cell in original.cells
        ),
    )
    with pytest.raises(TableError, match="hidden"):
        validate_region(hidden, region)
    with pytest.raises(TableError, match="cell budget"):
        validate_region(original, region, max_cells=1)


def test_cell_serialization_and_geometry_budgets_fail_not_truncate() -> None:
    with pytest.raises(TableError, match="focus"):
        build_table_passages(
            (
                replace(
                    table(),
                    cells=tuple(
                        replace(cell, value="\\" * 8192) if cell.row == 2 else cell
                        for cell in table().cells
                    ),
                ),
            )
        )
    with pytest.raises(TableError, match="duplicate|rectangle"):
        replace(table(), cells=table().cells + (Cell(2, 1, value="duplicate"),)).validate()
