from __future__ import annotations

import io
import stat
import struct
import warnings
import zlib
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import cast
from zipfile import ZIP_BZIP2, ZIP_DEFLATED, ZIP_STORED, ZipExtFile, ZipFile, ZipInfo

import pytest
from xlsx_fixtures import (
    MAIN_CONTENT_TYPE,
    PACKAGE_NS,
    REL_NS,
    XLSX_NS,
    make_xlsx,
)

import newsrag.docx_package as docx_package
import newsrag.xlsx_package as xlsx_package
from newsrag.adapters import AdapterError
from newsrag.xlsx_package import XLSX_PACKAGE_VERSION, load_xlsx_package

WORKBOOK = "xl/workbook.xml"
SHEET = "xl/worksheets/sheet1.xml"
SHEET_RELS = "xl/worksheets/_rels/sheet1.xml.rels"
WORKBOOK_RELS = "xl/_rels/workbook.xml.rels"


def relationships(body: str) -> str:
    return f'<Relationships xmlns="{PACKAGE_NS}">{body}</Relationships>'


def relation(rel_id: str, kind: str, target: str, mode: str = "") -> str:
    return (
        f'<Relationship Id="{rel_id}" Type="{REL_NS}/{kind}" Target="{target}"'
        + (f' TargetMode="{mode}"' if mode else "")
        + "/>"
    )


def worksheet(body: str = "<sheetData/>") -> str:
    return f'<worksheet xmlns="{XLSX_NS}" xmlns:r="{REL_NS}">{body}</worksheet>'


def rewrite(path: Path, mutation: Callable[[dict[str, bytes]], object]) -> None:
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    mutation(parts)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)


def add_part(path: Path, name: str, payload: str, kind: str, source: str, rel: str) -> None:
    def mutation(parts: dict[str, bytes]) -> None:
        parts[name] = payload.encode()
        parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
            b"</Types>",
            f'<Override PartName="/{name}" ContentType="{kind}"/></Types>'.encode(),
        )
        parts[source] = parts.get(source, relationships("").encode()).replace(
            b"</Relationships>", (rel + "</Relationships>").encode()
        )

    rewrite(path, mutation)


def test_public_contract_and_default_fixture(tmp_path: Path) -> None:
    package = load_xlsx_package(make_xlsx(tmp_path / "basic.xlsx"))
    assert XLSX_PACKAGE_VERSION == "1"
    assert xlsx_package.XLSX_NS == XLSX_NS
    assert xlsx_package.REL_NS == REL_NS
    assert package.workbook_part == WORKBOOK
    assert package.xml_parts[WORKBOOK].tag == f"{{{XLSX_NS}}}workbook"
    assert "Searchable evidence" in "".join(package.xml_parts[SHEET].itertext())
    assert package.relationships[WORKBOOK]["rId1"].relationship_type == f"{REL_NS}/worksheet"
    assert package.relationships[WORKBOOK]["rId1"].target_part == SHEET
    assert package.relationships[SHEET] == {}
    assert package.relationships[""]["main"].target_part == WORKBOOK
    assert set(package.xml_parts) == {
        "[Content_Types].xml",
        "_rels/.rels",
        WORKBOOK,
        WORKBOOK_RELS,
        SHEET,
    }


def test_fixture_order_and_explicit_xml_overrides(tmp_path: Path) -> None:
    sheets = {"xl/worksheets/z.xml": worksheet(), "xl/worksheets/a.xml": worksheet()}
    package = load_xlsx_package(make_xlsx(tmp_path / "ordered.xlsx", sheets=sheets))
    nodes = package.xml_parts[WORKBOOK].findall(f"{{{XLSX_NS}}}sheets/{{{XLSX_NS}}}sheet")
    assert [
        (node.get("name"), node.get("sheetId"), node.get(f"{{{REL_NS}}}id")) for node in nodes
    ] == [
        ("Sheet1", "1", "rId1"),
        ("Sheet2", "2", "rId2"),
    ]
    assert [rel.target_part for rel in package.relationships[WORKBOOK].values()] == list(sheets)
    empty = load_xlsx_package(make_xlsx(tmp_path / "empty.xlsx", sheets={}))
    assert empty.relationships[WORKBOOK] == {}


