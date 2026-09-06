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
from docx_fixtures import CONTENT_NS, PACKAGE_NS, make_docx, paragraph

import newsrag.docx_package as docx_package
from newsrag.adapters import AdapterError
from newsrag.docx_package import (
    DOCX_BLOCK_LOCATION_TYPE,
    DOCX_MAX_SOURCE_BYTES,
    DOCX_MEDIA_TYPE,
    DOCX_PACKAGE_VERSION,
    REL_NS,
    WORD_NS,
    load_docx_package,
)

MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
RELATIONSHIPS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"


def test_public_contract_and_related_parts(tmp_path: Path) -> None:
    path = make_docx(
        tmp_path / "valid.docx",
        paragraph("Visible evidence"),
        styles='<w:style w:type="paragraph" w:styleId="Normal"/>',
        numbering='<w:abstractNum w:abstractNumId="0"/>',
        footnotes='<w:footnote w:id="1"><w:p/></w:footnote>',
    )

    package = load_docx_package(path)

    assert DOCX_PACKAGE_VERSION == "1"
    assert DOCX_MEDIA_TYPE == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert DOCX_MAX_SOURCE_BYTES == 25 * 1024 * 1024
    assert DOCX_BLOCK_LOCATION_TYPE == "docx_block"
    assert WORD_NS == "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    assert REL_NS == "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    assert package.document_part == "word/document.xml"
    assert package.xml_parts[package.document_part].tag == f"{{{WORD_NS}}}document"
    assert (
        package.related_part(package.document_part, "styles")
        is package.xml_parts["word/styles.xml"]
    )
    assert (
        package.related_part(package.document_part, "/numbering/")
        is package.xml_parts["word/numbering.xml"]
    )
    assert (
        package.related_part(package.document_part, "footnotes")
        is package.xml_parts["word/footnotes.xml"]
    )
    assert package.related_part(package.document_part, "endnotes") is None
    assert package.related_part("../unsafe.xml", "styles") is None


def test_external_hyperlinks_are_ignored_but_visible_text_remains(tmp_path: Path) -> None:
    body = (
        '<w:p><w:hyperlink r:id="external"><w:r><w:t>visible link</w:t></w:r></w:hyperlink></w:p>'
    )
    relationship = (
        f'<Relationship Id="external" Type="{REL_NS}/hyperlink" '
        'Target="https://example.invalid/report" TargetMode="External"/>'
    )
    package = load_docx_package(
        make_docx(tmp_path / "hyperlink.docx", body, relationships=relationship)
    )

    assert "visible link" in "".join(package.xml_parts[package.document_part].itertext())
    assert package.related_part(package.document_part, "hyperlink") is None


@pytest.mark.parametrize("attribute_name", ["id", "embed", "link"])
def test_xml_relationship_references_must_resolve(
    tmp_path: Path,
    attribute_name: str,
) -> None:
    body = f'<w:p r:{attribute_name}="missing"><w:r><w:t>text</w:t></w:r></w:p>'

    with pytest.raises(AdapterError, match="references missing relationship Id 'missing'"):
        load_docx_package(make_docx(tmp_path / "dangling-id.docx", body))


@pytest.mark.parametrize(
    ("relationship", "error"),
    [
        (
            f'<Relationship Id="remote" Type="{REL_NS}/image" '
            'Target="https://example.invalid/image.png" TargetMode="External"/>',
            "forbidden non-hyperlink external",
        ),
        (
            f'<Relationship Id="missing" Type="{REL_NS}/styles" Target="missing.xml"/>',
            "targets missing part",
        ),
        (
            f'<Relationship Id="escape" Type="{REL_NS}/styles" Target="../../escape.xml"/>',
            "traverses outside",
        ),
        (
            f'<Relationship Id="bad id" Type="{REL_NS}/hyperlink" '
            'Target="https://example.invalid" TargetMode="External"/>',
            "invalid Id",
        ),
        (
            f'<Relationship Id="unknown" Type="{REL_NS}/attachedTemplate" Target="template.xml"/>',
            "unsupported relationship type",
        ),
        (
            f'<Relationship Id="odd" Type="{REL_NS}/hyperlink" '
            'Target="https://example.invalid" TargetMode="Remote"/>',
            "invalid TargetMode",
        ),
    ],
)
def test_unsafe_relationships_are_rejected(
    tmp_path: Path,
    relationship: str,
    error: str,
) -> None:
    path = make_docx(tmp_path / "relationship.docx", paragraph("text"), relationships=relationship)

    with pytest.raises(AdapterError, match=error):
        load_docx_package(path)


