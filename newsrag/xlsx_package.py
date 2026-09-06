from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from lxml import etree  # type: ignore[import-untyped]

from newsrag.adapters import AdapterError
from newsrag.opc_package import OpcReader

XLSX_PACKAGE_VERSION = "1"
XLSX_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
XLSX_MAX_SOURCE_BYTES = 25 * 1024 * 1024
XLSX_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
XLSX_MAX_PART_BYTES = 20 * 1024 * 1024
XLSX_MAX_MEMBERS = 2_000
XLSX_MAX_COMPRESSION_RATIO = 200
XLSX_MAX_XML_ELEMENTS = 500_000
XLSX_MAX_XML_DEPTH = 128

_PACKAGE_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_CUSTOM_NS = "http://schemas.openxmlformats.org/officeDocument/2006/customXml"
_VT_NS = "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"
_CORE_NS = "http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
_EXTENDED_NS = "http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"
_PROPERTIES_NS = "http://schemas.openxmlformats.org/officeDocument/2006/custom-properties"
_REL_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
_XML_CONTENT_TYPE = "application/xml"
_SHEET_TYPE_PREFIX = "application/vnd.openxmlformats-officedocument.spreadsheetml."
_OFFICE_TYPE_PREFIX = "application/vnd.openxmlformats-officedocument."

# Content types, XML roots, and relationship source/target kinds are deliberately
# independent of DOCX. Generic application/xml is only usable as related custom XML.
_PART_TYPES = {
    "workbook": (_SHEET_TYPE_PREFIX + "sheet.main+xml", f"{{{XLSX_NS}}}workbook"),
    "worksheet": (_SHEET_TYPE_PREFIX + "worksheet+xml", f"{{{XLSX_NS}}}worksheet"),
    "sharedStrings": (_SHEET_TYPE_PREFIX + "sharedstrings+xml", f"{{{XLSX_NS}}}sst"),
    "styles": (_SHEET_TYPE_PREFIX + "styles+xml", f"{{{XLSX_NS}}}styleSheet"),
    "table": (_SHEET_TYPE_PREFIX + "table+xml", f"{{{XLSX_NS}}}table"),
    "theme": (_OFFICE_TYPE_PREFIX + "theme+xml", f"{{{_DRAWING_NS}}}theme"),
    "core-properties": (
        "application/vnd.openxmlformats-package.core-properties+xml",
        f"{{{_CORE_NS}}}coreProperties",
    ),
    "extended-properties": (
        _OFFICE_TYPE_PREFIX + "extended-properties+xml",
        f"{{{_EXTENDED_NS}}}Properties",
    ),
    "custom-properties": (
        _OFFICE_TYPE_PREFIX + "custom-properties+xml",
        f"{{{_PROPERTIES_NS}}}Properties",
    ),
    "customXml": (_XML_CONTENT_TYPE, None),
    "customXmlProps": (
        _OFFICE_TYPE_PREFIX + "customxmlproperties+xml",
        f"{{{_CUSTOM_NS}}}datastoreItem",
    ),
    "relationships": (_REL_CONTENT_TYPE, f"{{{_PACKAGE_NS}}}Relationships"),
}
_KIND_BY_TYPE = {content_type: kind for kind, (content_type, _) in _PART_TYPES.items()}
_RELATIONSHIP_TARGET_KINDS = {
    f"{REL_NS}/officeDocument": "workbook",
    f"{_PACKAGE_NS}/metadata/core-properties": "core-properties",
    **{
        f"{REL_NS}/{kind}": kind
        for kind in (
            "worksheet",
            "sharedStrings",
            "styles",
            "table",
            "theme",
            "extended-properties",
            "custom-properties",
            "customXml",
            "customXmlProps",
        )
    },
}
_ALLOWED_TARGETS_BY_SOURCE = {
    "": frozenset({"workbook", "core-properties", "extended-properties", "custom-properties"}),
    "workbook": frozenset({"worksheet", "sharedStrings", "styles", "theme", "customXml"}),
    "worksheet": frozenset({"table"}),
    "customXml": frozenset({"customXmlProps"}),
}
_HYPERLINK_TYPE = f"{REL_NS}/hyperlink"
_RELATIONSHIP_ID_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_UNSUPPORTED_PART_TERMS = (
    "vbaproject",
    "/activex/",
    "/embeddings/",
    "/macros/",
    "/externallinks/",
    "/connections",
    "/querytables/",
    "/pivot",
    "/charts/",
    "/drawings/",
    "/media/",
)
_UNSUPPORTED_ELEMENTS = frozenset(
    {
        "externalreferences",
        "externalreference",
        "externalbook",
        "ddelink",
        "olelink",
        "connections",
        "connection",
        "querytable",
        "querytablerefresh",
        "webpr",
        "dbpr",
        "pivotcaches",
        "pivotcache",
        "pivottable",
        "pivottabledefinition",
        "pivotcachedefinition",
        "pivotcacherecords",
        "drawing",
        "legacydrawing",
        "legacydrawinghf",
        "picture",
        "blip",
        "blipfill",
        "graphic",
        "graphicframe",
        "pic",
        "oleobjects",
        "oleobject",
        "controls",
        "control",
        "object",
        "altchunk",
        "webpublishobjects",
        "webpublishobject",
        "webpublishing",
        "extlst",
        "alternatecontent",
        "customsheetviews",
        "customworkbookviews",
        "smarttagtypes",
        "smarttags",
    }
)
_SPREADSHEET_KINDS = frozenset({"workbook", "worksheet", "sharedStrings", "styles", "table"})
_NAMESPACE_BY_KIND = {
    "theme": frozenset({_DRAWING_NS}),
    "core-properties": frozenset(
        {
            _CORE_NS,
            "http://purl.org/dc/elements/1.1/",
            "http://purl.org/dc/terms/",
        }
    ),
    "extended-properties": frozenset({_EXTENDED_NS, _VT_NS}),
    "custom-properties": frozenset({_PROPERTIES_NS, _VT_NS}),
    "customXmlProps": frozenset({_CUSTOM_NS}),
    "relationships": frozenset({_PACKAGE_NS}),
}