def test_external_worksheet_hyperlinks_discard_targets_without_losing_text(tmp_path: Path) -> None:
    path = make_xlsx(
        tmp_path / "links.xlsx",
        sheets={
            SHEET: worksheet(
                '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Visible link</t>'
                '</is></c></row></sheetData><hyperlinks><hyperlink ref="A1" r:id="link"/>'
                '<hyperlink ref="A2" location="Sheet1!A1"/></hyperlinks>'
            )
        },
        extra_parts={
            SHEET_RELS: relationships(
                relation("link", "hyperlink", "https://example.invalid/private-target", "External")
            )
        },
    )
    package = load_xlsx_package(path)
    rel = package.relationships[SHEET]["link"]
    assert rel.relationship_type == f"{REL_NS}/hyperlink"
    assert rel.target_part is None
    assert "Visible link" in "".join(package.xml_parts[SHEET].itertext())
    assert package.xml_parts[SHEET_RELS][0].get("Target") is None


def test_all_supported_auxiliary_parts_and_metadata_are_exported(tmp_path: Path) -> None:
    path = make_xlsx(tmp_path / "auxiliary.xlsx")
    for name, kind, content, suffix in (
        (
            "xl/styles.xml",
            "styles",
            f'<styleSheet xmlns="{XLSX_NS}"><cellXfs count="0"/></styleSheet>',
            "styles",
        ),
        (
            "xl/sharedStrings.xml",
            "sharedStrings",
            f'<sst xmlns="{XLSX_NS}"><si><t>Unused but validated</t></si></sst>',
            "sharedStrings",
        ),
        (
            "xl/theme/theme1.xml",
            "theme",
            '<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"/>',
            "theme",
        ),
        (
            "customXml/item1.xml",
            "customXml",
            '<metadata xmlns="urn:example"><label>Inert</label></metadata>',
            "customXml",
        ),
    ):
        content_type = xlsx_package._PART_TYPES[kind][0]
        target = "../" + name if name.startswith("customXml/") else name.removeprefix("xl/")
        add_part(path, name, content, content_type, WORKBOOK_RELS, relation(kind, suffix, target))
    add_part(
        path,
        "customXml/itemProps1.xml",
        '<datastoreItem xmlns="http://schemas.openxmlformats.org/officeDocument/2006/customXml"/>',
        xlsx_package._PART_TYPES["customXmlProps"][0],
        "customXml/_rels/item1.xml.rels",
        relation("props", "customXmlProps", "itemProps1.xml"),
    )
    for name, kind, namespace in (
        (
            "docProps/core.xml",
            "core-properties",
            "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
        ),
        (
            "docProps/app.xml",
            "extended-properties",
            "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties",
        ),
        (
            "docProps/custom.xml",
            "custom-properties",
            "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties",
        ),
    ):
        tag = "coreProperties" if kind == "core-properties" else "Properties"
        rel = relation(kind, kind, name)
        if kind == "core-properties":
            rel = rel.replace(f"{REL_NS}/core-properties", f"{PACKAGE_NS}/metadata/core-properties")
        add_part(
            path,
            name,
            f'<{tag} xmlns="{namespace}"/>',
            xlsx_package._PART_TYPES[kind][0],
            "_rels/.rels",
            rel,
        )
    package = load_xlsx_package(path)
    assert {
        "xl/styles.xml",
        "xl/sharedStrings.xml",
        "xl/theme/theme1.xml",
        "customXml/item1.xml",
        "customXml/itemProps1.xml",
        "docProps/core.xml",
        "docProps/app.xml",
        "docProps/custom.xml",
    } <= package.xml_parts.keys()


