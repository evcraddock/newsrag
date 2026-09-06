from __future__ import annotations

from pathlib import Path
from xml.sax.saxutils import escape

import pytest
from xlsx_fixtures import (
    CONTENT_NS,
    MAIN_CONTENT_TYPE,
    PACKAGE_NS,
    REL_NS,
    WORKSHEET_CONTENT_TYPE,
    XLSX_NS,
    make_xlsx,
)

from newsrag.adapters import AdapterError, AdapterInput, AdapterResult
from newsrag.tabular import TableError, TableRegion, build_table_passages, validate_region
from newsrag.xlsx_adapter import XLSX_MEDIA_TYPE, XlsxSourceAdapter, normalize_xlsx_options


def worksheet(body: str, *, before: str = "", after: str = "") -> str:
    return f'<worksheet xmlns="{XLSX_NS}" xmlns:r="{REL_NS}">{before}<sheetData>{body}</sheetData>{after}</worksheet>'


def text_cell(coordinate: str, value: str) -> str:
    return f'<c r="{coordinate}" t="inlineStr"><is><t xml:space="preserve">{escape(value)}</t></is></c>'


def extract(path: Path, options: dict[str, object] | None = None) -> AdapterResult:
    return XlsxSourceAdapter().extract(
        AdapterInput(
            path, "fixture-hash", XLSX_MEDIA_TYPE, path.parent, options={"xlsx": options or {}}
        )
    )


def package_with_parts(
    tmp_path: Path,
    body: str,
    *,
    shared: str | None = None,
    styles: str | None = None,
    table: str | None = None,
    before: str = "",
    after: str = "",
) -> Path:
    payloads: dict[str, str | bytes] = {}
    types = {
        "/xl/workbook.xml": MAIN_CONTENT_TYPE,
        "/xl/worksheets/sheet1.xml": WORKSHEET_CONTENT_TYPE,
    }
    relationships = (
        f'<Relationship Id="rId1" Type="{REL_NS}/worksheet" Target="worksheets/sheet1.xml"/>'
    )
    for kind, xml, filename, content in (
        ("sharedStrings", shared, "sharedStrings.xml", "sharedStrings"),
        ("styles", styles, "styles.xml", "styles"),
    ):
        if xml is not None:
            payloads["xl/" + filename] = xml
            types["/xl/" + filename] = (
                f"application/vnd.openxmlformats-officedocument.spreadsheetml.{content}+xml"
            )
            relationships += (
                f'<Relationship Id="{kind}" Type="{REL_NS}/{kind}" Target="{filename}"/>'
            )
    if table is not None:
        payloads["xl/tables/table1.xml"] = table
        types["/xl/tables/table1.xml"] = (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
        )
        payloads["xl/worksheets/_rels/sheet1.xml.rels"] = (
            f'<Relationships xmlns="{PACKAGE_NS}"><Relationship Id="table" Type="{REL_NS}/table" Target="../tables/table1.xml"/></Relationships>'
        )
        after += '<tableParts count="1"><tablePart r:id="table"/></tableParts>'
    return make_xlsx(
        tmp_path / "book.xlsx",
        sheets={"xl/worksheets/sheet1.xml": worksheet(body, before=before, after=after)},
        extra_parts=payloads,
        content_types=f'<Types xmlns="{CONTENT_NS}"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>'
        + "".join(
            f'<Override PartName="{name}" ContentType="{content}"/>'
            for name, content in types.items()
        )
        + "</Types>",
        workbook_relationships=f'<Relationships xmlns="{PACKAGE_NS}">{relationships}</Relationships>',
    )


def test_minimal_workbook_uses_shared_canonical_plane(tmp_path: Path) -> None:
    result = extract(make_xlsx(tmp_path / "book.xlsx"))
    assert len(result.tables) == 1
    table = result.tables[0]
    assert (table.source_type, table.sheet_name, table.header_row) == ("xlsx", "Sheet1", None)
    assert table.cells[0].value == "Searchable evidence"
    assert result.units[0].ordinal == 1
    assert result.units[0].location["table_id"] == "sheet-1"