@dataclass(frozen=True)
class Relationship:
    """A validated relationship; external hyperlink targets are discarded."""

    relationship_type: str
    target_part: str | None


@dataclass(frozen=True)
class XlsxPackage:
    """An inert OPC view, prior to workbook/cell semantic extraction.

    Relationship maps preserve IDs and full type URIs. Every content part has a
    map (possibly empty); the package root uses the empty string as its source.
    External hyperlink targets are also removed from exported relationship XML.
    """

    xml_parts: dict[str, etree._Element]
    workbook_part: str
    relationships: dict[str, dict[str, Relationship]]


def _opc_reader() -> OpcReader:
    return OpcReader(
        format_name="XLSX",
        max_source_bytes=XLSX_MAX_SOURCE_BYTES,
        max_members=XLSX_MAX_MEMBERS,
        max_expanded_bytes=XLSX_MAX_EXPANDED_BYTES,
        max_part_bytes=XLSX_MAX_PART_BYTES,
        max_compression_ratio=XLSX_MAX_COMPRESSION_RATIO,
        max_xml_elements=XLSX_MAX_XML_ELEMENTS,
        max_xml_depth=XLSX_MAX_XML_DEPTH,
    )


def load_xlsx_package(path: Path) -> XlsxPackage:
    """Validate every part of a bounded transitional worksheet package.

    Fail with AdapterError for unsupported/active content or broken OPC structure.
    No URLs are followed and no workbook expressions are interpreted or calculated.
    Sheet names/order, coordinates, value indices, and table geometry belong to the
    semantic extractor; this layer checks all package relationship IDs and types.
    """
    reader = _opc_reader()
    payloads = reader.read_zip_members(reader.read_bounded_package(path))
    types_root = reader.parse_xml(
        reader.required_part(payloads, "[Content_Types].xml"), "[Content_Types].xml"
    )
    count = reader.validate_xml_tree(types_root, "[Content_Types].xml", 0)
    kinds = _content_kinds(reader, types_root, payloads)
    xml_parts = {"[Content_Types].xml": types_root}
    for name, kind in kinds.items():
        root = reader.parse_xml(payloads[name], name)
        count = reader.validate_xml_tree(root, name, count)
        _validate_profile(name, kind, root)
        xml_parts[name] = root
    relationships = _relationships(reader, payloads, xml_parts, kinds)
    workbook_parts = [name for name, kind in kinds.items() if kind == "workbook"]
    main_targets = [
        rel.target_part
        for rel in relationships[""].values()
        if rel.relationship_type == f"{REL_NS}/officeDocument"
    ]
    if len(main_targets) != 1 or workbook_parts != main_targets:
        raise AdapterError("XLSX package must contain exactly one related workbook officeDocument")
    workbook_part = workbook_parts[0]
    _validate_references(xml_parts, relationships, kinds)
    _validate_reachability(reader, relationships, kinds)
    return XlsxPackage(xml_parts, workbook_part, relationships)


