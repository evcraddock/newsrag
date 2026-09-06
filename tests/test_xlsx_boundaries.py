from __future__ import annotations

from pathlib import Path

import pytest
from test_xlsx_adapter import extract, package_with_parts, text_cell, worksheet
from xlsx_fixtures import REL_NS, XLSX_NS, make_xlsx

from newsrag.adapters import AdapterError
from newsrag.xlsx_adapter import _Budget


def test_per_value_limit_is_checked_before_serialization(tmp_path: Path) -> None:
    text = "".join(chr(33 + (index * 37) % 90) for index in range(8193))
    path = package_with_parts(tmp_path, '<row r="1">' + text_cell("A1", text) + "</row>")
    with pytest.raises(AdapterError, match="8192"):
        extract(path)


@pytest.mark.parametrize(
    "setting,value,error",
    [
        ("MAX_TOTAL_VALUE_CHARS", 5, "value/formula"),
        ("MAX_METADATA_BYTES", 32, "metadata"),
        ("MAX_ROWS", 0, "row/rectangular"),
        ("MAX_CELLS", 0, "row/rectangular"),
    ],
)
def test_workbook_budget_paths_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, setting: str, value: int, error: str
) -> None:
    monkeypatch.setattr("newsrag.xlsx_adapter." + setting, value)
    with pytest.raises(AdapterError, match=error):
        extract(make_xlsx(tmp_path / "book.xlsx"))


def test_cumulative_workbook_geometry_includes_hidden_positions() -> None:
    budget = _Budget()
    budget.geometry((1, 100_000, 1, 5))
    budget.geometry((1, 100_000, 1, 5))
    with pytest.raises(AdapterError, match="row/rectangular"):
        budget.geometry((1, 1, 1, 1))


def test_unreferenced_shared_strings_still_consume_value_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared = f'<sst xmlns="{XLSX_NS}"><si><t>UnusedHugeValue</t></si></sst>'
    path = package_with_parts(
        tmp_path, '<row r="1">' + text_cell("A1", "Visible") + "</row>", shared=shared
    )
    monkeypatch.setattr("newsrag.xlsx_adapter.MAX_TOTAL_VALUE_CHARS", 10)
    with pytest.raises(AdapterError, match="value/formula"):
        extract(path)


def test_formula_expressions_consume_value_budget_even_without_caches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = package_with_parts(
        tmp_path,
        '<row r="1"><c r="A1"><f>SUM(B1:Z99999)</f></c>' + text_cell("B1", "Visible") + "</row>",
    )
    monkeypatch.setattr("newsrag.xlsx_adapter.MAX_TOTAL_VALUE_CHARS", 10)
    with pytest.raises(AdapterError, match="value/formula"):
        extract(path)


def test_all_sheets_count_including_empty_and_hidden(tmp_path: Path) -> None:
    sheets = {f"xl/worksheets/sheet{index}.xml": worksheet("") for index in range(1, 34)}
    with pytest.raises(AdapterError, match="32"):
        extract(make_xlsx(tmp_path / "book.xlsx", sheets=sheets))


def test_native_table_and_merge_limits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = package_with_parts(
        tmp_path,
        '<row r="1">' + text_cell("A1", "Visible") + "</row>",
        after='<mergeCells><mergeCell ref="A1:B1"/></mergeCells>',
    )
    monkeypatch.setattr("newsrag.xlsx_adapter.MAX_MERGES", 0)
    with pytest.raises(AdapterError, match="merge"):
        extract(path)
    table = f'<table xmlns="{XLSX_NS}" id="1" name="Data" displayName="Data" ref="A1:A2"><tableColumns count="1"><tableColumn id="1" name="Header"/></tableColumns></table>'
    path = package_with_parts(
        tmp_path,
        '<row r="1">'
        + text_cell("A1", "Header")
        + '</row><row r="2">'
        + text_cell("A2", "Visible")
        + "</row>",
        table=table,
    )
    monkeypatch.setattr("newsrag.xlsx_adapter.MAX_NATIVE_TABLES", 0)
    with pytest.raises(AdapterError, match="native-table"):
        extract(path)