def test_native_table_relationship_ids_are_preserved(tmp_path: Path) -> None:
    path = make_xlsx(
        tmp_path / "table.xlsx",
        sheets={
            SHEET: worksheet(
                '<sheetData/><tableParts count="1"><tablePart r:id="table1"/></tableParts>'
            )
        },
    )
    add_part(
        path,
        "xl/tables/table1.xml",
        f'<table xmlns="{XLSX_NS}" id="1" name="Budget" ref="A1:B2"/>',
        xlsx_package._PART_TYPES["table"][0],
        SHEET_RELS,
        relation("table1", "table", "../tables/table1.xml"),
    )
    package = load_xlsx_package(path)
    assert package.relationships[SHEET]["table1"].target_part == "xl/tables/table1.xml"


@pytest.mark.parametrize(
    "kind",
    [
        "image",
        "drawing",
        "chart",
        "pivotTable",
        "pivotCacheDefinition",
        "externalLink",
        "connections",
        "queryTable",
        "vbaProject",
        "oleObject",
        "control",
        "attachedTemplate",
        "calcChain",
        "comments",
    ],
)
def test_unsupported_relationships_fail_even_without_evidence_references(
    tmp_path: Path, kind: str
) -> None:
    path = make_xlsx(
        tmp_path / "unsupported.xlsx",
        workbook_relationships=relationships(
            relation("rId1", "worksheet", "worksheets/sheet1.xml")
            + relation("unsupported", kind, "unneeded.xml")
        ),
    )
    with pytest.raises(AdapterError, match="unsupported relationship type"):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        (source, kind)
        for source in ("_rels/.rels", WORKBOOK_RELS, SHEET_RELS)
        for kind in ("worksheet", "image", "externalLink", "hyperlink")
        if (source, kind) != (SHEET_RELS, "hyperlink")
    ],
)
def test_external_relationship_policy_is_source_specific(
    tmp_path: Path, source: str, kind: str
) -> None:
    path = make_xlsx(tmp_path / "external.xlsx")
    rewrite(
        path,
        lambda parts: parts.__setitem__(
            source,
            parts.get(source, relationships("").encode()).replace(
                b"</Relationships>",
                (
                    relation("external", kind, "https://example.invalid/remote", "External")
                    + "</Relationships>"
                ).encode(),
            ),
        ),
    )
    with pytest.raises(AdapterError, match="forbids external relationships"):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    ("rel", "error"),
    [
        (relation("rId1", "worksheet", "missing.xml"), "targets missing part"),
        (relation("rId1", "worksheet", "../../escape.xml"), "traverses outside"),
        (relation("rId1", "worksheet", "%2fabsolute.xml"), "unsafe internal target"),
        (relation("rId1", "worksheet", "worksheets/sheet1.xml#fragment"), "unsafe internal target"),
        (relation("rId1", "worksheet", "worksheets/sheet1.xml?query"), "unsafe internal target"),
        (relation("rId1", "worksheet", "http://example.invalid/sheet.xml"), "unsafe traversal"),
        (relation("rId1", "worksheet", "worksheets/%GG.xml"), "unsafe internal target"),
        (relation("bad id", "worksheet", "worksheets/sheet1.xml"), "invalid Id"),
        (relation("rId1", "worksheet", "worksheets/sheet1.xml", "Remote"), "invalid TargetMode"),
        (relation("rId1", "styles", "worksheets/sheet1.xml"), "incompatible content type"),
        (relation("rId1", "officeDocument", "workbook.xml"), "incompatible source part"),
    ],
)
def test_invalid_relationships(tmp_path: Path, rel: str, error: str) -> None:
    with pytest.raises(AdapterError, match=error):
        load_xlsx_package(
            make_xlsx(tmp_path / "invalid.xlsx", workbook_relationships=relationships(rel))
        )


@pytest.mark.parametrize("attribute", ["id", "embed", "link", "unknown"])
def test_all_relationship_attribute_ids_must_exist(tmp_path: Path, attribute: str) -> None:
    path = make_xlsx(
        tmp_path / "missing-id.xlsx",
        sheets={SHEET: worksheet(f'<sheetData r:{attribute}="missing"/>')},
    )
    with pytest.raises(AdapterError, match="references missing relationship Id"):
        load_xlsx_package(path)


