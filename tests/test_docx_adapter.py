from __future__ import annotations

from pathlib import Path

import pytest
from docx_fixtures import REL_NS, make_docx, paragraph

from newsrag.adapters import AdapterError, AdapterInput, AdapterResult
from newsrag.docx_adapter import DocxSourceAdapter
from newsrag.sources import DOCX_MEDIA_TYPE


def extract(path: Path, adapter: DocxSourceAdapter | None = None) -> AdapterResult:
    return (adapter or DocxSourceAdapter()).extract(
        AdapterInput(path, "hash", DOCX_MEDIA_TYPE, path.parent)
    )


def test_headings_lists_tables_footnotes_and_order_have_stable_locations(tmp_path: Path) -> None:
    path = tmp_path / "minutes.docx"
    body = (
        paragraph("Council", '<w:pPr><w:pStyle w:val="Heading1"/></w:pPr>')
        + paragraph("Budget approved.", runs='<w:r><w:footnoteReference w:id="2"/></w:r>')
        + paragraph(
            "Repair roads",
            '<w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr>',
        )
        + "<w:tbl><w:tr><w:tc>"
        + paragraph("Item")
        + "</w:tc><w:tc>"
        + paragraph("Cost")
        + '</w:tc></w:tr><w:tr><w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr>'
        + paragraph("Roads $15,000")
        + "</w:tc></w:tr></w:tbl>"
        + paragraph("", runs='<w:r><w:footnoteReference w:id="2"/></w:r>')
    )
    styles = '<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>'
    numbering = '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum><w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>'
    footnotes = '<w:footnote w:id="2">' + paragraph("Original supporting note.") + "</w:footnote>"
    make_docx(path, body, styles=styles, numbering=numbering, footnotes=footnotes)
    result = extract(path)
    units = result.units
    assert [u.normalized_text for u in units] == [
        "Council",
        "Budget approved. [footnote ID 2]",
        "Original supporting note.",
        "Repair roads",
        "Item\tCost",
        "Roads $15,000",
        " [footnote ID 2]",
    ]
    assert [u.ordinal for u in units] == list(range(1, 8))
    assert all(u.location_type == "docx_block" for u in units)
    assert all(u.location["block_number"] == u.ordinal for u in units)
    assert units[0].structure["kind"] == "heading"
    assert units[2].location == {"block_number": 3, "footnote_id": "2", "paragraph_number": 1}
    assert units[3].structure["kind"] == "list_item"
    assert units[3].structure["numbering_format"] == "decimal"
    assert units[4].human_label == "Council — table 1, row 1"
    assert units[5].structure["cells"] == [{"column": 1, "grid_span": 2, "vertical_merge": None}]
    assert units[-1].location["paragraph_number"] == 4
    assert result.metadata_candidates == {}
    assert result.derived_artifact_path is None
    assert extract(path) == result


def test_external_hyperlink_text_is_retained_but_images_and_fields_not_executed(
    tmp_path: Path,
) -> None:
    body = paragraph(
        "Read ",
        runs='<w:hyperlink r:id="link"><w:r><w:t>public record</w:t></w:r></w:hyperlink><w:r><w:drawing><w:t>not image OCR</w:t></w:drawing></w:r><w:r><w:instrText>PAGE</w:instrText><w:t>1</w:t></w:r>',
    )
    path = make_docx(
        tmp_path / "link.docx",
        body,
        relationships=f'<Relationship Id="link" Type="{REL_NS}/hyperlink" Target="https://example.gov/private" TargetMode="External"/>',
    )
    assert extract(path).units[0].normalized_text == "Read public record1"


def test_paragraph_spacing_tabs_breaks_and_final_revision_view(tmp_path: Path) -> None:
    path = make_docx(
        tmp_path / "runs.docx",
        paragraph(
            "  first ",
            runs="<w:r><w:tab/><w:t>second</w:t><w:br/><w:t>third</w:t></w:r><w:del><w:r><w:delText>deleted</w:delText></w:r></w:del><w:ins><w:r><w:t> inserted</w:t></w:r></w:ins>",
        )
        + paragraph(""),
    )
    units = extract(path).units
    assert [u.normalized_text for u in units] == ["  first \tsecond\nthird inserted", ""]
    assert units[1].location["paragraph_number"] == 2


def test_inherited_heading_and_numbering_styles(tmp_path: Path) -> None:
    styles = '<w:style w:styleId="Base"><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style><w:style w:styleId="Child"><w:basedOn w:val="Base"/></w:style>'
    body = paragraph("Inherited heading", '<w:pPr><w:pStyle w:val="Child"/></w:pPr>') + paragraph(
        "Text"
    )
    units = extract(make_docx(tmp_path / "styles.docx", body, styles=styles)).units
    assert units[0].structure["heading_level"] == 2
    assert units[1].structure["heading_path"] == ["Inherited heading"]


def test_nested_tables_remain_ordered_inside_the_cited_outer_cell(tmp_path: Path) -> None:
    body = (
        "<w:tbl><w:tr><w:tc>"
        + paragraph("Before")
        + "<w:tbl><w:tr><w:tc>"
        + paragraph("Nested")
        + "</w:tc></w:tr></w:tbl>"
        + paragraph("After")
        + "</w:tc></w:tr></w:tbl>"
    )
    units = extract(make_docx(tmp_path / "table.docx", body)).units
    assert len(units) == 1
    assert units[0].normalized_text == "Before\nNested\nAfter"
    assert units[0].location["table_number"] == 1


@pytest.mark.parametrize(
    "body,error",
    [
        (paragraph(""), "no text evidence"),
        (paragraph("", runs="<w:r><w:drawing/></w:r>"), "image OCR"),
        (paragraph("Text", runs='<w:r><w:footnoteReference w:id="9"/></w:r>'), "missing footnote"),
        (paragraph("Text", '<w:pPr><w:numPr><w:numId w:val="3"/></w:numPr></w:pPr>'), "numbering"),
        (paragraph("Text", '<w:pPr><w:outlineLvl w:val="40"/></w:pPr>'), "outline level"),
        (paragraph("Text", runs='<w:r><w:endnoteReference w:id="1"/></w:r>'), "endnote"),
        ("<w:tbl/>", "no rows"),
        (paragraph("unsafe\u202econtrol"), "control characters"),
    ],
)
def test_invalid_or_unsupported_document_content_fails(
    tmp_path: Path, body: str, error: str
) -> None:
    with pytest.raises(AdapterError, match=error):
        extract(make_docx(tmp_path / "invalid.docx", body))


def test_style_cycles_fail_without_partial_output(tmp_path: Path) -> None:
    styles = '<w:style w:styleId="A"><w:basedOn w:val="B"/></w:style><w:style w:styleId="B"><w:basedOn w:val="A"/></w:style>'
    path = make_docx(
        tmp_path / "cycle.docx",
        paragraph("Text", '<w:pPr><w:pStyle w:val="A"/></w:pPr>'),
        styles=styles,
    )
    with pytest.raises(AdapterError, match="cyclic"):
        extract(path)


@pytest.mark.parametrize(
    "adapter,error",
    [
        (DocxSourceAdapter(max_units=1), "source-unit"),
        (DocxSourceAdapter(max_text_chars=3), "character"),
    ],
)
def test_text_and_unit_limits_fail_without_truncating(
    tmp_path: Path, adapter: DocxSourceAdapter, error: str
) -> None:
    path = make_docx(tmp_path / "bounded.docx", paragraph("First") + paragraph("Second"))
    with pytest.raises(AdapterError, match=error):
        extract(path, adapter)