def _content_kinds(
    reader: OpcReader, root: etree._Element, payloads: dict[str, bytes]
) -> dict[str, str]:
    if root.tag != f"{{{_CONTENT_NS}}}Types" or root.attrib:
        raise AdapterError("XLSX content types have an invalid root element or namespace")
    _validate_markup_text(root, "[Content_Types].xml")
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for declaration in root:
        content_type = (declaration.get("ContentType") or "").strip().lower()
        if content_type not in _KIND_BY_TYPE:
            raise AdapterError(f"XLSX package declares unsupported content type {content_type!r}")
        if len(declaration):
            raise AdapterError("XLSX content types contain a nested declaration")
        if declaration.tag == f"{{{_CONTENT_NS}}}Default":
            if set(declaration.attrib) != {"Extension", "ContentType"}:
                raise AdapterError("XLSX content types have invalid default attributes")
            extension = declaration.get("Extension", "").lower()
            if not extension or not extension.isascii() or not extension.isalnum():
                raise AdapterError("XLSX content types have an invalid default extension")
            if extension in defaults:
                raise AdapterError("XLSX content types duplicate extension")
            defaults[extension] = content_type
        elif declaration.tag == f"{{{_CONTENT_NS}}}Override":
            if set(declaration.attrib) != {"PartName", "ContentType"}:
                raise AdapterError("XLSX content types have invalid override attributes")
            raw_name = declaration.get("PartName", "")
            if not raw_name.startswith("/"):
                raise AdapterError("XLSX content-type override requires an absolute package name")
            name = reader.normalize_member_name(raw_name[1:], "content-type override")
            alias = reader.part_alias(name)
            if alias in overrides:
                raise AdapterError("XLSX content types duplicate part")
            if name == "[Content_Types].xml" or reader.match_part_name(payloads, name) is None:
                raise AdapterError("XLSX content types reference a missing or reserved part")
            overrides[alias] = content_type
        else:
            raise AdapterError("XLSX content types contain an unsupported declaration")
    kinds: dict[str, str] = {}
    for name in payloads:
        if name == "[Content_Types].xml":
            continue
        if any(term in f"/{name.casefold()}/" for term in _UNSUPPORTED_PART_TERMS):
            raise AdapterError(f"XLSX package contains unsupported or active part {name!r}")
        content_type = overrides.get(
            reader.part_alias(name), defaults.get(name.rpartition(".")[2].lower())
        )
        if content_type is None:
            raise AdapterError(f"XLSX part {name!r} has no content-type association")
        kind = _KIND_BY_TYPE[content_type]
        if name.endswith(".rels") != (kind == "relationships"):
            raise AdapterError(f"XLSX relationship part {name!r} has incompatible content type")
        if kind != "relationships" and not name.lower().endswith(".xml"):
            raise AdapterError(f"XLSX content part {name!r} must use an .xml part name")
        kinds[name] = kind
    return kinds


def _validate_markup_text(root: etree._Element, name: str) -> None:
    for element in root.iter():
        if (element.text or "").strip() or (element.tail or "").strip():
            raise AdapterError(f"XLSX OPC part {name!r} contains unexpected text")


