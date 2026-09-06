from __future__ import annotations

import io
import posixpath
import re
import stat
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote_to_bytes

from lxml import etree  # type: ignore[import-untyped]

from newsrag import sources as _sources
from newsrag.adapters import AdapterError

DOCX_PACKAGE_VERSION = "1"
DOCX_MEDIA_TYPE = _sources.DOCX_MEDIA_TYPE
DOCX_MAX_SOURCE_BYTES = _sources.DOCX_MAX_SOURCE_BYTES
DOCX_BLOCK_LOCATION_TYPE = _sources.DOCX_BLOCK_LOCATION_TYPE
DOCX_MAX_MEMBERS = 2_000
DOCX_MAX_EXPANDED_BYTES = 100 * 1024 * 1024
DOCX_MAX_PART_BYTES = 20 * 1024 * 1024
DOCX_MAX_COMPRESSION_RATIO = 200
DOCX_MAX_XML_ELEMENTS = 100_000
DOCX_MAX_XML_DEPTH = 128

WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"

_PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_CONTENT_TYPES_NS = "http://schemas.openxmlformats.org/package/2006/content-types"
_OFFICE_REL_PREFIX = f"{REL_NS}/"
_PACKAGE_REL_PREFIX = "http://schemas.openxmlformats.org/package/2006/relationships/"
_MS_OFFICE_REL_PREFIX = "http://schemas.microsoft.com/office/2007/relationships/"
_STRICT_NAMESPACE_FRAGMENT = "http://purl.oclc.org/ooxml/"

