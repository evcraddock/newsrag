from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import test_text_ingestion as text_support
from test_xlsx_adapter import extract, package_with_parts, text_cell, worksheet
from xlsx_fixtures import REL_NS, XLSX_NS, make_xlsx

from newsrag.adapters import AdapterError


@pytest.mark.parametrize("formula_kind", ["normal", "shared", "array"])
@pytest.mark.parametrize("cache", ["missing", "empty-number", "empty-string"])
def test_formula_cache_presence_is_independent_of_blank_normalization(
    tmp_path: Path, formula_kind: str, cache: str
) -> None:
    kind = ' t="str"' if cache == "empty-string" else ""
    value = "" if cache == "missing" else "<v/>"
    if formula_kind == "normal":
        body = (
            f'<row r="1"><c r="A1"{kind}><f>1+2</f>{value}</c>'
            + text_cell("B1", "Visible")
            + "</row>"
        )
        target = (1, 1)
    elif formula_kind == "shared":
        body = (
            '<row r="1"><c r="A1"><f t="shared" si="0" ref="A1:A2">1+2</f><v>3</v></c></row>'
            + f'<row r="2"><c r="A2"{kind}><f t="shared" si="0"/>{value}</c></row>'
        )
        target = (2, 1)
    else:
        body = (
            '<row r="1"><c r="A1"><f t="array" ref="A1:B1">{1,2}</f><v>1</v></c>'
            + f'<c r="B1"{kind}>{value}</c></row>'
        )
        target = (1, 2)
    path = package_with_parts(tmp_path, body)
    if cache == "empty-number":
        with pytest.raises(AdapterError, match="numeric|number"):
            extract(path)
        return
    cell = next(cell for cell in extract(path).tables[0].cells if (cell.row, cell.column) == target)
    assert cell.metadata["formula"]["cache_present"] == (cache == "empty-string")
    assert cell.kind == ("string" if cache == "empty-string" else "unavailable")
    assert cell.value == ("" if cache == "empty-string" else None)


@pytest.mark.parametrize(
    "metadata",
    [
        "<sheetPr><unknownRequiredFeature/></sheetPr>",
        '<pageMargins><c r="XFE1" t="unknown"><v>junk</v></c></pageMargins>',
        '<dimension ref="A1"><c r="A1"/></dimension>',
        "<sheetFormatPr><unknownRequiredFeature/></sheetFormatPr>",
        '<cols><col min="1" max="1"><c r="XFE1"/></col></cols>',
        '<sheetViews><sheetView workbookViewId="0"><pane><c r="XFE1"/></pane></sheetView></sheetViews>',
        '<sheetViews><sheetView workbookViewId="0"><selection activeCell="XFE1"/></sheetView></sheetViews>',
        '<sheetViews><sheetView workbookViewId="0" zoomScale="invalid"/></sheetViews>',
        '<pageMargins left="invalid"/>',
        '<pageMargins unknownRequiredAttribute="1"/>',
        '<sheetPr><outlinePr summaryBelow="invalid"/></sheetPr>',
        "<sheetPr><outlinePr/><outlinePr/></sheetPr>",
        '<sheetFormatPr defaultRowHeight="15" zeroHeight="1"/>',
        '<autoFilter ref="A1"><filterColumn colId="0"><unknownRequiredFeature/></filterColumn></autoFilter>',
    ],
)
def test_allowed_metadata_subtrees_do_not_hide_malformed_descendants(
    tmp_path: Path, metadata: str
) -> None:
    path = package_with_parts(
        tmp_path, '<row r="1">' + text_cell("A1", "Visible") + "</row>", before=metadata
    )
    with pytest.raises(AdapterError, match="XLSX"):
        extract(path)