def test_existing_but_wrong_kind_reference_fails(tmp_path: Path) -> None:
    path = make_xlsx(
        tmp_path / "wrong-kind.xlsx",
        sheets={SHEET: worksheet('<sheetData/><tableParts><tablePart r:id="link"/></tableParts>')},
        extra_parts={
            SHEET_RELS: relationships(
                relation("link", "hyperlink", "https://example.invalid", "External")
            )
        },
    )
    with pytest.raises(AdapterError, match="incompatible relationship reference"):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    "body",
    [
        "<sheetData/><tableParts><tablePart/></tableParts>",
        '<sheetData/><tablePart r:id="missing"/>',
        '<sheetData/><hyperlinks><hyperlink r:embed="link"/></hyperlinks>',
        '<sheetData r:id="link"/>',
    ],
)
def test_relationships_require_expected_elements_attributes_and_locations(
    tmp_path: Path, body: str
) -> None:
    path = make_xlsx(
        tmp_path / "bad-reference.xlsx",
        sheets={SHEET: worksheet(body)},
        extra_parts={
            SHEET_RELS: relationships(
                relation("link", "hyperlink", "https://example.invalid", "External")
            )
        },
    )
    with pytest.raises(AdapterError, match="relationship (Id|reference)"):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda parts: parts.pop("[Content_Types].xml"), "missing required part"),
        (lambda parts: parts.pop("_rels/.rels"), "missing required root relationship"),
        (lambda parts: parts.pop(WORKBOOK_RELS), "references missing relationship Id"),
        (
            lambda parts: parts.__setitem__(WORKBOOK_RELS, relationships("").encode()),
            "references missing relationship Id",
        ),
        (
            lambda parts: parts.__setitem__("_rels/.rels", relationships("").encode()),
            "exactly one related workbook",
        ),
        (lambda parts: parts.__setitem__(SHEET, b"<notWorksheet/>"), "invalid root element"),
        (lambda parts: parts.__setitem__(WORKBOOK, b"<notWorkbook/>"), "invalid root element"),
        (
            lambda parts: parts.__setitem__("[Content_Types].xml", b'<Types xmlns="urn:not-opc"/>'),
            "invalid root element",
        ),
        (
            lambda parts: parts.__setitem__(WORKBOOK_RELS, b'<Relationships xmlns="urn:not-opc"/>'),
            "invalid root element",
        ),
        (
            lambda parts: parts.__setitem__("unrelated.dat", b"unused"),
            "no content-type association",
        ),
        (
            lambda parts: parts.__setitem__("unrelated.xml", b"<unused/>"),
            "orphan or unrelated part",
        ),
        (
            lambda parts: parts.__setitem__(
                "xl/_rels/missing.xml.rels", relationships("").encode()
            ),
            "missing or invalid source",
        ),
        (
            lambda parts: parts.__setitem__("xl/stray.rels", relationships("").encode()),
            "invalid package location",
        ),
    ],
)
def test_required_parts_and_reachability(
    tmp_path: Path, mutation: Callable[[dict[str, bytes]], object], error: str
) -> None:
    path = make_xlsx(tmp_path / "required.xlsx")
    rewrite(path, mutation)
    with pytest.raises(AdapterError, match=error):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    "content_type",
    [
        "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
        "application/vnd.ms-excel.sheet.binary.macroEnabled.main",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.chartsheet+xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.pivotTable+xml",
        "application/vnd.openxmlformats-officedocument.drawing+xml",
        "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
        "image/png",
        "application/octet-stream",
        "",
    ],
)
def test_format_content_type_allowlist(tmp_path: Path, content_type: str) -> None:
    path = make_xlsx(tmp_path / "disguised.xlsx")
    rewrite(
        path,
        lambda parts: parts.__setitem__(
            "[Content_Types].xml",
            parts["[Content_Types].xml"].replace(MAIN_CONTENT_TYPE.encode(), content_type.encode()),
        ),
    )
    with pytest.raises(AdapterError, match="unsupported content type"):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    "declaration",
    [
        '<Default Extension="xml" ContentType="application/xml"/>',
        f'<Override PartName="/{WORKBOOK}" ContentType="{MAIN_CONTENT_TYPE}"/>',
        '<Override PartName="/missing.xml" ContentType="application/xml"/>',
        '<Override PartName="/[Content_Types].xml" ContentType="application/xml"/>',
        '<Override PartName="relative.xml" ContentType="application/xml"/>',
        '<Default Extension=".xml" ContentType="application/xml"/>',
        '<Default Extension="unused" ContentType="image/png"/>',
        '<Unknown ContentType="application/xml"/>',
        '<Default Extension="extra" ContentType="application/xml"><Nested/></Default>',
    ],
)
def test_invalid_even_unused_content_declarations_fail(tmp_path: Path, declaration: str) -> None:
    path = make_xlsx(tmp_path / "types.xlsx")
    rewrite(
        path,
        lambda parts: parts.__setitem__(
            "[Content_Types].xml",
            parts["[Content_Types].xml"].replace(b"</Types>", (declaration + "</Types>").encode()),
        ),
    )
    with pytest.raises(AdapterError, match="XLSX"):
        load_xlsx_package(path)


