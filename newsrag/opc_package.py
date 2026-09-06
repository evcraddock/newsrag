from __future__ import annotations

import io
import posixpath
import re
import stat
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote_to_bytes

from lxml import etree  # type: ignore[import-untyped]

from newsrag.adapters import AdapterError

_XML_DECLARATION_PATTERN = re.compile(rb"<!\s*(?:DOCTYPE|ENTITY)\b", re.IGNORECASE)
_PERCENT_ESCAPE_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")


@dataclass(frozen=True)
class OpcReader:
    """Shared bounded OPC transport; format policy is owned by each caller.

    Construct per load so format-specific limits remain independently configurable.
    No filesystem extraction, entity expansion, recovery, or network access occurs.
    """

    format_name: str
    max_source_bytes: int
    max_members: int
    max_expanded_bytes: int
    max_part_bytes: int
    max_compression_ratio: int
    max_xml_elements: int
    max_xml_depth: int

    def read_bounded_package(self, path: Path) -> bytes:
        """Read at most the raw limit plus one byte, even if the file grows."""
        try:
            path_stat = path.stat()
            if not stat.S_ISREG(path_stat.st_mode):
                raise AdapterError(f"{self.format_name} artifact must be a regular file")
            if path_stat.st_size > self.max_source_bytes:
                raise AdapterError(
                    f"{self.format_name} artifact exceeds the {self.max_source_bytes}-byte raw input limit"
                )
            with path.open("rb") as package_file:
                package = package_file.read(self.max_source_bytes + 1)
        except AdapterError:
            raise
        except OSError as exc:
            raise AdapterError(
                f"Failed reading {self.format_name} artifact {path} ({type(exc).__name__})"
            ) from exc
        if len(package) > self.max_source_bytes:
            raise AdapterError(
                f"{self.format_name} artifact exceeds the {self.max_source_bytes}-byte raw input limit"
            )
        if not package:
            raise AdapterError(f"{self.format_name} artifact is empty")
        if not package.startswith(b"PK\x03\x04"):
            raise AdapterError(f"{self.format_name} artifact is not a ZIP package")
        return package

    def read_zip_members(self, package: bytes) -> dict[str, bytes]:
        """Read and CRC-check every ZIP part without filesystem extraction."""
        payloads: dict[str, bytes] = {}
        aliases: dict[str, str] = {}
        total_expanded = 0
        try:
            with zipfile.ZipFile(io.BytesIO(package), mode="r") as archive:
                members = archive.infolist()
                if len(members) > self.max_members:
                    raise AdapterError(
                        f"{self.format_name} package exceeds the {self.max_members}-member limit"
                    )
                for member in members:
                    part_name = self.validate_zip_member(member, aliases)
                    if member.is_dir():
                        continue
                    total_expanded += member.file_size
                    if total_expanded > self.max_expanded_bytes:
                        raise AdapterError(
                            f"{self.format_name} package exceeds the "
                            f"{self.max_expanded_bytes}-byte total expansion limit"
                        )
                    payload, actual_size = self.read_zip_member(archive, member, total_expanded)
                    if actual_size != member.file_size:
                        raise AdapterError(
                            f"{self.format_name} part {part_name!r} expanded to an unexpected size"
                        )
                    payloads[part_name] = payload
        except AdapterError:
            raise
        except (EOFError, NotImplementedError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
            raise AdapterError(
                f"{self.format_name} artifact has an invalid ZIP package ({type(exc).__name__})"
            ) from exc
        if not payloads:
            raise AdapterError(f"{self.format_name} ZIP package contains no parts")
        return payloads

    def validate_zip_member(self, member: zipfile.ZipInfo, aliases: dict[str, str]) -> str:
        """Validate names, aliases, ZIP flags, and declared expansion budgets."""
        original_name = getattr(member, "orig_filename", member.filename)
        path_to_validate = original_name[:-1] if member.is_dir() else original_name
        part_name = self.normalize_member_name(path_to_validate, "ZIP member")
        alias = self.part_alias(part_name)
        previous = aliases.get(alias)
        if previous is not None:
            raise AdapterError(
                f"{self.format_name} package contains duplicate or aliased parts {previous!r} and {original_name!r}"
            )
        aliases[alias] = original_name
        mode = member.external_attr >> 16
        if stat.S_IFMT(mode) == stat.S_IFLNK:
            raise AdapterError(
                f"{self.format_name} ZIP member {original_name!r} is a symbolic link"
            )
        if member.flag_bits & 0x1:
            raise AdapterError(f"{self.format_name} ZIP member {original_name!r} is encrypted")
        if member.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise AdapterError(
                f"{self.format_name} ZIP member {original_name!r} uses an unsupported compression method"
            )
        if member.is_dir() and (member.file_size != 0 or member.CRC != 0):
            raise AdapterError(
                f"{self.format_name} ZIP directory member {original_name!r} contains payload data"
            )
        if member.file_size > self.max_part_bytes:
            raise AdapterError(
                f"{self.format_name} part {original_name!r} exceeds the {self.max_part_bytes}-byte part limit"
            )
        if member.file_size and (
            member.compress_size == 0
            or member.file_size / member.compress_size > self.max_compression_ratio
        ):
            raise AdapterError(
                f"{self.format_name} part {original_name!r} exceeds the {self.max_compression_ratio}:1 "
                "compression-ratio limit"
            )
        return part_name

    def read_zip_member(
        self,
        archive: zipfile.ZipFile,
        member: zipfile.ZipInfo,
        declared_total: int,
    ) -> tuple[bytes, int]:
        """Bound actual expansion independently of the ZIP's declared sizes."""
        chunks: list[bytes] = []
        actual_size = 0
        try:
            with archive.open(member, mode="r") as member_file:
                while chunk := member_file.read(64 * 1024):
                    actual_size += len(chunk)
                    if actual_size > self.max_part_bytes:
                        raise AdapterError(
                            f"{self.format_name} part {member.filename!r} exceeds the actual expanded-size limit"
                        )
                    actual_total = declared_total - member.file_size + actual_size
                    if actual_total > self.max_expanded_bytes:
                        raise AdapterError(
                            f"{self.format_name} package exceeds the actual total expansion limit"
                        )
                    chunks.append(chunk)
        except AdapterError:
            raise
        except (EOFError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
            raise AdapterError(
                f"{self.format_name} part {member.filename!r} failed CRC or expanded-size validation"
            ) from exc
        return b"".join(chunks), actual_size

    def normalize_member_name(self, value: str, context: str) -> str:
        """Normalize UTF-8 URI escapes and reject unsafe package paths."""
        if not value or value.startswith(("/", "\\")):
            raise AdapterError(f"{self.format_name} {context} has an unsafe absolute or empty path")
        if "\\" in value or "\x00" in value or _PERCENT_ESCAPE_PATTERN.search(value):
            raise AdapterError(f"{self.format_name} {context} has an unsafe path {value!r}")
        try:
            decoded = unquote_to_bytes(value).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AdapterError(f"{self.format_name} {context} has a non-UTF-8 part name") from exc
        decoded = unicodedata.normalize("NFC", decoded)
        if (
            decoded.startswith("/")
            or "\\" in decoded
            or "\x00" in decoded
            or "?" in decoded
            or "#" in decoded
        ):
            raise AdapterError(f"{self.format_name} {context} has an unsafe path {value!r}")
        segments = decoded.split("/")
        if any(
            not segment
            or segment in {".", ".."}
            or ":" in segment
            or any(ord(character) < 32 or ord(character) == 127 for character in segment)
            for segment in segments
        ):
            raise AdapterError(
                f"{self.format_name} {context} has an unsafe traversal or aliased path {value!r}"
            )
        return decoded

    def part_alias(self, part_name: str) -> str:
        """Identify Unicode- and case-equivalent part names consistently."""
        return unicodedata.normalize("NFC", part_name).casefold()

    def required_part(self, payloads: dict[str, bytes], part_name: str) -> bytes:
        """Require a canonical control-part name rather than a case alias."""
        match = next(
            (name for name in payloads if self.part_alias(name) == self.part_alias(part_name)), None
        )
        if match is None:
            raise AdapterError(f"{self.format_name} package is missing required part {part_name!r}")
        if match != part_name:
            raise AdapterError(
                f"{self.format_name} required part must use canonical name {part_name!r}"
            )
        return payloads[match]

    def parse_xml(self, payload: bytes, part_name: str) -> etree._Element:
        """Parse strictly, forbidding DTDs/entities in any supported encoding."""
        if _XML_DECLARATION_PATTERN.search(payload):
            raise AdapterError(
                f"{self.format_name} XML part {part_name!r} contains a forbidden DTD or entity"
            )
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
            raise AdapterError(f"{self.format_name} XML part {part_name!r} is malformed") from exc
        document_info = root.getroottree().docinfo
        if document_info.doctype or document_info.internalDTD is not None:
            raise AdapterError(
                f"{self.format_name} XML part {part_name!r} contains a forbidden DTD or entity"
            )
        if any(isinstance(node, etree._Entity) for node in root.iter()):
            raise AdapterError(
                f"{self.format_name} XML part {part_name!r} contains an unresolved entity"
            )
        if not isinstance(root.tag, str):
            raise AdapterError(f"{self.format_name} XML part {part_name!r} has no document element")
        return root

    def validate_xml_tree(self, root: etree._Element, part_name: str, current_count: int) -> int:
        """Check tree depth and return the updated cumulative element count."""
        element_count = current_count
        stack: list[tuple[etree._Element, int]] = [(root, 1)]
        while stack:
            element, depth = stack.pop()
            if not isinstance(element.tag, str):
                continue
            element_count += 1
            if element_count > self.max_xml_elements:
                raise AdapterError(
                    f"{self.format_name} XML exceeds the {self.max_xml_elements}-element cumulative limit"
                )
            if depth > self.max_xml_depth:
                raise AdapterError(
                    f"{self.format_name} XML part {part_name!r} exceeds the {self.max_xml_depth}-level depth limit"
                )
            stack.extend((child, depth + 1) for child in element if isinstance(child.tag, str))
        return element_count

    def source_for_relationship_part(self, part_name: str) -> str:
        """Resolve a conventional nested relationship part's owning source."""
        if part_name == "_rels/.rels":
            return ""
        directory, separator, filename = part_name.rpartition("/")
        if not separator or not directory.endswith("/_rels") or not filename.endswith(".rels"):
            raise AdapterError(
                f"{self.format_name} relationship part {part_name!r} has an invalid package location"
            )
        source_directory = directory.removesuffix("/_rels")
        source_filename = filename.removesuffix(".rels")
        return f"{source_directory}/{source_filename}"

    def resolve_relationship_target(self, source_part: str, target: str) -> str:
        """Resolve an internal URI without allowing traversal outside the ZIP."""
        if not target or "?" in target or "#" in target or "\\" in target:
            raise AdapterError(
                f"{self.format_name} relationship has unsafe internal target {target!r}"
            )
        is_absolute = target.startswith("/")
        candidate = target[1:] if is_absolute else target
        if _PERCENT_ESCAPE_PATTERN.search(candidate):
            raise AdapterError(
                f"{self.format_name} relationship has unsafe internal target {target!r}"
            )
        try:
            decoded = unquote_to_bytes(candidate).decode("utf-8", errors="strict")
        except UnicodeDecodeError as exc:
            raise AdapterError(
                f"{self.format_name} relationship target is not valid UTF-8"
            ) from exc
        if "\\" in decoded or "\x00" in decoded or decoded.startswith("/"):
            raise AdapterError(
                f"{self.format_name} relationship has unsafe internal target {target!r}"
            )
        base_directory = "" if is_absolute else posixpath.dirname(source_part)
        resolved = posixpath.normpath(posixpath.join(base_directory, decoded))
        if resolved in {"", ".", ".."} or resolved.startswith("../"):
            raise AdapterError(
                f"{self.format_name} relationship target traverses outside the package: {target!r}"
            )
        return self.normalize_member_name(resolved, "relationship target")

    def match_part_name(self, payloads: dict[str, bytes], requested: str) -> str | None:
        """Find an existing part using the same aliases checked at ZIP loading."""
        alias = self.part_alias(requested)
        return next(
            (part_name for part_name in payloads if self.part_alias(part_name) == alias), None
        )