def test_exact_cells_presence_types_strings_and_date_system(tmp_path: Path) -> None:
    row = (
        '<row r="4">'
        + text_cell("B4", "0012 \n literal")
        + '<c r="C4"><v>12345678901234567890.00100</v></c><c r="D4" t="b"><v>0</v></c><c r="E4" t="d"><v>2026-06-01T12:30:00</v></c><c r="F4" t="e"><v>#N/A</v></c><c r="G4"/>'
        + text_cell("I4", "")
        + "</row>"
    )
    workbook = f'<workbook xmlns="{XLSX_NS}" xmlns:r="{REL_NS}"><workbookPr date1904="1"/><sheets><sheet name="Budget" sheetId="8" r:id="rId1"/></sheets></workbook>'
    result = extract(
        make_xlsx(
            tmp_path / "book.xlsx",
            workbook=workbook,
            sheets={
                "xl/worksheets/sheet1.xml": worksheet(
                    row, before='<dimension ref="A1:XFD1048576"/>'
                )
            },
        )
    )
    table = result.tables[0]
    assert (table.row_start, table.row_end, table.column_start, table.column_end) == (4, 4, 2, 9)
    assert table.metadata["date_system"] == "1904"
    cells = {cell.column: cell for cell in table.cells}
    assert cells[2].value == "0012 \n literal"
    assert cells[3].value == cells[3].raw == "12345678901234567890.00100"
    assert cells[4].value is False and cells[4].kind == "boolean"
    assert cells[5].value == "2026-06-01T12:30:00"
    assert cells[6].kind == "error" and not cells[6].searchable
    assert cells[7].presence == "explicit-empty" and cells[7].value is None
    assert cells[8].presence == "absent" and cells[8].value is None
    assert cells[9].presence == "stored" and cells[9].value == ""


def test_shared_rich_and_escaped_strings(tmp_path: Path) -> None:
    shared = f'<sst xmlns="{XLSX_NS}" count="2" uniqueCount="2"><si><r><t>Road</t></r><r><t>s</t></r></si><si><t>_x005F_x0041_ _xD83D__xDE00_</t></si></sst>'
    path = package_with_parts(
        tmp_path,
        '<row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row>',
        shared=shared,
    )
    cells = extract(path).tables[0].cells
    assert cells[0].value == "Roads" and cells[0].raw == "0"
    assert cells[1].value == "_x0041_ 😀"


def test_styles_never_convert_serial_dates_or_infer_units(tmp_path: Path) -> None:
    styles = f'<styleSheet xmlns="{XLSX_NS}"><numFmts count="1"><numFmt numFmtId="164" formatCode="$0.00"/></numFmts><cellXfs count="2"><xf numFmtId="14"/><xf numFmtId="164"/></cellXfs></styleSheet>'
    path = package_with_parts(
        tmp_path,
        '<row r="1"><c r="A1" s="0"><v>60</v></c><c r="B1" s="1"><v>0012.50</v></c></row>',
        styles=styles,
    )
    cells = extract(path).tables[0].cells
    assert [(cell.kind, cell.value) for cell in cells] == [("number", "60"), ("number", "0012.50")]
    assert cells[1].metadata["style"]["format_code"] == "$0.00"


def test_formula_caches_preserve_independent_presence_kind_and_expressions(tmp_path: Path) -> None:
    body = '<row r="1"><c r="A1"><f>SUM(B1:C1)</f><v>9.00</v></c><c r="B1"><f>1+2</f></c><c r="C1" t="e"><f>1/0</f><v>#DIV/0!</v></c><c r="D1" t="str"><f>IF(1,"","")</f><v/></c></row>'
    result = extract(package_with_parts(tmp_path, body))
    cells = result.tables[0].cells
    assert cells[0].value == "9.00"
    assert cells[0].metadata["formula"]["expression"] == "SUM(B1:C1)"
    assert cells[1].kind == "unavailable" and not cells[1].metadata["formula"]["cache_present"]
    assert cells[2].kind == "error" and cells[2].metadata["formula"]["cache_present"]
    assert cells[3].value == "" and cells[3].metadata["formula"]["cache_present"]
    assert "SUM(B1:C1)" not in result.units[0].normalized_text
    assert "freshness not verified" in build_table_passages(result.tables)[0].text


def test_shared_and_array_formulas_never_translate_or_inherit_caches(tmp_path: Path) -> None:
    body = '<row r="2"><c r="B2"><f t="shared" si="3" ref="B2:B3">A2*2</f><v>12</v></c><c r="D2"><f t="array" ref="D2:E3">SUM(A1:A3)</f><v>6</v></c></row><row r="3"><c r="B3"><f t="shared" si="3"/></c><c r="E3"><v>18</v></c></row>'
    table = extract(package_with_parts(tmp_path, body)).tables[0]
    cells = {(cell.row, cell.column): cell for cell in table.cells}
    assert cells[3, 2].kind == "unavailable"
    assert cells[3, 2].metadata["formula"]["expression"] is None
    assert cells[2, 5].kind == "unavailable" and cells[2, 5].presence == "absent"
    assert cells[3, 5].value == "18" and cells[3, 5].metadata["formula"]["cache_present"]
    assert len(table.metadata["formula_groups"]) == 2
    assert table.metadata["formula_groups"][0]["expression"] == "A2*2"