def test_duplicate_ids_singletons_targets_and_undeclared_sheets(tmp_path: Path) -> None:
    for suffix, rels, error in (
        ("ids", relation("rId1", "worksheet", "worksheets/sheet1.xml") * 2, "duplicates Id"),
        (
            "undeclared",
            relation("rId1", "worksheet", "worksheets/sheet1.xml")
            + relation("unused", "worksheet", "worksheets/sheet1.xml"),
            "undeclared worksheet",
        ),
    ):
        with pytest.raises(AdapterError, match=error):
            load_xlsx_package(
                make_xlsx(tmp_path / f"{suffix}.xlsx", workbook_relationships=relationships(rels))
            )
    path = make_xlsx(tmp_path / "singleton.xlsx")
    rewrite(
        path,
        lambda parts: parts.__setitem__(
            "_rels/.rels",
            parts["_rels/.rels"].replace(
                b"</Relationships>",
                (relation("second", "officeDocument", WORKBOOK) + "</Relationships>").encode(),
            ),
        ),
    )
    with pytest.raises(AdapterError, match="duplicate singleton"):
        load_xlsx_package(path)
    path = make_xlsx(tmp_path / "same-target.xlsx")
    rewrite(
        path,
        lambda parts: parts.__setitem__(
            WORKBOOK,
            parts[WORKBOOK].replace(
                b"</sheets>", b'<sheet name="Second" sheetId="2" r:id="rId1"/></sheets>'
            ),
        ),
    )
    with pytest.raises(AdapterError, match="referenced more than once"):
        load_xlsx_package(path)


@pytest.mark.parametrize("part", [WORKBOOK, SHEET, "customXml/hidden.xml", "xl/sharedStrings.xml"])
@pytest.mark.parametrize("encoding", ["utf-8", "utf-16"])
def test_dtd_and_entities_rejected_in_all_parts(tmp_path: Path, part: str, encoding: str) -> None:
    payload = (
        f'<?xml version="1.0" encoding="{encoding}"?>'
        '<!DOCTYPE worksheet [<!ENTITY leak SYSTEM "https://example.invalid/never-fetch">]>'
        f'<worksheet xmlns="{XLSX_NS}"><sheetData>&leak;</sheetData></worksheet>'
    ).encode(encoding)
    path = make_xlsx(tmp_path / "dtd.xlsx", extra_parts={part: payload})
    with pytest.raises(AdapterError, match="forbidden DTD or entity|unresolved entity"):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    "body",
    [
        "<drawing/>",
        "<legacyDrawing/>",
        "<picture/>",
        "<pivotCaches/>",
        "<externalReferences/>",
        "<oleObjects/>",
        "<controls/>",
        "<connections/>",
        "<queryTable/>",
        "<extLst/>",
        '<future:feature xmlns:future="urn:future"/>',
        '<c:chart xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"/>',
    ],
)
def test_unsupported_features_in_hidden_sheets_fail(tmp_path: Path, body: str) -> None:
    path = make_xlsx(
        tmp_path / "hidden.xlsx",
        sheets={SHEET: worksheet(), "xl/worksheets/hidden.xml": worksheet(body)},
    )
    rewrite(
        path,
        lambda parts: parts.__setitem__(
            WORKBOOK, parts[WORKBOOK].replace(b'name="Sheet2"', b'name="Sheet2" state="veryHidden"')
        ),
    )
    with pytest.raises(AdapterError, match="unsupported"):
        load_xlsx_package(path)