def test_duplicate_relationship_ids_are_rejected(tmp_path: Path) -> None:
    relationships = (
        f'<Relationship Id="same" Type="{REL_NS}/hyperlink" Target="https://one.invalid" '
        'TargetMode="External"/>'
        f'<Relationship Id="same" Type="{REL_NS}/hyperlink" Target="https://two.invalid" '
        'TargetMode="External"/>'
    )

    with pytest.raises(AdapterError, match="duplicates Id"):
        load_docx_package(
            make_docx(
                tmp_path / "duplicate-relationship.docx",
                paragraph("text"),
                relationships=relationships,
            )
        )


def test_relationship_suffix_requires_matching_content_type_and_xml_root(tmp_path: Path) -> None:
    wrong_type = make_docx(
        tmp_path / "wrong-type.docx",
        paragraph("text"),
        styles="<w:style/>",
    )
    _rewrite_parts(
        wrong_type,
        lambda parts: parts.__setitem__(
            "[Content_Types].xml",
            parts["[Content_Types].xml"].replace(
                b"wordprocessingml.styles+xml", b"wordprocessingml.numbering+xml"
            ),
        ),
    )
    with pytest.raises(AdapterError, match="incompatible content type"):
        load_docx_package(wrong_type)

    wrong_root = make_docx(
        tmp_path / "wrong-root.docx",
        paragraph("text"),
        styles="<w:style/>",
    )
    _rewrite_parts(
        wrong_root,
        lambda parts: parts.__setitem__(
            "word/styles.xml", f'<w:numbering xmlns:w="{WORD_NS}"/>'.encode()
        ),
    )
    with pytest.raises(AdapterError, match="invalid root element"):
        load_docx_package(wrong_root)


@pytest.mark.parametrize(
    ("member_name", "error"),
    [
        ("../escape.xml", "unsafe traversal"),
        ("/absolute.xml", "unsafe absolute"),
        ("word\\backslash.xml", "unsafe path"),
        ("word/./alias.xml", "unsafe traversal"),
        ("word/%2e%2e/escape.xml", "unsafe traversal"),
        ("word/name%23fragment.xml", "unsafe path"),
    ],
)
def test_unsafe_zip_member_names_are_rejected(
    tmp_path: Path,
    member_name: str,
    error: str,
) -> None:
    path = make_docx(tmp_path / "unsafe-name.docx", paragraph("text"))
    with ZipFile(path, "a", compression=ZIP_STORED) as archive:
        archive.writestr(member_name, b"unused")

    with pytest.raises(AdapterError, match=error):
        load_docx_package(path)


@pytest.mark.parametrize("duplicate_name", ["word/document.xml", "WORD/DOCUMENT.XML"])
def test_duplicate_and_case_aliased_zip_members_are_rejected(
    tmp_path: Path,
    duplicate_name: str,
) -> None:
    path = make_docx(tmp_path / "duplicate.docx", paragraph("text"))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with ZipFile(path, "a", compression=ZIP_STORED) as archive:
            archive.writestr(duplicate_name, b"duplicate")

    with pytest.raises(AdapterError, match="duplicate or aliased"):
        load_docx_package(path)