def test_hidden_sheet_malformed_metadata_fails_entire_candidate_atomically(tmp_path: Path) -> None:
    corpus = text_support._corpus(tmp_path)
    source = make_xlsx(tmp_path / "valid.xlsx")
    assert corpus.ingest(str(source)).status == "done"
    with sqlite3.connect(corpus.paths.database) as connection:
        before = connection.execute("SELECT id FROM documents").fetchall()
    workbook = f'<workbook xmlns="{XLSX_NS}" xmlns:r="{REL_NS}"><sheets><sheet name="Good" sheetId="1" r:id="rId1"/><sheet name="Hidden" sheetId="2" state="hidden" r:id="rId2"/></sheets></workbook>'
    bad = make_xlsx(
        tmp_path / "bad.xlsx",
        workbook=workbook,
        sheets={
            "xl/worksheets/sheet1.xml": worksheet(
                '<row r="1">' + text_cell("A1", "Candidate") + "</row>"
            ),
            "xl/worksheets/sheet2.xml": worksheet(
                "", before='<pageMargins><c r="XFE1" t="unknown"><v>junk</v></c></pageMargins>'
            ),
        },
    )
    failed = corpus.ingest(str(bad))
    assert failed.status == "failed"
    assert "XLSX" in str(failed.error)
    with sqlite3.connect(corpus.paths.database) as connection:
        assert connection.execute("SELECT id FROM documents").fetchall() == before
    assert text_support._keyword_results(corpus.paths.database, "Candidate") == []


def test_supported_nested_display_metadata_remains_usable_without_interpretation(
    tmp_path: Path,
) -> None:
    before = (
        '<sheetPr><tabColor rgb="FF0000FF"/><outlinePr summaryBelow="1"/><pageSetUpPr fitToPage="1"/></sheetPr>'
        '<dimension ref="A1:B2"/><sheetViews><sheetView workbookViewId="0" showGridLines="true">'
        '<pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/>'
        '<selection pane="bottomLeft" activeCell="A2" sqref="A2:B2"/></sheetView></sheetViews>'
        '<sheetFormatPr defaultRowHeight="15" baseColWidth="8"/>'
        '<cols><col min="1" max="2" width="12" customWidth="1"/></cols>'
    )
    after = (
        '<autoFilter ref="A1:B2"><filterColumn colId="0"><filters blank="0"><filter val="Metadata"/></filters></filterColumn>'
        '<sortState ref="A1:B2"><sortCondition ref="A2:A2"/></sortState></autoFilter>'
        '<printOptions gridLines="true"/><pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
        '<pageSetup orientation="landscape" fitToWidth="1" fitToHeight="0"/>'
        '<headerFooter differentOddEven="0"><oddHeader>&amp;LHeader</oddHeader></headerFooter>'
        '<rowBreaks count="1" manualBreakCount="1"><brk id="1" min="0" max="16383" man="1"/></rowBreaks>'
        '<ignoredErrors><ignoredError sqref="A1:B2" numberStoredAsText="1"/></ignoredErrors>'
    )
    styles = (
        f'<styleSheet xmlns="{XLSX_NS}"><fonts count="1"><font><name val="Calibri"/><family val="2"/><sz val="11"/><color theme="1"/><scheme val="minor"/></font></fonts>'
        '<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>'
        '<borders count="1"><border><left style="thin"><color rgb="FF000000"/></left><right/><top/><bottom/><diagonal/></border></borders>'
        '<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>'
        '<cellXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"><alignment horizontal="left" wrapText="1"/><protection locked="1"/></xf></cellXfs>'
        '<cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles><tableStyles count="0" defaultTableStyle="TableStyleMedium9"/></styleSheet>'
    )
    path = package_with_parts(
        tmp_path,
        '<row r="1" ht="15" customHeight="1" s="0">'
        + text_cell("A1", "Metadata")
        + '<c r="B1" s="0"><v>0012.50</v></c></row><row r="2">'
        + text_cell("A2", "Visible")
        + "</row>",
        before=before,
        after=after,
        styles=styles,
    )
    result = extract(path)
    assert result.tables[0].cells[1].value == "0012.50"
    assert result.tables[0].cells[1].metadata["style"]["index"] == 0
    assert "Visible" in result.units[1].normalized_text