def test_unknown_required_attributes_and_custom_xml_office_disguises_fail(tmp_path: Path) -> None:
    path = make_xlsx(
        tmp_path / "required-extension.xlsx",
        sheets={SHEET: worksheet('<sheetData xmlns:future="urn:future" future:required="yes"/>')},
    )
    with pytest.raises(AdapterError, match="unsupported XML attribute"):
        load_xlsx_package(path)
    path = make_xlsx(tmp_path / "custom-disguise.xlsx")
    add_part(
        path,
        "customXml/item1.xml",
        worksheet(),
        "application/xml",
        WORKBOOK_RELS,
        relation("custom", "customXml", "../customXml/item1.xml"),
    )
    with pytest.raises(AdapterError, match="disguises Office content"):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    "name",
    [
        "../escape.xml",
        "/absolute.xml",
        "xl\\windows.xml",
        "xl/./alias.xml",
        "xl/%2e%2e/out.xml",
        "xl/name%23fragment.xml",
        "xl/name?query.xml",
        "xl/%GG.xml",
        "xl/%ff.xml",
        "xl//empty.xml",
        "xl/drive:C.xml",
        "xl/\x01control.xml",
    ],
)
def test_unsafe_member_names(tmp_path: Path, name: str) -> None:
    path = make_xlsx(tmp_path / "unsafe.xlsx")
    with ZipFile(path, "a") as archive:
        archive.writestr(name, b"unused")
    with pytest.raises(AdapterError, match="unsafe|non-UTF-8"):
        load_xlsx_package(path)


@pytest.mark.parametrize("alias", [WORKBOOK, "XL/WORKBOOK.XML", "xl/%77orkbook.xml"])
def test_duplicate_and_aliased_members(tmp_path: Path, alias: str) -> None:
    path = make_xlsx(tmp_path / "alias.xlsx")
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(path, "a") as archive:
            archive.writestr(alias, b"duplicate")
    with pytest.raises(AdapterError, match="duplicate or aliased"):
        load_xlsx_package(path)


def test_unicode_normalization_aliases(tmp_path: Path) -> None:
    path = make_xlsx(tmp_path / "unicode.xlsx", sheets={"xl/worksheets/caf\u00e9.xml": worksheet()})
    with ZipFile(path, "a") as archive:
        archive.writestr("xl/worksheets/cafe\u0301.xml", worksheet())
    with pytest.raises(AdapterError, match="duplicate or aliased"):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    ("name", "payload", "mode", "error"),
    [
        ("xl/link.xml", b"target", stat.S_IFLNK | 0o777, "symbolic link"),
        ("xl/hidden/", b"payload", stat.S_IFDIR | 0o755, "directory member.*payload data"),
    ],
)
def test_symlink_and_directory_payloads(
    tmp_path: Path, name: str, payload: bytes, mode: int, error: str
) -> None:
    path = make_xlsx(tmp_path / "special.xlsx")
    member = ZipInfo(name)
    member.create_system = 3
    member.external_attr = mode << 16
    with ZipFile(path, "a") as archive:
        archive.writestr(member, payload)
    with pytest.raises(AdapterError, match=error):
        load_xlsx_package(path)