def test_zip_symlinks_encryption_and_unsupported_compression_are_rejected(
    tmp_path: Path,
) -> None:
    directory_path = make_docx(tmp_path / "directory-payload.docx", paragraph("text"))
    directory = ZipInfo("word/hidden/")
    with ZipFile(directory_path, "a") as archive:
        archive.writestr(directory, b"hidden payload")
    with pytest.raises(AdapterError, match="directory member.*payload data"):
        load_docx_package(directory_path)

    symlink_path = make_docx(tmp_path / "symlink.docx", paragraph("text"))
    link = ZipInfo("word/media/link.png")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    with ZipFile(symlink_path, "a") as archive:
        archive.writestr(link, b"target")
    with pytest.raises(AdapterError, match="symbolic link"):
        load_docx_package(symlink_path)

    encrypted_path = make_docx(tmp_path / "encrypted.docx", paragraph("text"))
    encrypted_path.write_bytes(_set_first_member_encrypted(encrypted_path.read_bytes()))
    with pytest.raises(AdapterError, match="encrypted"):
        load_docx_package(encrypted_path)

    bzip_path = tmp_path / "bzip.docx"
    parts = _minimal_parts(paragraph("text"))
    with ZipFile(bzip_path, "w") as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload, compress_type=ZIP_BZIP2)
    with pytest.raises(AdapterError, match="unsupported compression method"):
        load_docx_package(bzip_path)


def test_crc_mismatch_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bad-crc.docx"
    parts = _minimal_parts(paragraph("original marker"))
    with ZipFile(path, "w", compression=ZIP_STORED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)
    corrupted = path.read_bytes().replace(b"original marker", b"corrupt! marker", 1)
    path.write_bytes(corrupted)

    with pytest.raises(AdapterError, match="CRC or expanded-size"):
        load_docx_package(path)


def test_bounded_reader_limits_a_file_that_grows_after_stat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RacingPath:
        def stat(self) -> SimpleNamespace:
            return SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_size=4)

        def open(self, mode: str) -> io.BytesIO:
            assert mode == "rb"
            return io.BytesIO(b"PK\x03\x04" + (b"x" * 100))

    monkeypatch.setattr(docx_package, "DOCX_MAX_SOURCE_BYTES", 8)

    with pytest.raises(AdapterError, match="raw input limit"):
        docx_package._read_bounded_package(cast(Path, RacingPath()))


def test_bad_deflate_errors_are_wrapped_with_part_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = make_docx(tmp_path / "bad-deflate.docx", paragraph("text"))

    def raise_deflate_error(self: ZipExtFile, size: int = -1) -> bytes:
        del self, size
        raise zlib.error("invalid compressed stream")

    monkeypatch.setattr(ZipExtFile, "read", raise_deflate_error)

    with pytest.raises(AdapterError, match="failed CRC or expanded-size validation"):
        load_docx_package(path)


def test_raw_member_expansion_part_ratio_and_xml_limits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = make_docx(tmp_path / "limits.docx", paragraph("bounded content"))

    monkeypatch.setattr(docx_package, "DOCX_MAX_SOURCE_BYTES", 10)
    with pytest.raises(AdapterError, match="raw input limit"):
        load_docx_package(path)

    monkeypatch.setattr(docx_package, "DOCX_MAX_SOURCE_BYTES", 25 * 1024 * 1024)
    monkeypatch.setattr(docx_package, "DOCX_MAX_MEMBERS", 2)
    with pytest.raises(AdapterError, match="member limit"):
        load_docx_package(path)

    monkeypatch.setattr(docx_package, "DOCX_MAX_MEMBERS", 2_000)
    monkeypatch.setattr(docx_package, "DOCX_MAX_EXPANDED_BYTES", 100)
    with pytest.raises(AdapterError, match="total expansion limit"):
        load_docx_package(path)

    monkeypatch.setattr(docx_package, "DOCX_MAX_EXPANDED_BYTES", 100 * 1024 * 1024)
    monkeypatch.setattr(docx_package, "DOCX_MAX_PART_BYTES", 100)
    with pytest.raises(AdapterError, match="part limit"):
        load_docx_package(path)

    monkeypatch.setattr(docx_package, "DOCX_MAX_PART_BYTES", 20 * 1024 * 1024)
    monkeypatch.setattr(docx_package, "DOCX_MAX_COMPRESSION_RATIO", 1)
    with pytest.raises(AdapterError, match="compression-ratio limit"):
        load_docx_package(path)

    monkeypatch.setattr(docx_package, "DOCX_MAX_COMPRESSION_RATIO", 200)
    monkeypatch.setattr(docx_package, "DOCX_MAX_XML_ELEMENTS", 5)
    with pytest.raises(AdapterError, match="element cumulative limit"):
        load_docx_package(path)

    monkeypatch.setattr(docx_package, "DOCX_MAX_XML_ELEMENTS", 100_000)
    monkeypatch.setattr(docx_package, "DOCX_MAX_XML_DEPTH", 3)
    with pytest.raises(AdapterError, match="depth limit"):
        load_docx_package(path)