@pytest.mark.parametrize(
    "sheets,error",
    [
        (
            '<sheet name="Same" sheetId="1" r:id="rId1"/><sheet name="same" sheetId="2" r:id="rId2"/>',
            "duplicate",
        ),
        (
            '<sheet name="One" sheetId="1" r:id="rId1"/><sheet name="Two" sheetId="1" r:id="rId2"/>',
            "duplicate",
        ),
        ('<sheet name="Bad/Name" sheetId="1" r:id="rId1"/>', "name"),
        ('<sheet name="Sheet1" sheetId="1" state="invisible" r:id="rId1"/>', "visibility"),
    ],
)
def test_invalid_workbook_sheet_descriptors(tmp_path: Path, sheets: str, error: str) -> None:
    workbook = (
        f'<workbook xmlns="{XLSX_NS}" xmlns:r="{REL_NS}"><sheets>{sheets}</sheets></workbook>'
    )
    parts = {
        "xl/worksheets/sheet1.xml": worksheet('<row r="1">' + text_cell("A1", "Value") + "</row>"),
    }
    if 'r:id="rId2"' in sheets:
        parts["xl/worksheets/sheet2.xml"] = worksheet("")
    with pytest.raises(AdapterError, match=error):
        extract(make_xlsx(tmp_path / "book.xlsx", workbook=workbook, sheets=parts))


@pytest.mark.parametrize(
    "styles,error",
    [
        ('<cellXfs count="2"><xf/></cellXfs>', "count"),
        ('<cellXfs><xf numFmtId="164"/></cellXfs>', "undefined"),
        ('<cellXfs><xf fontId="1"/></cellXfs>', "fontId"),
        ('<cellStyles><cellStyle name="Normal" xfId="2"/></cellStyles>', "xfId"),
        (
            '<numFmts><numFmt numFmtId="164" formatCode="0"/><numFmt numFmtId="164" formatCode="0.00"/></numFmts>',
            "duplicate",
        ),
        (
            '<tableStyles><tableStyle name="Custom" count="1"><tableStyleElement type="wholeTable" dxfId="2"/></tableStyle></tableStyles>',
            "differential",
        ),
    ],
)
def test_invalid_style_definitions(tmp_path: Path, styles: str, error: str) -> None:
    path = package_with_parts(
        tmp_path,
        '<row r="1">' + text_cell("A1", "Value") + "</row>",
        styles=f'<styleSheet xmlns="{XLSX_NS}">{styles}</styleSheet>',
    )
    with pytest.raises(AdapterError, match=error):
        extract(path)


@pytest.mark.parametrize(
    "body,before,error",
    [
        ('<row r="1">' + text_cell("A1", "Value") + "</row>", '<dimension ref="XFE1"/>', "native"),
        (
            '<row r="1">' + text_cell("A1", "Value") + "</row>",
            '<cols><col min="1" max="2"/><col min="2" max="3"/></cols>',
            "overlap",
        ),
        ('<row r="1" hidden="yes">' + text_cell("A1", "Value") + "</row>", "", "boolean"),
        ('<row r="1">' + text_cell("A1", "_xD800_") + "</row>", "", "surrogate"),
        ('<row r="1">discarded text' + text_cell("A1", "Value") + "</row>", "", "mixed text"),
    ],
)
def test_nonvalue_structure_is_validated(
    tmp_path: Path, body: str, before: str, error: str
) -> None:
    with pytest.raises(AdapterError, match=error):
        extract(package_with_parts(tmp_path, body, before=before))


def test_formula_missing_cache_is_a_stored_cell_not_an_empty_cell(tmp_path: Path) -> None:
    result = extract(
        package_with_parts(
            tmp_path, '<row r="1"><c r="A1"><f>1+2</f></c>' + text_cell("B1", "Value") + "</row>"
        )
    )
    assert result.tables[0].cells[0].presence == "stored"
    assert result.tables[0].cells[0].kind == "unavailable"


def test_explicit_empty_sheet_header_is_rejected(tmp_path: Path) -> None:
    path = make_xlsx(tmp_path / "book.xlsx", sheets={"xl/worksheets/sheet1.xml": worksheet("")})
    with pytest.raises(AdapterError, match="empty sheet"):
        extract(path, {"header_rows": {"Sheet1": 1}})