def test_hidden_and_empty_sheets_keep_order_rows_and_values_but_no_evidence(tmp_path: Path) -> None:
    workbook = f'<workbook xmlns="{XLSX_NS}" xmlns:r="{REL_NS}"><sheets><sheet name="Secret" sheetId="5" state="veryHidden" r:id="rId1"/><sheet name="Empty" sheetId="8" r:id="rId2"/><sheet name="Public" sheetId="9" r:id="rId3"/></sheets></workbook>'
    sheets = {
        "xl/worksheets/sheet1.xml": worksheet(
            '<row r="3">' + text_cell("B3", "HiddenSheetValue") + "</row>"
        ),
        "xl/worksheets/sheet2.xml": worksheet(""),
        "xl/worksheets/sheet3.xml": worksheet(
            '<row r="5">'
            + text_cell("B5", "PublicValue")
            + text_cell("C5", "HiddenColumnValue")
            + text_cell("D5", "OtherPublic")
            + '</row><row r="6" hidden="1">'
            + text_cell("B6", "HiddenRowValue")
            + '</row><row r="7">'
            + text_cell("B7", "AfterHidden")
            + "</row>",
            before='<cols><col min="3" max="3" hidden="1"/></cols>',
        ),
    }
    result = extract(make_xlsx(tmp_path / "book.xlsx", workbook=workbook, sheets=sheets))
    assert [table.sheet_name for table in result.tables] == ["Secret", "Empty", "Public"]
    assert result.tables[1].row_start is None
    assert [unit.ordinal for unit in result.units] == [1, 2, 3, 4]
    assert result.tables[0].cells[0].value == "HiddenSheetValue"
    all_text = "\n".join(unit.normalized_text for unit in result.units) + "\n".join(
        p.text for p in build_table_passages(result.tables)
    )
    assert "PublicValue" in all_text
    assert not any(
        value in all_text for value in ("HiddenSheetValue", "HiddenColumnValue", "HiddenRowValue")
    )
    assert result.units[0].normalized_text == result.units[2].normalized_text == ""
    with pytest.raises(TableError, match="hidden|Hidden"):
        validate_region(result.tables[2], TableRegion("sheet-3", 3, "cells", 5, 5, 3, 3))


def test_merge_retains_only_anchor_value_and_original_coordinates(tmp_path: Path) -> None:
    path = package_with_parts(
        tmp_path,
        '<row r="8">' + text_cell("B8", "Merged heading") + "</row>",
        after='<mergeCells count="1"><mergeCell ref="B8:C8"/></mergeCells>',
    )
    result = extract(path)
    assert result.tables[0].cells[1].kind == "merged-covered"
    assert result.tables[0].cells[1].value is None
    assert result.tables[0].cells[1].metadata["merge_anchor"] == {"row": 8, "column": 2}
    assert result.tables[0].metadata["merges"][0]["column_end"] == 3
    assert "merged anchor: cells B8:C8" in result.units[0].normalized_text


def test_native_header_metadata_and_explicit_override(tmp_path: Path) -> None:
    table = f'<table xmlns="{XLSX_NS}" id="1" name="Expenses" displayName="Expenses" ref="B4:C6" totalsRowCount="1"><tableColumns count="2"><tableColumn id="1" name="Department"/><tableColumn id="2" name="Amount"/></tableColumns></table>'
    body = (
        '<row r="4">'
        + text_cell("B4", "Department")
        + text_cell("C4", "Amount")
        + '</row><row r="5">'
        + text_cell("B5", "Roads")
        + '<c r="C5"><v>1200</v></c></row><row r="6">'
        + text_cell("B6", "Totals")
        + '<c r="C6"><v>1200</v></c></row>'
    )
    path = package_with_parts(tmp_path, body, table=table)
    result = extract(path)
    region = result.tables[0].metadata["native_regions"][0]
    assert region["header_row_count"] == region["totals_row_count"] == 1
    assert region["column_names"] == ["Department", "Amount"]
    assert all(p.focus.row_start != 4 for p in build_table_passages(result.tables))
    overridden = extract(path, {"header_rows": {"Sheet1": 5}})
    assert overridden.tables[0].header_row == 5
    assert (
        overridden.tables[0].metadata["native_regions"]
        == result.tables[0].metadata["native_regions"]
    )


@pytest.mark.parametrize(
    "options",
    [
        None,
        [],
        {"wrong": 1},
        {"header_rows": []},
        {"header_rows": {"Sheet1": True}},
        {"header_rows": {"Sheet1": 0}},
        {"header_rows": {"Sheet1": 1048577}},
        {"header_rows": {"bad/name": 3}},
    ],
)
def test_invalid_recipe(options: object) -> None:
    with pytest.raises(AdapterError, match="XLSX"):
        normalize_xlsx_options(options)