@pytest.mark.parametrize(
    ("mutation", "error"),
    [
        (lambda parts: parts.pop("[Content_Types].xml"), "missing required part"),
        (lambda parts: parts.pop("_rels/.rels"), "missing required root relationship"),
        (
            lambda parts: parts.__setitem__(
                "[Content_Types].xml",
                b'<Types xmlns="urn:not-opc"><Default Extension="xml" ContentType="application/xml"/></Types>',
            ),
            "invalid root element or namespace",
        ),
        (
            lambda parts: parts.__setitem__(
                "[Content_Types].xml",
                parts["[Content_Types].xml"].replace(
                    b"wordprocessingml.document.main+xml",
                    b"wordprocessingml.document.macroEnabled.main+xml",
                ),
            ),
            "unsafe active content type",
        ),
        (
            lambda parts: parts.__setitem__(
                "[Content_Types].xml",
                parts["[Content_Types].xml"].replace(
                    MAIN_CONTENT_TYPE.encode(),
                    b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
                ),
            ),
            "unsupported content type",
        ),
        (
            lambda parts: parts.__setitem__("unrelated.dat", b"orphan"),
            "no content-type association",
        ),
    ],
)
def test_required_package_and_content_type_validation(
    tmp_path: Path,
    mutation: Callable[[dict[str, bytes]], object],
    error: str,
) -> None:
    path = make_docx(tmp_path / "types.docx", paragraph("text"))
    _rewrite_parts(path, mutation)

    with pytest.raises(AdapterError, match=error):
        load_docx_package(path)


