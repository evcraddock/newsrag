from __future__ import annotations

from pathlib import Path

import pytest
from docx_fixtures import make_docx, paragraph
from test_docx_adapter import extract

from newsrag.adapters import AdapterError
from newsrag.docx_adapter import DocxSourceAdapter


def test_canonical_metadata_is_bounded_as_well_as_text(tmp_path: Path) -> None:
    body = paragraph("A heading", '<w:pPr><w:outlineLvl w:val="0"/></w:pPr>') + paragraph("")
    with pytest.raises(AdapterError, match="text and metadata limit"):
        extract(
            make_docx(tmp_path / "metadata.docx", body), DocxSourceAdapter(max_output_chars=200)
        )


def test_deep_style_chains_are_bounded(tmp_path: Path) -> None:
    styles = "".join(
        f'<w:style w:styleId="A{i}"><w:basedOn w:val="A{i + 1}"/></w:style>' for i in range(65)
    )
    body = paragraph("Text", '<w:pPr><w:pStyle w:val="A0"/></w:pPr>')
    with pytest.raises(AdapterError, match="64-level"):
        extract(make_docx(tmp_path / "deep.docx", body, styles=styles))


def test_zero_id_normal_footnote_is_not_confused_with_a_separator(tmp_path: Path) -> None:
    body = paragraph("Reference", runs='<w:r><w:footnoteReference w:id="0"/></w:r>')
    notes = '<w:footnote w:id="0">' + paragraph("Normal note") + "</w:footnote>"
    result = extract(make_docx(tmp_path / "normal.docx", body, footnotes=notes))
    assert result.units[-1].location["footnote_id"] == "0"
    notes = '<w:footnote w:id="0" w:type="separator">' + paragraph("Separator") + "</w:footnote>"
    with pytest.raises(AdapterError, match="separator"):
        extract(make_docx(tmp_path / "separator.docx", body, footnotes=notes))


def test_table_grid_spans_advance_column_locations(tmp_path: Path) -> None:
    table = (
        '<w:tbl><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
        + paragraph("Merged")
        + "</w:tc><w:tc>"
        + paragraph("Third")
        + "</w:tc></w:tr></w:tbl>"
    )
    result = extract(make_docx(tmp_path / "merged.docx", table))
    cells = result.units[0].structure["cells"]
    assert isinstance(cells, list)
    assert [cell["column"] for cell in cells] == [1, 3]


@pytest.mark.parametrize(
    "fields",
    [
        '<w:fldSimple w:instr="PAGE"/><w:fldSimple w:instr="DDEAUTO command"/>',
        '<w:r><w:fldChar w:fldCharType="begin"/><w:instrText>PAGE</w:instrText><w:fldChar w:fldCharType="end"/></w:r><w:r><w:fldChar w:fldCharType="begin"/><w:instrText>DDEAUTO command</w:instrText><w:fldChar w:fldCharType="end"/></w:r>',
        '<w:fldSimple w:instr="MACROBUTTON RunMacro click"/>',
    ],
)
def test_active_fields_cannot_hide_after_benign_fields(tmp_path: Path, fields: str) -> None:
    with pytest.raises(AdapterError, match="active field"):
        extract(make_docx(tmp_path / "fields.docx", paragraph("Visible text", runs=fields)))


def test_heading_name_can_be_inherited_from_base_style(tmp_path: Path) -> None:
    styles = '<w:style w:styleId="Base"><w:name w:val="heading 2"/></w:style><w:style w:styleId="Child"><w:basedOn w:val="Base"/></w:style>'
    body = paragraph("Inherited", '<w:pPr><w:pStyle w:val="Child"/></w:pPr>')
    result = extract(make_docx(tmp_path / "heading.docx", body, styles=styles))
    assert result.units[0].structure["heading_level"] == 2