_RELATIONSHIPS_CONTENT_TYPE = "application/vnd.openxmlformats-package.relationships+xml"
_XML_CONTENT_TYPE = "application/xml"
_MAIN_CONTENT_TYPE = (
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
)
_CONTENT_TYPE_BY_RELATIONSHIP = {
    f"{_OFFICE_REL_PREFIX}officeDocument": frozenset({_MAIN_CONTENT_TYPE}),
    f"{_PACKAGE_REL_PREFIX}metadata/core-properties": frozenset(
        {"application/vnd.openxmlformats-package.core-properties+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}extended-properties": frozenset(
        {"application/vnd.openxmlformats-officedocument.extended-properties+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}custom-properties": frozenset(
        {"application/vnd.openxmlformats-officedocument.custom-properties+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}styles": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}numbering": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}footnotes": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}endnotes": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}comments": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}settings": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}webSettings": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.websettings+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}fontTable": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.fonttable+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}theme": frozenset(
        {"application/vnd.openxmlformats-officedocument.theme+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}header": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}footer": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}glossaryDocument": frozenset(
        {"application/vnd.openxmlformats-officedocument.wordprocessingml.document.glossary+xml"}
    ),
    f"{_OFFICE_REL_PREFIX}customXml": frozenset({_XML_CONTENT_TYPE}),
    f"{_OFFICE_REL_PREFIX}customXmlProps": frozenset(
        {"application/vnd.openxmlformats-officedocument.customxmlproperties+xml"}
    ),
    f"{_MS_OFFICE_REL_PREFIX}stylesWithEffects": frozenset(
        {"application/vnd.ms-word.styleswitheffects+xml"}
    ),
}
_IMAGE_CONTENT_TYPES = frozenset(
    {
        "image/bmp",
        "image/gif",
        "image/jpeg",
        "image/png",
        "image/tiff",
        "image/x-emf",
        "image/x-wmf",
    }
)
_CONTENT_TYPE_BY_RELATIONSHIP[f"{_OFFICE_REL_PREFIX}image"] = _IMAGE_CONTENT_TYPES
_CONTENT_TYPE_BY_RELATIONSHIP[f"{_PACKAGE_REL_PREFIX}metadata/thumbnail"] = _IMAGE_CONTENT_TYPES
_ROOT_RELATIONSHIP_TYPES = frozenset(
    {
        f"{_OFFICE_REL_PREFIX}officeDocument",
        f"{_PACKAGE_REL_PREFIX}metadata/core-properties",
        f"{_PACKAGE_REL_PREFIX}metadata/thumbnail",
        f"{_OFFICE_REL_PREFIX}extended-properties",
        f"{_OFFICE_REL_PREFIX}custom-properties",
    }
)
_ALLOWED_CONTENT_TYPES = frozenset(
    {
        _RELATIONSHIPS_CONTENT_TYPE,
        _XML_CONTENT_TYPE,
        *[value for values in _CONTENT_TYPE_BY_RELATIONSHIP.values() for value in values],
    }
)
_XML_CONTENT_TYPES = _ALLOWED_CONTENT_TYPES - _IMAGE_CONTENT_TYPES
_EXTERNAL_HYPERLINK_TYPE = f"{_OFFICE_REL_PREFIX}hyperlink"
_HYPERLINK_SOURCE_CONTENT_TYPES = frozenset(
    {
        _MAIN_CONTENT_TYPE,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document.glossary+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml",
    }
)
_SINGLETON_RELATIONSHIP_TYPES = frozenset(
    {
        f"{_OFFICE_REL_PREFIX}{suffix}"
        for suffix in (
            "styles",
            "numbering",
            "footnotes",
            "endnotes",
            "comments",
            "settings",
            "webSettings",
            "fontTable",
            "theme",
            "glossaryDocument",
            "customXmlProps",
        )
    }
    | {f"{_MS_OFFICE_REL_PREFIX}stylesWithEffects"}
)
_EXPECTED_ROOT_BY_CONTENT_TYPE = {
    _MAIN_CONTENT_TYPE: f"{{{WORD_NS}}}document",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml": (
        f"{{{WORD_NS}}}styles"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml": (
        f"{{{WORD_NS}}}numbering"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footnotes+xml": (
        f"{{{WORD_NS}}}footnotes"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.endnotes+xml": (
        f"{{{WORD_NS}}}endnotes"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.comments+xml": (
        f"{{{WORD_NS}}}comments"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.settings+xml": (
        f"{{{WORD_NS}}}settings"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.websettings+xml": (
        f"{{{WORD_NS}}}webSettings"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.fonttable+xml": (
        f"{{{WORD_NS}}}fonts"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml": (
        f"{{{WORD_NS}}}hdr"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml": (
        f"{{{WORD_NS}}}ftr"
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.glossary+xml": (
        f"{{{WORD_NS}}}glossaryDocument"
    ),
    "application/vnd.openxmlformats-officedocument.theme+xml": (
        "{http://schemas.openxmlformats.org/drawingml/2006/main}theme"
    ),
    "application/vnd.openxmlformats-package.core-properties+xml": (
        "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}coreProperties"
    ),
    "application/vnd.openxmlformats-officedocument.extended-properties+xml": (
        "{http://schemas.openxmlformats.org/officeDocument/2006/extended-properties}Properties"
    ),
    "application/vnd.openxmlformats-officedocument.custom-properties+xml": (
        "{http://schemas.openxmlformats.org/officeDocument/2006/custom-properties}Properties"
    ),
    "application/vnd.openxmlformats-officedocument.customxmlproperties+xml": (
        "{http://schemas.openxmlformats.org/officeDocument/2006/customXml}datastoreItem"
    ),
    "application/vnd.ms-word.styleswitheffects+xml": f"{{{WORD_NS}}}styles",
}
_IMAGE_EXTENSIONS = {
    "bmp": frozenset({"image/bmp"}),
    "emf": frozenset({"image/x-emf"}),
    "gif": frozenset({"image/gif"}),
    "jpe": frozenset({"image/jpeg"}),
    "jpeg": frozenset({"image/jpeg"}),
    "jpg": frozenset({"image/jpeg"}),
    "png": frozenset({"image/png"}),
    "tif": frozenset({"image/tiff"}),
    "tiff": frozenset({"image/tiff"}),
    "wmf": frozenset({"image/x-wmf"}),
}
_DANGEROUS_CONTENT_TYPE_TERMS = (
    "activex",
    "macroenabled",
    "oleobject",
    "vbaproject",
)
_DANGEROUS_PART_TERMS = (
    "/activex/",
    "/embeddings/",
    "/macros/",
    "vbaproject",
)
_DANGEROUS_ELEMENT_NAMES = frozenset(
    {"altchunk", "control", "controlpr", "movie", "object", "oleobject"}
)
_DANGEROUS_ELEMENT_NAMESPACES = frozenset(
    {
        WORD_NS,
        "urn:schemas-microsoft-com:office:office",
        "urn:schemas-microsoft-com:vml",
    }
)
_XML_DECLARATION_PATTERN = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_RELATIONSHIP_ID_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")
_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")
_DANGEROUS_FIELD_PATTERN = re.compile(
    r"(?<![A-Z])(?:DATABASE|DDEAUTO|DDE|INCLUDEPICTURE|INCLUDETEXT|LINK|RD|MACROBUTTON)"
)
_ACTIVE_BINARY_SIGNATURES = {
    bytes.fromhex("D0CF11E0A1B11AE1"): "embedded OLE object",
    b"PK\x03\x04": "embedded ZIP package",
    b"MZ": "executable payload",
}


@dataclass(frozen=True)
class _Relationship:
    relationship_type: str
    target_part: str


@dataclass(frozen=True)
class DocxPackage:
    """A validated, inert view of the XML parts in one DOCX package."""

    xml_parts: dict[str, etree._Element]
    document_part: str
    _relationships: dict[str, tuple[_Relationship, ...]] = field(repr=False)

    def related_part(
        self,
        source_part: str,
        relationship_type: str,
    ) -> etree._Element | None:
        """Return the first related XML part for a transitional relationship suffix."""

        suffix = relationship_type.strip("/")
        if not suffix or ":" in suffix or suffix.startswith("."):
            return None
        try:
            normalized_source = _normalize_member_name(source_part, "relationship source")
        except AdapterError:
            return None
        expected_type = f"{_OFFICE_REL_PREFIX}{suffix}"
        for relationship in self._relationships.get(normalized_source, ()):
            if relationship.relationship_type == expected_type:
                return self.xml_parts.get(relationship.target_part)
        return None


def load_docx_package(path: Path) -> DocxPackage:
    """Read and validate a bounded transitional DOCX package without extracting it."""

    raw_package = _read_bounded_package(path)
    payloads = _read_zip_members(raw_package)
    content_types_part = _required_part(payloads, "[Content_Types].xml")
    content_types_root = _parse_xml(content_types_part, "[Content_Types].xml")
    content_types = _validate_content_types(content_types_root, payloads)
    _validate_part_content_types(payloads, content_types)

    xml_parts: dict[str, etree._Element] = {"[Content_Types].xml": content_types_root}
    element_count = _validate_xml_tree(content_types_root, "[Content_Types].xml", 0)
    for part_name, payload in payloads.items():
        if part_name == "[Content_Types].xml":
            continue
        content_type = content_types[part_name]
        if content_type not in _XML_CONTENT_TYPES:
            _validate_inert_binary_part(part_name, payload, content_type)
            continue
        root = _parse_xml(payload, part_name)
        element_count = _validate_xml_tree(root, part_name, element_count)
        _validate_supported_profile(root, part_name)
        xml_parts[part_name] = root

    relationships, relationship_parts, relationship_ids = _validate_relationships(
        xml_parts,
        payloads,
        content_types,
    )
    _validate_xml_relationship_references(xml_parts, relationship_ids)
    document_part = _validate_root_relationship(relationships, content_types)
    _validate_document_profile(xml_parts, content_types, document_part)
    _validate_reachable_parts(payloads, relationships, relationship_parts, document_part)
    return DocxPackage(
        xml_parts=xml_parts,
        document_part=document_part,
        _relationships=relationships,
    )


def _read_bounded_package(path: Path) -> bytes:
    try:
        path_stat = path.stat()
        if not stat.S_ISREG(path_stat.st_mode):
            raise AdapterError("DOCX artifact must be a regular file")
        if path_stat.st_size > DOCX_MAX_SOURCE_BYTES:
            raise AdapterError(
                f"DOCX artifact exceeds the {DOCX_MAX_SOURCE_BYTES}-byte raw input limit"
            )
        with path.open("rb") as package_file:
            package = package_file.read(DOCX_MAX_SOURCE_BYTES + 1)
    except AdapterError:
        raise
    except OSError as exc:
        raise AdapterError(f"Failed reading DOCX artifact {path} ({type(exc).__name__})") from exc
    if len(package) > DOCX_MAX_SOURCE_BYTES:
        raise AdapterError(
            f"DOCX artifact exceeds the {DOCX_MAX_SOURCE_BYTES}-byte raw input limit"
        )
    if not package:
        raise AdapterError("DOCX artifact is empty")
    if not package.startswith(b"PK\x03\x04"):
        raise AdapterError("DOCX artifact is not a ZIP package")
    return package


def _read_zip_members(package: bytes) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    aliases: dict[str, str] = {}
    total_expanded = 0
    try:
        with zipfile.ZipFile(io.BytesIO(package), mode="r") as archive:
            members = archive.infolist()
            if len(members) > DOCX_MAX_MEMBERS:
                raise AdapterError(f"DOCX package exceeds the {DOCX_MAX_MEMBERS}-member limit")
            for member in members:
                part_name = _validate_zip_member(member, aliases)
                if member.is_dir():
                    continue
                total_expanded += member.file_size
                if total_expanded > DOCX_MAX_EXPANDED_BYTES:
                    raise AdapterError(
                        "DOCX package exceeds the "
                        f"{DOCX_MAX_EXPANDED_BYTES}-byte total expansion limit"
                    )
                payload, actual_size = _read_zip_member(archive, member, total_expanded)
                if actual_size != member.file_size:
                    raise AdapterError(f"DOCX part {part_name!r} expanded to an unexpected size")
                payloads[part_name] = payload
    except AdapterError:
        raise
    except (EOFError, NotImplementedError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise AdapterError(
            f"DOCX artifact has an invalid ZIP package ({type(exc).__name__})"
        ) from exc
    if not payloads:
        raise AdapterError("DOCX ZIP package contains no parts")
    return payloads


def _validate_zip_member(member: zipfile.ZipInfo, aliases: dict[str, str]) -> str:
    original_name = getattr(member, "orig_filename", member.filename)
    path_to_validate = original_name[:-1] if member.is_dir() else original_name
    part_name = _normalize_member_name(path_to_validate, "ZIP member")
    alias = _part_alias(part_name)
    previous = aliases.get(alias)
    if previous is not None:
        raise AdapterError(
            f"DOCX package contains duplicate or aliased parts {previous!r} and {original_name!r}"
        )
    aliases[alias] = original_name
    mode = member.external_attr >> 16
    if stat.S_IFMT(mode) == stat.S_IFLNK:
        raise AdapterError(f"DOCX ZIP member {original_name!r} is a symbolic link")
    if member.flag_bits & 0x1:
        raise AdapterError(f"DOCX ZIP member {original_name!r} is encrypted")
    if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
        raise AdapterError(
            f"DOCX ZIP member {original_name!r} uses an unsupported compression method"
        )
    if member.is_dir() and (member.file_size != 0 or member.CRC != 0):
        raise AdapterError(f"DOCX ZIP directory member {original_name!r} contains payload data")
    if member.file_size > DOCX_MAX_PART_BYTES:
        raise AdapterError(
            f"DOCX part {original_name!r} exceeds the {DOCX_MAX_PART_BYTES}-byte part limit"
        )
    if member.file_size and (
        member.compress_size == 0
        or member.file_size / member.compress_size > DOCX_MAX_COMPRESSION_RATIO
    ):
        raise AdapterError(
            f"DOCX part {original_name!r} exceeds the {DOCX_MAX_COMPRESSION_RATIO}:1 "
            "compression-ratio limit"
        )
    return part_name


def _read_zip_member(
    archive: zipfile.ZipFile,
    member: zipfile.ZipInfo,
    declared_total: int,
) -> tuple[bytes, int]:
    chunks: list[bytes] = []
    actual_size = 0
    try:
        with archive.open(member, mode="r") as member_file:
            while chunk := member_file.read(64 * 1024):
                actual_size += len(chunk)
                if actual_size > DOCX_MAX_PART_BYTES:
                    raise AdapterError(
                        f"DOCX part {member.filename!r} exceeds the actual expanded-size limit"
                    )
                actual_total = declared_total - member.file_size + actual_size
                if actual_total > DOCX_MAX_EXPANDED_BYTES:
                    raise AdapterError("DOCX package exceeds the actual total expansion limit")
                chunks.append(chunk)
    except AdapterError:
        raise
    except (EOFError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise AdapterError(
            f"DOCX part {member.filename!r} failed CRC or expanded-size validation"
        ) from exc
    return b"".join(chunks), actual_size


def _normalize_member_name(value: str, context: str) -> str:
    if not value or value.startswith(("/", "\\")):
        raise AdapterError(f"DOCX {context} has an unsafe absolute or empty path")
    if "\\" in value or "\x00" in value or _PERCENT_ESCAPE_PATTERN.search(value):
        raise AdapterError(f"DOCX {context} has an unsafe path {value!r}")
    try:
        decoded = unquote_to_bytes(value).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AdapterError(f"DOCX {context} has a non-UTF-8 part name") from exc
    decoded = unicodedata.normalize("NFC", decoded)
    if (
        decoded.startswith("/")
        or "\\" in decoded
        or "\x00" in decoded
        or "?" in decoded
        or "#" in decoded
    ):
        raise AdapterError(f"DOCX {context} has an unsafe path {value!r}")
    segments = decoded.split("/")
    if any(
        not segment
        or segment in {".", ".."}
        or ":" in segment
        or any(ord(character) < 32 or ord(character) == 127 for character in segment)
        for segment in segments
    ):
        raise AdapterError(f"DOCX {context} has an unsafe traversal or aliased path {value!r}")
    return decoded


def _part_alias(part_name: str) -> str:
    return unicodedata.normalize("NFC", part_name).casefold()


def _required_part(payloads: dict[str, bytes], part_name: str) -> bytes:
    match = next((name for name in payloads if _part_alias(name) == _part_alias(part_name)), None)
    if match is None:
        raise AdapterError(f"DOCX package is missing required part {part_name!r}")
    if match != part_name:
        raise AdapterError(f"DOCX required part must use canonical name {part_name!r}")
    return payloads[match]


def _parse_xml(payload: bytes, part_name: str) -> etree._Element:
    if _XML_DECLARATION_PATTERN.search(payload):
        raise AdapterError(f"DOCX XML part {part_name!r} contains a forbidden DTD or entity")
    parser = etree.XMLParser(
        load_dtd=False,
        no_network=True,
        recover=False,
        remove_comments=True,
        remove_pis=True,
        resolve_entities=False,
        huge_tree=False,
    )
    try:
        root = etree.fromstring(payload, parser=parser)
    except (ValueError, etree.ParserError, etree.XMLSyntaxError) as exc:
        raise AdapterError(f"DOCX XML part {part_name!r} is malformed") from exc
    document_info = root.getroottree().docinfo
    if document_info.doctype or document_info.internalDTD is not None:
        raise AdapterError(f"DOCX XML part {part_name!r} contains a forbidden DTD or entity")
    if any(isinstance(node, etree._Entity) for node in root.iter()):
        raise AdapterError(f"DOCX XML part {part_name!r} contains an unresolved entity")
    if not isinstance(root.tag, str):
        raise AdapterError(f"DOCX XML part {part_name!r} has no document element")
    return root


def _validate_xml_tree(root: etree._Element, part_name: str, current_count: int) -> int:
    element_count = current_count
    stack: list[tuple[etree._Element, int]] = [(root, 1)]
    while stack:
        element, depth = stack.pop()
        if not isinstance(element.tag, str):
            continue
        element_count += 1
        if element_count > DOCX_MAX_XML_ELEMENTS:
            raise AdapterError(
                f"DOCX XML exceeds the {DOCX_MAX_XML_ELEMENTS}-element cumulative limit"
            )
        if depth > DOCX_MAX_XML_DEPTH:
            raise AdapterError(
                f"DOCX XML part {part_name!r} exceeds the {DOCX_MAX_XML_DEPTH}-level depth limit"
            )
        stack.extend((child, depth + 1) for child in element if isinstance(child.tag, str))
    return element_count


def _validate_content_types(
    root: etree._Element,
    payloads: dict[str, bytes],
) -> dict[str, str]:
    if root.tag != f"{{{_CONTENT_TYPES_NS}}}Types":
        raise AdapterError("DOCX [Content_Types].xml has an invalid root element or namespace")
    defaults: dict[str, str] = {}
    overrides: dict[str, str] = {}
    for declaration in root:
        if not isinstance(declaration.tag, str):
            continue
        if declaration.tag == f"{{{_CONTENT_TYPES_NS}}}Default":
            extension = (declaration.get("Extension") or "").strip().lower()
            content_type = (declaration.get("ContentType") or "").strip().lower()
            if not extension or extension.startswith(".") or not extension.isalnum():
                raise AdapterError("DOCX content types contain an invalid default extension")
            if extension in defaults:
                raise AdapterError(f"DOCX content types duplicate extension {extension!r}")
            _validate_declared_content_type(content_type)
            defaults[extension] = content_type
        elif declaration.tag == f"{{{_CONTENT_TYPES_NS}}}Override":
            raw_part_name = (declaration.get("PartName") or "").strip()
            if not raw_part_name.startswith("/"):
                raise AdapterError(
                    "DOCX content-type override must use an absolute package part name"
                )
            part_name = _normalize_member_name(raw_part_name[1:], "content-type override")
            alias = _part_alias(part_name)
            if alias in overrides:
                raise AdapterError(f"DOCX content types duplicate part {part_name!r}")
            content_type = (declaration.get("ContentType") or "").strip().lower()
            _validate_declared_content_type(content_type)
            overrides[alias] = content_type
        else:
            raise AdapterError("DOCX content types contain an unsupported declaration")

    known_aliases = {_part_alias(part_name) for part_name in payloads}
    missing_overrides = sorted(set(overrides) - known_aliases)
    if missing_overrides:
        raise AdapterError("DOCX content types reference a missing package part")

    associations: dict[str, str] = {}
    for part_name in payloads:
        if part_name == "[Content_Types].xml":
            continue
        extension = part_name.rpartition(".")[2].lower()
        content_type = overrides.get(_part_alias(part_name), defaults.get(extension))
        if content_type is None:
            raise AdapterError(f"DOCX part {part_name!r} has no content-type association")
        associations[part_name] = content_type
    return associations


def _validate_declared_content_type(content_type: str) -> None:
    if not content_type:
        raise AdapterError("DOCX package declares an empty content type")
    if any(term in content_type for term in _DANGEROUS_CONTENT_TYPE_TERMS):
        raise AdapterError(f"DOCX package declares unsafe active content type {content_type!r}")
    if content_type not in _ALLOWED_CONTENT_TYPES:
        raise AdapterError(f"DOCX package declares unsupported content type {content_type!r}")


def _validate_part_content_types(
    payloads: dict[str, bytes],
    content_types: dict[str, str],
) -> None:
    for part_name, content_type in content_types.items():
        lowered_path = f"/{part_name.casefold()}/"
        if any(term.casefold() in lowered_path for term in _DANGEROUS_PART_TERMS):
            raise AdapterError(f"DOCX package contains unsafe active part {part_name!r}")
        if part_name.endswith(".rels"):
            if content_type != _RELATIONSHIPS_CONTENT_TYPE:
                raise AdapterError(
                    f"DOCX relationship part {part_name!r} has the wrong content type"
                )
        elif content_type == _RELATIONSHIPS_CONTENT_TYPE:
            raise AdapterError(f"DOCX non-relationship part {part_name!r} has a relationship type")
        elif content_type in _XML_CONTENT_TYPES and not part_name.lower().endswith(".xml"):
            raise AdapterError(f"DOCX XML content part {part_name!r} must use an .xml part name")
        elif content_type in _IMAGE_CONTENT_TYPES:
            extension = part_name.rpartition(".")[2].lower()
            if content_type not in _IMAGE_EXTENSIONS.get(extension, frozenset()):
                raise AdapterError(f"DOCX image part {part_name!r} conflicts with its content type")
        if part_name not in payloads:  # Defensive invariant for type checkers and future changes.
            raise AdapterError(f"DOCX content type references missing part {part_name!r}")


def _validate_inert_binary_part(part_name: str, payload: bytes, content_type: str) -> None:
    if content_type not in _IMAGE_CONTENT_TYPES:
        raise AdapterError(f"DOCX binary part {part_name!r} is not an allowed inert image")
    for signature, description in _ACTIVE_BINARY_SIGNATURES.items():
        if payload.startswith(signature):
            raise AdapterError(f"DOCX image part {part_name!r} disguises an {description}")


def _validate_supported_profile(root: etree._Element, part_name: str) -> None:
    field_instruction_fragments: list[str] = []

    def validate_instruction(fragments: list[str]) -> None:
        compact = re.sub(r"\s+", "", "".join(fragments)).upper()
        if _DANGEROUS_FIELD_PATTERN.search(compact) is not None:
            raise AdapterError(
                f"DOCX XML part {part_name!r} contains an unsafe active field instruction"
            )

    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        if _STRICT_NAMESPACE_FRAGMENT in element.tag or any(
            _STRICT_NAMESPACE_FRAGMENT in attribute for attribute in element.attrib
        ):
            raise AdapterError(
                f"DOCX XML part {part_name!r} uses the unsupported strict OOXML profile"
            )
        element_name = etree.QName(element.tag)
        if (
            element_name.namespace in _DANGEROUS_ELEMENT_NAMESPACES
            and element_name.localname.casefold() in _DANGEROUS_ELEMENT_NAMES
        ):
            raise AdapterError(
                f"DOCX XML part {part_name!r} contains unsafe active or embedded content"
            )
        if element.tag in {f"{{{WORD_NS}}}p", f"{{{WORD_NS}}}fldChar"}:
            validate_instruction(field_instruction_fragments)
            field_instruction_fragments.clear()
        if element.tag == f"{{{WORD_NS}}}instrText" and element.text:
            field_instruction_fragments.append(element.text)
        simple_instruction = element.get(f"{{{WORD_NS}}}instr")
        if simple_instruction:
            validate_instruction([simple_instruction])
    validate_instruction(field_instruction_fragments)


def _validate_relationships(
    xml_parts: dict[str, etree._Element],
    payloads: dict[str, bytes],
    content_types: dict[str, str],
) -> tuple[
    dict[str, tuple[_Relationship, ...]],
    dict[str, str],
    dict[str, frozenset[str]],
]:
    if "_rels/.rels" not in xml_parts:
        raise AdapterError("DOCX package is missing required root relationship part '_rels/.rels'")
    relationships: dict[str, tuple[_Relationship, ...]] = {}
    relationship_parts: dict[str, str] = {}
    relationship_ids: dict[str, frozenset[str]] = {}
    for part_name, root in xml_parts.items():
        if not part_name.endswith(".rels"):
            continue
        source_part = _source_for_relationship_part(part_name)
        if source_part and source_part not in payloads:
            raise AdapterError(
                f"DOCX relationship part {part_name!r} has missing source {source_part!r}"
            )
        if source_part and content_types[source_part] not in _XML_CONTENT_TYPES:
            raise AdapterError(f"DOCX relationship part {part_name!r} belongs to a non-XML source")
        relationship_parts[source_part] = part_name
        source_relationships, source_relationship_ids = _parse_relationships(
            root,
            part_name,
            source_part,
            payloads,
            content_types,
        )
        relationships[source_part] = source_relationships
        relationship_ids[source_part] = source_relationship_ids
    return relationships, relationship_parts, relationship_ids


def _source_for_relationship_part(part_name: str) -> str:
    if part_name == "_rels/.rels":
        return ""
    directory, separator, filename = part_name.rpartition("/")
    if not separator or not directory.endswith("/_rels") or not filename.endswith(".rels"):
        raise AdapterError(f"DOCX relationship part {part_name!r} has an invalid package location")
    source_directory = directory.removesuffix("/_rels")
    source_filename = filename.removesuffix(".rels")
    return f"{source_directory}/{source_filename}"


def _parse_relationships(
    root: etree._Element,
    relationship_part: str,
    source_part: str,
    payloads: dict[str, bytes],
    content_types: dict[str, str],
) -> tuple[tuple[_Relationship, ...], frozenset[str]]:
    if root.tag != f"{{{_PACKAGE_REL_NS}}}Relationships":
        raise AdapterError(
            f"DOCX relationship part {relationship_part!r} has an invalid root or namespace"
        )
    parsed: list[_Relationship] = []
    relationship_ids: set[str] = set()
    type_counts: dict[str, int] = {}
    for element in root:
        if not isinstance(element.tag, str):
            continue
        if element.tag != f"{{{_PACKAGE_REL_NS}}}Relationship":
            raise AdapterError(
                f"DOCX relationship part {relationship_part!r} has an unsupported element"
            )
        relationship_id = (element.get("Id") or "").strip()
        if _RELATIONSHIP_ID_PATTERN.fullmatch(relationship_id) is None:
            raise AdapterError(f"DOCX relationship has invalid Id {relationship_id!r}")
        if relationship_id in relationship_ids:
            raise AdapterError(f"DOCX relationship part duplicates Id {relationship_id!r}")
        relationship_ids.add(relationship_id)
        relationship_type = (element.get("Type") or "").strip()
        target = (element.get("Target") or "").strip()
        target_mode = (element.get("TargetMode") or "Internal").strip()
        if source_part == "" and relationship_type not in _ROOT_RELATIONSHIP_TYPES:
            raise AdapterError(
                f"DOCX package root contains unsupported relationship type {relationship_type!r}"
            )
        if source_part and relationship_type in _ROOT_RELATIONSHIP_TYPES:
            raise AdapterError(
                f"DOCX non-root part contains package-root relationship {relationship_type!r}"
            )
        if target_mode == "External":
            if relationship_type != _EXTERNAL_HYPERLINK_TYPE:
                raise AdapterError(
                    "DOCX package contains a forbidden non-hyperlink external relationship"
                )
            if content_types.get(source_part) not in _HYPERLINK_SOURCE_CONTENT_TYPES:
                raise AdapterError(
                    "DOCX external hyperlinks are only allowed from visible Word content parts"
                )
            if not target or any(ord(character) < 32 for character in target):
                raise AdapterError("DOCX external hyperlink has an invalid target")
            continue
        if target_mode != "Internal":
            raise AdapterError(f"DOCX relationship has invalid TargetMode {target_mode!r}")
        expected_content_types = _CONTENT_TYPE_BY_RELATIONSHIP.get(relationship_type)
        if expected_content_types is None:
            raise AdapterError(
                f"DOCX package contains unsupported relationship type {relationship_type!r}"
            )
        target_part = _resolve_relationship_target(source_part, target)
        matched_target = _match_part_name(payloads, target_part)
        if matched_target is None:
            raise AdapterError(
                f"DOCX relationship {relationship_id!r} targets missing part {target_part!r}"
            )
        target_content_type = content_types.get(matched_target)
        if target_content_type not in expected_content_types:
            raise AdapterError(
                f"DOCX relationship {relationship_id!r} target has incompatible content type"
            )
        type_counts[relationship_type] = type_counts.get(relationship_type, 0) + 1
        if (
            relationship_type in _SINGLETON_RELATIONSHIP_TYPES
            and type_counts[relationship_type] > 1
        ):
            raise AdapterError(
                f"DOCX source {source_part or '/'} has duplicate singleton relationships"
            )
        parsed.append(_Relationship(relationship_type, matched_target))
    return tuple(parsed), frozenset(relationship_ids)


def _validate_xml_relationship_references(
    xml_parts: dict[str, etree._Element],
    relationship_ids: dict[str, frozenset[str]],
) -> None:
    relationship_attributes = tuple(
        f"{{{REL_NS}}}{attribute_name}" for attribute_name in ("id", "embed", "link")
    )
    for part_name, root in xml_parts.items():
        if part_name == "[Content_Types].xml" or part_name.endswith(".rels"):
            continue
        available_ids = relationship_ids.get(part_name, frozenset())
        for element in root.iter():
            if not isinstance(element.tag, str):
                continue
            for attribute_name in relationship_attributes:
                referenced_id = element.get(attribute_name)
                if referenced_id is not None and referenced_id not in available_ids:
                    raise AdapterError(
                        f"DOCX XML part {part_name!r} references missing relationship Id "
                        f"{referenced_id!r}"
                    )


def _resolve_relationship_target(source_part: str, target: str) -> str:
    if not target or "?" in target or "#" in target or "\\" in target:
        raise AdapterError(f"DOCX relationship has unsafe internal target {target!r}")
    is_absolute = target.startswith("/")
    candidate = target[1:] if is_absolute else target
    if _PERCENT_ESCAPE_PATTERN.search(candidate):
        raise AdapterError(f"DOCX relationship has unsafe internal target {target!r}")
    try:
        decoded = unquote_to_bytes(candidate).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise AdapterError("DOCX relationship target is not valid UTF-8") from exc
    if "\\" in decoded or "\x00" in decoded or decoded.startswith("/"):
        raise AdapterError(f"DOCX relationship has unsafe internal target {target!r}")
    base_directory = "" if is_absolute else posixpath.dirname(source_part)
    resolved = posixpath.normpath(posixpath.join(base_directory, decoded))
    if resolved in {"", ".", ".."} or resolved.startswith("../"):
        raise AdapterError(f"DOCX relationship target traverses outside the package: {target!r}")
    return _normalize_member_name(resolved, "relationship target")


def _match_part_name(payloads: dict[str, bytes], requested: str) -> str | None:
    alias = _part_alias(requested)
    return next((part_name for part_name in payloads if _part_alias(part_name) == alias), None)


def _validate_root_relationship(
    relationships: dict[str, tuple[_Relationship, ...]],
    content_types: dict[str, str],
) -> str:
    office_document_type = f"{_OFFICE_REL_PREFIX}officeDocument"
    document_targets = [
        relationship.target_part
        for relationship in relationships.get("", ())
        if relationship.relationship_type == office_document_type
    ]
    if len(document_targets) != 1:
        raise AdapterError("DOCX root relationships must contain exactly one officeDocument target")
    document_part = document_targets[0]
    if content_types.get(document_part) != _MAIN_CONTENT_TYPE:
        raise AdapterError(
            "DOCX officeDocument target does not have the required main content type"
        )
    main_parts = [
        part_name
        for part_name, content_type in content_types.items()
        if content_type == _MAIN_CONTENT_TYPE
    ]
    if main_parts != [document_part]:
        raise AdapterError("DOCX package must contain exactly one related main document part")
    return document_part


def _validate_document_profile(
    xml_parts: dict[str, etree._Element],
    content_types: dict[str, str],
    document_part: str,
) -> None:
    document = xml_parts.get(document_part)
    if document is None:
        raise AdapterError("DOCX main document part is not XML")
    for part_name, content_type in content_types.items():
        expected_root = _EXPECTED_ROOT_BY_CONTENT_TYPE.get(content_type)
        if expected_root is None:
            continue
        root = xml_parts.get(part_name)
        if root is None or root.tag != expected_root:
            if content_type == _MAIN_CONTENT_TYPE:
                raise AdapterError(
                    "DOCX main document uses an unsupported namespace or OOXML profile"
                )
            raise AdapterError(f"DOCX XML part {part_name!r} has an invalid root element")
    bodies = document.findall(f"{{{WORD_NS}}}body")
    if len(bodies) != 1:
        raise AdapterError("DOCX main document must contain exactly one WordprocessingML body")


def _validate_reachable_parts(
    payloads: dict[str, bytes],
    relationships: dict[str, tuple[_Relationship, ...]],
    relationship_parts: dict[str, str],
    document_part: str,
) -> None:
    reachable = {document_part}
    queue = [document_part]
    for relationship in relationships.get("", ()):
        reachable.add(relationship.target_part)
        if relationship.target_part != document_part:
            queue.append(relationship.target_part)
    while queue:
        source = queue.pop()
        for relationship in relationships.get(source, ()):
            if relationship.target_part not in reachable:
                reachable.add(relationship.target_part)
                queue.append(relationship.target_part)

    for source_part, relationship_part in relationship_parts.items():
        if source_part and source_part not in reachable:
            raise AdapterError(
                f"DOCX package contains orphan relationship part {relationship_part!r}"
            )
    content_parts = {
        part_name
        for part_name in payloads
        if part_name != "[Content_Types].xml" and not part_name.endswith(".rels")
    }
    orphan_parts = sorted(content_parts - reachable)
    if orphan_parts:
        raise AdapterError(f"DOCX package contains orphan or unrelated part {orphan_parts[0]!r}")