def test_ordinary_producer_metadata_custom_xml_and_styles_with_effects_are_safe(
    tmp_path: Path,
) -> None:
    relationships = (
        f'<Relationship Id="metadata" Type="{REL_NS}/customXml" '
        'Target="../docProps/meta.xml"/>'
        f'<Relationship Id="custom" Type="{REL_NS}/customXml" '
        'Target="../customXml/item1.xml"/>'
        f'<Relationship Id="theme" Type="{REL_NS}/theme" Target="theme/theme1.xml"/>'
        '<Relationship Id="effects" '
        'Type="http://schemas.microsoft.com/office/2007/relationships/stylesWithEffects" '
        'Target="stylesWithEffects.xml"/>'
    )
    extra_types = (
        '<Override PartName="/word/theme/theme1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>'
        '<Override PartName="/word/stylesWithEffects.xml" '
        'ContentType="application/vnd.ms-word.stylesWithEffects+xml"/>'
        '<Override PartName="/docProps/core.xml" '
        'ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
        '<Override PartName="/docProps/app.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>'
        '<Override PartName="/customXml/itemProps1.xml" '
        'ContentType="application/vnd.openxmlformats-officedocument.customXmlProperties+xml"/>'
    )
    extra_parts = {
        "word/theme/theme1.xml": (
            b'<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            b'name="Office"/>'
        ),
        "word/stylesWithEffects.xml": f'<w:styles xmlns:w="{WORD_NS}"/>'.encode(),
        "docProps/core.xml": (
            b'<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/'
            b'2006/metadata/core-properties"/>'
        ),
        "docProps/app.xml": (
            b'<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/'
            b'2006/extended-properties"/>'
        ),
        "docProps/meta.xml": (
            b'<meta xmlns="http://schemas.apple.com/cocoa/2006/metadata">'
            b"<generator>producer</generator></meta>"
        ),
        "customXml/item1.xml": b'<metadata xmlns="urn:example">safe</metadata>',
        "customXml/itemProps1.xml": (
            b'<ds:datastoreItem xmlns:ds="http://schemas.openxmlformats.org/'
            b'officeDocument/2006/customXml" ds:itemID="{safe}"/>'
        ),
        "customXml/_rels/item1.xml.rels": (
            f'<Relationships xmlns="{PACKAGE_NS}"><Relationship Id="props" '
            f'Type="{REL_NS}/customXmlProps" '
            'Target="itemProps1.xml"/></Relationships>'
        ).encode(),
    }
    path = make_docx(
        tmp_path / "producer.docx",
        paragraph("producer text"),
        relationships=relationships,
        extra_parts=extra_parts,
        extra_types=extra_types,
    )

    def add_root_properties(parts: dict[str, bytes]) -> None:
        parts["_rels/.rels"] = parts["_rels/.rels"].replace(
            b"</Relationships>",
            (
                f'<Relationship Id="core" Type="http://schemas.openxmlformats.org/package/'
                '2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>'
                f'<Relationship Id="app" Type="{REL_NS}/extended-properties" '
                'Target="docProps/app.xml"/></Relationships>'
            ).encode(),
        )

    _rewrite_parts(path, add_root_properties)
    package = load_docx_package(path)

    assert package.xml_parts["docProps/meta.xml"].tag == (
        "{http://schemas.apple.com/cocoa/2006/metadata}meta"
    )
    assert package.xml_parts["customXml/itemProps1.xml"].tag.endswith("}datastoreItem")
    assert package.xml_parts["word/stylesWithEffects.xml"].tag == f"{{{WORD_NS}}}styles"


def test_root_relationships_are_limited_to_document_metadata_and_thumbnail(
    tmp_path: Path,
) -> None:
    thumbnail_path = make_docx(
        tmp_path / "thumbnail.docx",
        paragraph("text"),
        extra_parts={"docProps/thumbnail.jpeg": b"inert thumbnail"},
        extra_types='<Default Extension="jpeg" ContentType="image/jpeg"/>',
    )

    def add_thumbnail(parts: dict[str, bytes]) -> None:
        parts["_rels/.rels"] = parts["_rels/.rels"].replace(
            b"</Relationships>",
            (
                b'<Relationship Id="thumbnail" '
                b'Type="http://schemas.openxmlformats.org/package/2006/relationships/'
                b'metadata/thumbnail" Target="docProps/thumbnail.jpeg"/></Relationships>'
            ),
        )

    _rewrite_parts(thumbnail_path, add_thumbnail)
    package = load_docx_package(thumbnail_path)
    assert "docProps/thumbnail.jpeg" not in package.xml_parts

    unrelated_path = make_docx(tmp_path / "root-hyperlink.docx", paragraph("text"))

    def add_root_hyperlink(parts: dict[str, bytes]) -> None:
        parts["_rels/.rels"] = parts["_rels/.rels"].replace(
            b"</Relationships>",
            (
                f'<Relationship Id="link" Type="{REL_NS}/hyperlink" '
                'Target="https://example.invalid" TargetMode="External"/></Relationships>'
            ).encode(),
        )

    _rewrite_parts(unrelated_path, add_root_hyperlink)
    with pytest.raises(AdapterError, match="root contains unsupported relationship"):
        load_docx_package(unrelated_path)