def _validate_profile(name: str, kind: str, root: etree._Element) -> None:
    expected_root = _PART_TYPES[kind][1]
    if expected_root is not None and root.tag != expected_root:
        raise AdapterError(f"XLSX part {name!r} has an invalid root element or namespace")
    allowed_namespaces = (
        frozenset({XLSX_NS}) if kind in _SPREADSHEET_KINDS else _NAMESPACE_BY_KIND.get(kind)
    )
    for element in root.iter():
        qname = etree.QName(element.tag)
        if qname.localname.casefold() in _UNSUPPORTED_ELEMENTS or (
            qname.namespace == _VT_NS
            and qname.localname
            in {"blob", "oblob", "stream", "storage", "vstream", "cf", "ostorage"}
        ):
            raise AdapterError(f"XLSX part {name!r} contains unsupported or active XML content")
        if "http://purl.oclc.org/ooxml/" in element.tag or any(
            "http://purl.oclc.org/ooxml/" in attribute for attribute in element.attrib
        ):
            raise AdapterError(f"XLSX part {name!r} uses the unsupported strict OOXML profile")
        if allowed_namespaces is not None and qname.namespace not in allowed_namespaces:
            raise AdapterError(f"XLSX part {name!r} contains an unsupported XML namespace")
        if (
            kind == "customXml"
            and qname.namespace
            and (
                "schemas.openxmlformats.org" in qname.namespace
                or "schemas.microsoft.com" in qname.namespace
                or qname.namespace.startswith("urn:schemas-microsoft-com:")
            )
        ):
            raise AdapterError(f"XLSX custom XML part {name!r} disguises Office content")
        # Foreign attributes can declare mandatory extension behavior, even without
        # foreign child elements. mc:Ignorable alone is inert; extensions still fail.
        for attribute in element.attrib:
            attribute_ns = etree.QName(attribute).namespace
            if attribute_ns is None or attribute_ns in {
                REL_NS,
                "http://www.w3.org/XML/1998/namespace",
                "http://www.w3.org/2001/XMLSchema-instance",
            }:
                continue
            if (
                attribute
                == "{http://schemas.openxmlformats.org/markup-compatibility/2006}Ignorable"
            ):
                continue
            if kind == "customXml" or (allowed_namespaces and attribute_ns in allowed_namespaces):
                continue
            raise AdapterError(f"XLSX part {name!r} contains an unsupported XML attribute")


def _relationships(
    reader: OpcReader,
    payloads: dict[str, bytes],
    xml_parts: dict[str, etree._Element],
    kinds: dict[str, str],
) -> dict[str, dict[str, Relationship]]:
    if "_rels/.rels" not in xml_parts:
        raise AdapterError("XLSX package is missing required root relationship part '_rels/.rels'")
    result: dict[str, dict[str, Relationship]] = {"": {}}
    result.update({name: {} for name, kind in kinds.items() if kind != "relationships"})
    sources: set[str] = set()
    for name, kind in kinds.items():
        if kind != "relationships":
            continue
        source = _relationship_source(reader, name)
        if source not in result or source in sources:
            raise AdapterError(f"XLSX relationship part {name!r} has missing or invalid source")
        sources.add(source)
        source_kind = kinds.get(source, "")
        root = xml_parts[name]
        _validate_markup_text(root, name)
        if root.attrib:
            raise AdapterError(f"XLSX relationship part {name!r} has invalid root attributes")
        target_kinds: set[str] = set()
        for element in root:
            if element.tag != f"{{{_PACKAGE_NS}}}Relationship" or len(element):
                raise AdapterError(f"XLSX relationship part {name!r} has an unsupported element")
            if not {"Id", "Type", "Target"} <= set(element.attrib) or set(element.attrib) - {
                "Id",
                "Type",
                "Target",
                "TargetMode",
            }:
                raise AdapterError(f"XLSX relationship part {name!r} has invalid attributes")
            rel_id = element.get("Id", "")
            if _RELATIONSHIP_ID_PATTERN.fullmatch(rel_id) is None:
                raise AdapterError(f"XLSX relationship has invalid Id {rel_id!r}")
            if rel_id in result[source]:
                raise AdapterError(f"XLSX relationship part {name!r} duplicates Id {rel_id!r}")
            rel_type = element.get("Type", "")
            mode = element.get("TargetMode", "Internal")
            target = element.get("Target", "")
            if mode == "External":
                if rel_type != _HYPERLINK_TYPE or source_kind != "worksheet":
                    raise AdapterError(
                        "XLSX forbids external relationships except worksheet hyperlinks"
                    )
                if not target.strip() or any(ord(char) < 32 or ord(char) == 127 for char in target):
                    raise AdapterError("XLSX external hyperlink has an invalid target")
                result[source][rel_id] = Relationship(rel_type, None)
                del element.attrib["Target"]
                continue
            if mode != "Internal":
                raise AdapterError(f"XLSX relationship has invalid TargetMode {mode!r}")
            target_kind = _RELATIONSHIP_TARGET_KINDS.get(rel_type)
            if target_kind is None:
                raise AdapterError(
                    f"XLSX package contains unsupported relationship type {rel_type!r}"
                )
            if target_kind not in _ALLOWED_TARGETS_BY_SOURCE.get(source_kind, frozenset()):
                raise AdapterError(f"XLSX relationship {rel_id!r} has an incompatible source part")
            resolved = reader.resolve_relationship_target(source, target)
            target_part = reader.match_part_name(payloads, resolved)
            if target_part is None:
                raise AdapterError(
                    f"XLSX relationship {rel_id!r} targets missing part {resolved!r}"
                )
            if kinds.get(target_part) != target_kind:
                raise AdapterError(f"XLSX relationship {rel_id!r} has incompatible content type")
            if (
                target_kind not in {"worksheet", "table", "customXml"}
                and target_kind in target_kinds
            ):
                raise AdapterError(
                    f"XLSX source {source or '/'} has duplicate singleton relationships"
                )
            target_kinds.add(target_kind)
            result[source][rel_id] = Relationship(rel_type, target_part)
    return result