@pytest.mark.parametrize(
    "options", [{"header_rows": {"Missing": 1}}, {"header_rows": {"Sheet1": 2}}]
)
def test_invalid_header_reference(tmp_path: Path, options: dict[str, object]) -> None:
    with pytest.raises(AdapterError, match="header|sheet name"):
        extract(make_xlsx(tmp_path / "book.xlsx"), options)


@pytest.mark.parametrize(
    "body,error",
    [
        ('<row r="1"><c r="A1"><v>NaN</v></c></row>', "numeric"),
        ('<row r="1"><c r="A1" t="b"><v>true</v></c></row>', "boolean"),
        ('<row r="1"><c r="A1" t="d"><v>2026-02-30</v></c></row>', "date"),
        ('<row r="1"><c r="A1" t="e"><v>ERROR</v></c></row>', "error"),
        ('<row r="1"><c r="A1" t="s"><v>0</v></c></row>', "index"),
        ('<row r="1"><c r="A1" t="unknown"><v>1</v></c></row>', "type"),
        ('<row r="1"><c r="A2"><v>1</v></c></row>', "wrong row"),
        ('<row r="1"><c r="A1"><v>1</v></c><c r="A1"><v>2</v></c></row>', "duplicated"),
        ('<row r="1"><c r="XFE1"><v>1</v></c></row>', "native"),
        ('<row r="1048577"><c r="A1048577"><v>1</v></c></row>', "bounds"),
        ('<row r="1"><c r="A1" s="0"><v>1</v></c></row>', "style"),
        ('<row r="1"><c r="A1"><f t="dataTable" ref="A1:A2"/><v>1</v></c></row>', "data-table"),
        ('<row r="1"><c r="A1"><f t="shared" si="1"/><v>1</v></c></row>', "anchor"),
        (
            '<row r="1"><c r="A1"><f t="shared" si="1" ref="A1:A2">1+2</f><v>3</v></c></row>',
            "members",
        ),
        (
            '<row r="1"><c r="A1"><f t="array" ref="A1:B1">1+2</f><v>3</v></c><c r="B1"><f>7</f><v>7</v></c></row>',
            "overlaps",
        ),
        ('<row r="1"><c r="A1"><f>1+2</f><v/></c></row>', "numeric"),
        ('<row r="1"><c r="A1" t="inlineStr"><is><t>_x001B_</t></is></c></row>', "control"),
        ('<row r="1"><c r="A1"><v>1</v></c><c r="IW1"><v>2</v></c></row>', "span"),
        (
            '<row r="1"><c r="A1"><v>1</v></c></row><row r="100001"><c r="A100001"><v>2</v></c></row>',
            "span",
        ),
        (
            '<row r="1"><c r="A1"><v>1</v></c></row><row r="5000"><c r="IV5000"><v>2</v></c></row>',
            "rectangular",
        ),
    ],
    ids=[
        "nonfinite",
        "boolean",
        "date",
        "error",
        "string-index",
        "type",
        "wrong-row",
        "duplicate",
        "native-column",
        "native-row",
        "style-index",
        "data-table",
        "missing-anchor",
        "missing-follower",
        "array-overlap",
        "empty-numeric-cache",
        "escaped-control",
        "column-span",
        "row-span",
        "sparse-rectangle",
    ],
)
def test_malformed_semantics_fail(tmp_path: Path, body: str, error: str) -> None:
    with pytest.raises(AdapterError, match=error):
        extract(package_with_parts(tmp_path, body))


@pytest.mark.parametrize(
    "after,error",
    [
        ('<mergeCells count="2"><mergeCell ref="A1:B1"/></mergeCells>', "count"),
        ('<mergeCells><mergeCell ref="A1:B1"/><mergeCell ref="B1:C1"/></mergeCells>', "overlap"),
        ('<mergeCells><mergeCell ref="A1:B1"/></mergeCells>', "conflicting"),
    ],
)
def test_invalid_merges_fail(tmp_path: Path, after: str, error: str) -> None:
    body = (
        '<row r="1">'
        + text_cell("A1", "Anchor")
        + (text_cell("B1", "Conflict") if error == "conflicting" else "")
        + "</row>"
    )
    with pytest.raises(AdapterError, match=error):
        extract(package_with_parts(tmp_path, body, after=after))


@pytest.mark.parametrize(
    "body",
    [
        '<row r="1"><c r="A1"><f>1+2</f></c></row>',
        '<row r="1"><c r="A1" t="e"><v>#N/A</v></c></row>',
        '<row r="1"><c r="A1" t="str"><v/></c></row>',
        '<row r="1" hidden="1"><c r="A1"><v>2</v></c></row>',
    ],
)
def test_unavailable_error_empty_and_hidden_only_workbooks_fail(tmp_path: Path, body: str) -> None:
    with pytest.raises(AdapterError, match="searchable"):
        extract(package_with_parts(tmp_path, body))