def test_exactly_one_internal_root_office_document_is_required(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "two-main-rels.docx", paragraph("text"))

    def add_root_relationship(parts: dict[str, bytes]) -> None:
        parts["_rels/.rels"] = parts["_rels/.rels"].replace(
            b"</Relationships>",
            f'<Relationship Id="other" Type="{REL_NS}/officeDocument" '
            'Target="word/document.xml"/></Relationships>'.encode(),
        )

    _rewrite_parts(path, add_root_relationship)
    with pytest.raises(AdapterError, match="exactly one officeDocument"):
        load_docx_package(path)

    external_path = make_docx(tmp_path / "external-main.docx", paragraph("text"))
    _rewrite_parts(
        external_path,
        lambda parts: parts.__setitem__(
            "_rels/.rels",
            parts["_rels/.rels"].replace(
                b'Target="word/document.xml"',
                b'Target="https://example.invalid/document.xml" TargetMode="External"',
            ),
        ),
    )
    with pytest.raises(AdapterError, match="forbidden non-hyperlink external"):
        load_docx_package(external_path)


def test_hardened_xml_parser_rejects_dtd_entities_and_malformed_xml(tmp_path: Path) -> None:
    dtd_path = make_docx(tmp_path / "dtd.docx", paragraph("text"))
    _rewrite_parts(
        dtd_path,
        lambda parts: parts.__setitem__(
            "word/document.xml",
            (
                b'<!DOCTYPE document [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                + parts["word/document.xml"].replace(b"text", b"&xxe;")
            ),
        ),
    )
    with pytest.raises(AdapterError, match="forbidden DTD or entity"):
        load_docx_package(dtd_path)

    utf16_dtd_path = make_docx(tmp_path / "utf16-dtd.docx", paragraph("text"))
    utf16_document = (
        '<?xml version="1.0" encoding="UTF-16"?>'
        '<!DOCTYPE w:document [<!ENTITY hidden "blocked">]>'
        f'<w:document xmlns:w="{WORD_NS}" xmlns:r="{REL_NS}"><w:body>'
        "<w:p><w:r><w:t>&hidden;</w:t></w:r></w:p>"
        "</w:body></w:document>"
    ).encode("utf-16")
    _rewrite_parts(
        utf16_dtd_path,
        lambda parts: parts.__setitem__("word/document.xml", utf16_document),
    )
    with pytest.raises(AdapterError, match="forbidden DTD or entity|unresolved entity"):
        load_docx_package(utf16_dtd_path)

    malformed_path = make_docx(tmp_path / "malformed.docx", paragraph("text"))
    _rewrite_parts(
        malformed_path,
        lambda parts: parts.__setitem__("word/document.xml", b"<w:document>"),
    )
    with pytest.raises(AdapterError, match="is malformed"):
        load_docx_package(malformed_path)


def test_strict_ooxml_namespace_is_rejected_explicitly(tmp_path: Path) -> None:
    path = make_docx(tmp_path / "strict.docx", paragraph("text"))
    strict_ns = b"http://purl.oclc.org/ooxml/wordprocessingml/main"
    _rewrite_parts(
        path,
        lambda parts: parts.__setitem__(
            "word/document.xml", parts["word/document.xml"].replace(WORD_NS.encode(), strict_ns)
        ),
    )

    with pytest.raises(AdapterError, match="unsupported strict OOXML profile"):
        load_docx_package(path)


@pytest.mark.parametrize(
    "active_xml",
    [
        '<w:altChunk r:id="payload"/>',
        "<w:object><w:r><w:t>object</w:t></w:r></w:object>",
        '<w:fldSimple w:instr="INCLUDETEXT https://example.invalid/payload"/>',
        "<w:p><w:r><w:instrText>INCLUDE</w:instrText></w:r>"
        "<w:r><w:instrText>PICTURE https://example.invalid/pixel</w:instrText></w:r></w:p>",
        "<w:p><w:r><w:instrText>DDEAUTO command</w:instrText></w:r></w:p>",
        "<w:p><w:r><w:instrText>DATABASE query</w:instrText></w:r></w:p>",
        "<w:p><w:r><w:instrText>LINK object</w:instrText></w:r></w:p>",
    ],
)
def test_active_elements_and_field_instructions_are_rejected(
    tmp_path: Path,
    active_xml: str,
) -> None:
    path = make_docx(tmp_path / "active.docx", active_xml)

    with pytest.raises(AdapterError, match="unsafe active"):
        load_docx_package(path)