def _relationship_source(reader: OpcReader, name: str) -> str:
    # OPC also permits a workbook at the package root, unlike usual xl/ layouts.
    if name.startswith("_rels/") and name != "_rels/.rels":
        filename = name.removeprefix("_rels/").removesuffix(".rels")
        if "/" in filename:
            raise AdapterError(f"XLSX relationship part {name!r} has an invalid package location")
        return reader.normalize_member_name(filename, "relationship source")
    return reader.source_for_relationship_part(name)


def _validate_references(
    xml_parts: dict[str, etree._Element],
    relationships: dict[str, dict[str, Relationship]],
    kinds: dict[str, str],
) -> None:
    # Explicit declarations must refer to the right kind, not merely an existing ID.
    reference_kinds = {
        f"{{{XLSX_NS}}}sheet": ("workbook", "sheets", "worksheet"),
        f"{{{XLSX_NS}}}tablePart": ("worksheet", "tableParts", "table"),
        f"{{{XLSX_NS}}}hyperlink": ("worksheet", "hyperlinks", "hyperlink"),
    }
    owned_targets: set[str] = set()
    for name, kind in kinds.items():
        if kind == "relationships":
            continue
        referenced: set[str] = set()
        for element in xml_parts[name].iter():
            expected = reference_kinds.get(element.tag)
            rel_attributes = [key for key in element.attrib if key.startswith(f"{{{REL_NS}}}")]
            rel_id = element.get(f"{{{REL_NS}}}id")
            if expected is not None:
                source_kind, parent_name, target_kind = expected
                parent = element.getparent()
                if (
                    kind != source_kind
                    or parent is None
                    or parent.tag != f"{{{XLSX_NS}}}{parent_name}"
                    or parent.getparent() is not xml_parts[name]
                ):
                    raise AdapterError(
                        f"XLSX part {name!r} has an invalid relationship reference location"
                    )
                if target_kind != "hyperlink" and rel_id is None:
                    raise AdapterError(f"XLSX part {name!r} has a missing relationship Id")
            for attribute in rel_attributes:
                value = element.get(attribute)
                rel = relationships[name].get(value)
                if rel is None:
                    raise AdapterError(
                        f"XLSX part {name!r} references missing relationship Id {value!r}"
                    )
                if (
                    expected is None
                    or attribute != f"{{{REL_NS}}}id"
                    or rel.relationship_type != f"{REL_NS}/{expected[2]}"
                ):
                    raise AdapterError(
                        f"XLSX part {name!r} has an incompatible relationship reference"
                    )
                if expected[2] in {"worksheet", "table"}:
                    if rel.target_part is None or rel.target_part in owned_targets:
                        raise AdapterError(
                            "XLSX worksheet or table target is referenced more than once"
                        )
                    owned_targets.add(rel.target_part)
                referenced.add(value)
        for rel_id, rel in relationships[name].items():
            if (
                rel.relationship_type in {f"{REL_NS}/worksheet", f"{REL_NS}/table"}
                and rel_id not in referenced
            ):
                raise AdapterError(
                    f"XLSX part {name!r} has an undeclared worksheet or table relationship"
                )


def _validate_reachability(
    reader: OpcReader,
    relationships: dict[str, dict[str, Relationship]],
    kinds: dict[str, str],
) -> None:
    reachable = {""}
    queue = [""]
    while queue:
        source = queue.pop()
        for rel in relationships[source].values():
            if rel.target_part is not None and rel.target_part not in reachable:
                reachable.add(rel.target_part)
                queue.append(rel.target_part)
    for name, kind in kinds.items():
        source = _relationship_source(reader, name) if kind == "relationships" else name
        if source not in reachable:
            raise AdapterError(f"XLSX package contains orphan or unrelated part {name!r}")