def test_encryption_and_unsupported_compression(tmp_path: Path) -> None:
    path = make_xlsx(tmp_path / "encrypted.xlsx")
    raw = bytearray(path.read_bytes())
    for signature, offset in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        index = raw.index(signature) + offset
        struct.pack_into("<H", raw, index, struct.unpack_from("<H", raw, index)[0] | 1)
    path.write_bytes(raw)
    with pytest.raises(AdapterError, match="encrypted"):
        load_xlsx_package(path)
    path = make_xlsx(tmp_path / "bzip.xlsx")
    with ZipFile(path, "a", compression=ZIP_BZIP2) as archive:
        archive.writestr("unused.xml", b"not allowed")
    with pytest.raises(AdapterError, match="unsupported compression"):
        load_xlsx_package(path)


def test_crc_and_bad_deflate_have_context(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = make_xlsx(tmp_path / "crc.xlsx")
    with ZipFile(path) as archive:
        parts = {name: archive.read(name) for name in archive.namelist()}
    with ZipFile(path, "w", compression=ZIP_STORED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    path.write_bytes(path.read_bytes().replace(b"Searchable evidence", b"Corrupted! evidence", 1))
    with pytest.raises(AdapterError, match="CRC or expanded-size"):
        load_xlsx_package(path)
    path = make_xlsx(tmp_path / "deflate.xlsx")

    def fail(self: ZipExtFile, size: int = -1) -> bytes:
        raise zlib.error("broken compressed stream")

    monkeypatch.setattr(ZipExtFile, "read", fail)
    with pytest.raises(AdapterError, match="XLSX part.*CRC or expanded-size"):
        load_xlsx_package(path)


@pytest.mark.parametrize(
    ("constant", "limit", "error"),
    [
        ("XLSX_MAX_SOURCE_BYTES", 10, "raw input limit"),
        ("XLSX_MAX_MEMBERS", 2, "member limit"),
        ("XLSX_MAX_EXPANDED_BYTES", 100, "total expansion limit"),
        ("XLSX_MAX_PART_BYTES", 100, "part limit"),
        ("XLSX_MAX_COMPRESSION_RATIO", 1, "compression-ratio limit"),
        ("XLSX_MAX_XML_ELEMENTS", 5, "element cumulative limit"),
        ("XLSX_MAX_XML_DEPTH", 2, "depth limit"),
    ],
)
def test_resource_limits_are_fail_closed_and_independent_of_docx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, constant: str, limit: int, error: str
) -> None:
    path = make_xlsx(tmp_path / "bounded.xlsx")
    monkeypatch.setattr(xlsx_package, constant, limit)
    with pytest.raises(AdapterError, match=error):
        load_xlsx_package(path)
    assert docx_package.DOCX_MAX_XML_ELEMENTS == 100_000


def test_exact_limits_and_cumulative_element_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert xlsx_package.XLSX_MAX_SOURCE_BYTES == 25 * 1024 * 1024
    assert xlsx_package.XLSX_MAX_EXPANDED_BYTES == 100 * 1024 * 1024
    assert xlsx_package.XLSX_MAX_PART_BYTES == 20 * 1024 * 1024
    assert xlsx_package.XLSX_MAX_MEMBERS == 2_000
    assert xlsx_package.XLSX_MAX_COMPRESSION_RATIO == 200
    assert xlsx_package.XLSX_MAX_XML_ELEMENTS == 500_000
    assert xlsx_package.XLSX_MAX_XML_DEPTH == 128
    path = make_xlsx(tmp_path / "exact.xlsx")
    package = load_xlsx_package(path)
    count = sum(sum(1 for _ in root.iter()) for root in package.xml_parts.values())
    monkeypatch.setattr(xlsx_package, "XLSX_MAX_XML_ELEMENTS", count)
    load_xlsx_package(path)
    monkeypatch.setattr(xlsx_package, "XLSX_MAX_XML_ELEMENTS", count - 1)
    with pytest.raises(AdapterError, match="cumulative limit"):
        load_xlsx_package(path)


def test_raw_bounded_reader_catches_growth_after_stat(monkeypatch: pytest.MonkeyPatch) -> None:
    class RacingPath:
        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=4)

        def open(self, mode: str) -> io.BytesIO:
            return io.BytesIO(b"PK\x03\x04" + b"x" * 100)

    monkeypatch.setattr(xlsx_package, "XLSX_MAX_SOURCE_BYTES", 8)
    with pytest.raises(AdapterError, match="raw input limit"):
        xlsx_package._opc_reader().read_bounded_package(cast(Path, RacingPath()))