def test_images_are_bounded_inert_and_never_exposed_as_xml(tmp_path: Path) -> None:
    image_relationship = (
        f'<Relationship Id="image" Type="{REL_NS}/image" Target="media/image1.png"/>'
    )
    image_type = '<Default Extension="png" ContentType="image/png"/>'
    path = make_docx(
        tmp_path / "image.docx",
        paragraph("image caption"),
        relationships=image_relationship,
        extra_parts={"word/media/image1.png": b"\x89PNG\r\n\x1a\n inert"},
        extra_types=image_type,
    )

    package = load_docx_package(path)

    assert "word/media/image1.png" not in package.xml_parts
    assert package.related_part(package.document_part, "image") is None


def test_disguised_active_images_and_orphan_parts_are_rejected(tmp_path: Path) -> None:
    relationship = f'<Relationship Id="image" Type="{REL_NS}/image" Target="media/image.png"/>'
    image_type = '<Default Extension="png" ContentType="image/png"/>'
    for filename, payload, error in (
        ("ole-image.docx", bytes.fromhex("D0CF11E0A1B11AE1") + b"payload", "embedded OLE"),
        ("zip-image.docx", b"PK\x03\x04embedded package", "embedded ZIP"),
        ("executable-image.docx", b"MZexecutable", "executable payload"),
    ):
        disguised_path = make_docx(
            tmp_path / filename,
            paragraph("text"),
            relationships=relationship,
            extra_parts={"word/media/image.png": payload},
            extra_types=image_type,
        )
        with pytest.raises(AdapterError, match=error):
            load_docx_package(disguised_path)

    orphan_path = make_docx(
        tmp_path / "orphan-image.docx",
        paragraph("text"),
        extra_parts={"word/media/image.png": b"\x89PNG inert"},
        extra_types=image_type,
    )
    with pytest.raises(AdapterError, match="orphan or unrelated part"):
        load_docx_package(orphan_path)


def _minimal_parts(body: str) -> dict[str, bytes]:
    return {
        "[Content_Types].xml": (
            f'<Types xmlns="{CONTENT_NS}">'
            f'<Default Extension="rels" ContentType="{RELATIONSHIPS_CONTENT_TYPE}"/>'
            '<Default Extension="xml" ContentType="application/xml"/>'
            f'<Override PartName="/word/document.xml" ContentType="{MAIN_CONTENT_TYPE}"/>'
            "</Types>"
        ).encode(),
        "_rels/.rels": (
            f'<Relationships xmlns="{PACKAGE_NS}"><Relationship Id="main" '
            f'Type="{REL_NS}/officeDocument" Target="word/document.xml"/></Relationships>'
        ).encode(),
        "word/document.xml": (
            f'<w:document xmlns:w="{WORD_NS}" xmlns:r="{REL_NS}"><w:body>{body}'
            "</w:body></w:document>"
        ).encode(),
    }


def _rewrite_parts(path: Path, mutation: Callable[[dict[str, bytes]], object]) -> None:
    with ZipFile(path) as archive:
        parts = {member.filename: archive.read(member) for member in archive.infolist()}
    mutation(parts)
    with ZipFile(path, "w", compression=ZIP_DEFLATED) as archive:
        for name, payload in parts.items():
            archive.writestr(name, payload)


def _set_first_member_encrypted(package: bytes) -> bytes:
    modified = bytearray(package)
    local_header = modified.index(b"PK\x03\x04")
    local_flags = struct.unpack_from("<H", modified, local_header + 6)[0]
    struct.pack_into("<H", modified, local_header + 6, local_flags | 0x1)
    central_header = modified.index(b"PK\x01\x02")
    central_flags = struct.unpack_from("<H", modified, central_header + 8)[0]
    struct.pack_into("<H", modified, central_header + 8, central_flags | 0x1)
    return bytes(modified)
