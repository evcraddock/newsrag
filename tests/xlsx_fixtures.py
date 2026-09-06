from __future__ import annotations

import posixpath
from pathlib import Path
from xml.sax.saxutils import quoteattr
from zipfile import ZIP_DEFLATED, ZipFile

XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
MAIN_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"
WORKSHEET_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"


def make_xlsx(
    path: Path,
    *,
    workbook: str | None = None,
    sheets: dict[str, str] | None = None,
    extra_parts: dict[str, str | bytes] | None = None,
    content_types: str | None = None,
    root_relationships: str | None = None,
    workbook_relationships: str | None = None,
) -> Path:
    """Build a real minimal OPC workbook; explicit XML parameters are full documents.

    Sheet paths retain mapping order. Extra parts replace generated payloads and do
    not automatically add declarations or relationships, enabling malformed fixtures.
    """
    if sheets is None:
        sheets = {
            "xl/worksheets/sheet1.xml": (
                f'<worksheet xmlns="{XLSX_NS}" xmlns:r="{REL_NS}"><sheetData>'
                '<row r="1"><c r="A1" t="inlineStr"><is><t>Searchable evidence</t>'
                "</is></c></row></sheetData></worksheet>"
            )
        }
    declarations = "".join(
        f'<sheet name="Sheet{index}" sheetId="{index}" r:id="rId{index}"/>'
        for index, _ in enumerate(sheets, 1)
    )
    if workbook is None:
        workbook = (
            f'<workbook xmlns="{XLSX_NS}" xmlns:r="{REL_NS}">'
            f"<sheets>{declarations}</sheets></workbook>"
        )
    if workbook_relationships is None:
        relationships = "".join(
            f'<Relationship Id="rId{index}" Type="{REL_NS}/worksheet" '
            f"Target={quoteattr(posixpath.relpath(name, 'xl'))}/>"
            for index, name in enumerate(sheets, 1)
        )
        workbook_relationships = (
            f'<Relationships xmlns="{PACKAGE_NS}">{relationships}</Relationships>'
        )
    if root_relationships is None:
        root_relationships = (
            f'<Relationships xmlns="{PACKAGE_NS}"><Relationship Id="main" '
            f'Type="{REL_NS}/officeDocument" Target="xl/workbook.xml"/></Relationships>'
        )
    if content_types is None:
        overrides = "".join(
            f'<Override PartName={quoteattr("/" + name)} ContentType="{WORKSHEET_CONTENT_TYPE}"/>'
            for name in sheets
        )
        content_types = (
            f'<Types xmlns="{CONTENT_NS}"><Default Extension="rels" '
            'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/xl/workbook.xml" ContentType="{MAIN_CONTENT_TYPE}"/>'
            f"{overrides}</Types>"
        )
    parts: dict[str, str | bytes] = {
        "[Content_Types].xml": content_types,
        "_rels/.rels": root_relationships,
        "xl/workbook.xml": workbook,
        "xl/_rels/workbook.xml.rels": workbook_relationships,
        **sheets,
        **(extra_parts or {}),
    }
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    return path