@pytest.mark.parametrize(
    "payload", [b"", b"not a ZIP", bytes.fromhex("D0CF11E0A1B11AE1"), b"PK\x03\x04broken"]
)
def test_invalid_and_non_zip_packages(tmp_path: Path, payload: bytes) -> None:
    path = tmp_path / "invalid.xlsx"
    path.write_bytes(payload)
    with pytest.raises(AdapterError, match="XLSX.*(empty|not a ZIP|invalid ZIP)"):
        load_xlsx_package(path)


@pytest.mark.parametrize("name", ["[Content_Types].xml", "_rels/.rels", WORKBOOK_RELS])
def test_opc_markup_cannot_contain_unexpected_text(tmp_path: Path, name: str) -> None:
    path = make_xlsx(tmp_path / "text-in-opc.xlsx")
    rewrite(
        path, lambda parts: parts.__setitem__(name, parts[name].replace(b">", b">invalid text", 1))
    )
    with pytest.raises(AdapterError, match="unexpected text"):
        load_xlsx_package(path)


def test_relationship_part_cannot_claim_wrong_directory_source(tmp_path: Path) -> None:
    path = make_xlsx(tmp_path / "wrong-source-location.xlsx")
    rewrite(
        path,
        lambda parts: parts.__setitem__("_rels/xl/workbook.xml.rels", parts.pop(WORKBOOK_RELS)),
    )
    with pytest.raises(AdapterError, match="invalid package location"):
        load_xlsx_package(path)


@pytest.mark.parametrize("tag", ["blip", "blipFill", "graphic", "pic"])
def test_theme_cannot_hide_unsupported_images_or_drawings(tmp_path: Path, tag: str) -> None:
    path = make_xlsx(tmp_path / "theme-image.xlsx")
    add_part(
        path,
        "xl/theme/theme1.xml",
        f'<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><a:{tag}/></a:theme>',
        xlsx_package._PART_TYPES["theme"][0],
        WORKBOOK_RELS,
        relation("theme", "theme", "theme/theme1.xml"),
    )
    with pytest.raises(AdapterError, match="unsupported or active XML"):
        load_xlsx_package(path)


@pytest.mark.parametrize("tag", ["blob", "oblob", "stream", "storage", "vstream"])
def test_custom_properties_cannot_hide_binary_payloads(tmp_path: Path, tag: str) -> None:
    path = make_xlsx(tmp_path / "binary-metadata.xlsx")
    add_part(
        path,
        "docProps/custom.xml",
        '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/custom-properties" '
        'xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
        f'<property name="hidden"><vt:{tag}>TVpFeGVjdXRhYmxl</vt:{tag}></property></Properties>',
        xlsx_package._PART_TYPES["custom-properties"][0],
        "_rels/.rels",
        relation("custom", "custom-properties", "docProps/custom.xml"),
    )
    with pytest.raises(AdapterError, match="unsupported or active XML"):
        load_xlsx_package(path)


def test_root_level_workbook_and_absolute_targets(tmp_path: Path) -> None:
    path = make_xlsx(tmp_path / "root-workbook.xlsx")

    def mutation(parts: dict[str, bytes]) -> None:
        parts["workbook.xml"] = parts.pop(WORKBOOK)
        parts["_rels/workbook.xml.rels"] = parts.pop(WORKBOOK_RELS).replace(
            b'Target="worksheets/', b'Target="/xl/worksheets/'
        )
        parts["_rels/.rels"] = parts["_rels/.rels"].replace(WORKBOOK.encode(), b"workbook.xml")
        parts["[Content_Types].xml"] = parts["[Content_Types].xml"].replace(
            WORKBOOK.encode(), b"workbook.xml"
        )

    rewrite(path, mutation)
    package = load_xlsx_package(path)
    assert package.workbook_part == "workbook.xml"
    assert package.relationships["workbook.xml"]["rId1"].target_part == SHEET
