from __future__ import annotations

import codecs
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from email.message import Message
from pathlib import Path

from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterResult,
    CanonicalSourceUnit,
    ExtractorIdentity,
)
from newsrag.sources import TEXT_LINE_LOCATION_TYPE, TEXT_MAX_SOURCE_BYTES, TEXT_MEDIA_TYPE

MAX_TEXT_BYTES = TEXT_MAX_SOURCE_BYTES
MAX_TEXT_CHARS = 10 * 1024 * 1024
MAX_TEXT_LINES = 100_000
TEXT_EXTRACTOR = ExtractorIdentity("plain-text", "1")
_ALLOWED_ENCODINGS = frozenset(
    {"ascii", "utf-8", "utf-16", "utf-16-le", "utf-16-be", "cp1252", "iso8859-1"}
)
_DOCUMENT_SIGNATURE = re.compile(r"^(?:%PDF-|<!doctype\s+html\b|<html(?:\s|>)|<\?xml\b)", re.I)
_BINARY_SIGNATURES = (
    b"PK\x03\x04",
    b"PK\x05\x06",
    b"PK\x07\x08",
    b"\x89PNG",
    b"GIF87a",
    b"GIF89a",
    b"\xff\xd8\xff",
    b"\xd0\xcf\x11\xe0",
    b"\x7fELF",
    b"\x1f\x8b",
    b"RIFF",
)


@dataclass(frozen=True)
class PlainTextSourceAdapter:
    """Decode bounded literal text into original, one-based physical lines."""

    @property
    def media_types(self) -> Sequence[str]:
        return (TEXT_MEDIA_TYPE,)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        if artifact.media_type.partition(";")[0].strip().lower() != TEXT_MEDIA_TYPE:
            raise AdapterError("Plain-text adapter requires text/plain media type")
        lines, encoding = read_text_lines(artifact.artifact_path, artifact.media_type)
        units = tuple(
            CanonicalSourceUnit(
                ordinal=number,
                location_type=TEXT_LINE_LOCATION_TYPE,
                location={"line_start": number, "line_end": number},
                human_label=f"line {number}",
                normalized_text=line,
                structure={"kind": "text_line"},
                extractor=TEXT_EXTRACTOR,
            )
            for number, line in enumerate(lines, start=1)
        )
        return AdapterResult(
            media_type=TEXT_MEDIA_TYPE,
            units=units,
            extractor=TEXT_EXTRACTOR,
            metadata_candidates={"text_encoding": encoding},
        )


def read_text_lines(
    path: Path, media_type: str, *, allow_markup: bool = False, allow_blank: bool = False
) -> tuple[list[str], str]:
    """Validate and decode physical lines for literal text or inert Markdown parsing."""

    raw = _read_text(path)
    if raw.startswith(_BINARY_SIGNATURES):
        raise AdapterError("Plain-text artifact has a non-text file signature")
    text, encoding = _decode_text(raw, media_type)
    if len(text) > MAX_TEXT_CHARS:
        raise AdapterError(f"Plain-text artifact exceeds the {MAX_TEXT_CHARS}-character limit")
    if _DOCUMENT_SIGNATURE.match(text.lstrip()) is not None and (
        not allow_markup or text.lstrip().upper().startswith("%PDF-")
    ):
        raise AdapterError("Plain-text artifact has a contradictory PDF/HTML/XML signature")
    for character in text:
        category = unicodedata.category(character)
        if (category == "Cc" and character not in "\t\r\n") or (
            category == "Cf" and character not in "\u200c\u200d"
        ):
            raise AdapterError("Plain-text artifact contains unsupported control characters")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if not allow_blank and not text.strip():
        raise AdapterError("Plain-text artifact contains no non-whitespace text")
    line_count = text.count("\n") + (not text.endswith("\n"))
    if line_count > MAX_TEXT_LINES:
        raise AdapterError(f"Plain-text artifact exceeds the {MAX_TEXT_LINES}-line limit")
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()  # A terminator does not add a phantom physical line.
    return lines, encoding


def _read_text(path: Path) -> bytes:
    try:
        if not path.is_file():
            raise AdapterError("Plain-text artifact must be a regular file")
        with path.open("rb") as stream:
            raw = stream.read(MAX_TEXT_BYTES + 1)
    except OSError as exc:
        raise AdapterError(f"Cannot read plain-text artifact ({type(exc).__name__})") from exc
    if len(raw) > MAX_TEXT_BYTES:
        raise AdapterError(f"Plain-text artifact exceeds the {MAX_TEXT_BYTES}-byte limit")
    if not raw:
        raise AdapterError("Plain-text artifact is empty")
    return raw


def _decode_text(raw: bytes, media_type: str) -> tuple[str, str]:
    message = Message()
    message["content-type"] = media_type
    declarations = set()
    for key, value in (message.get_params() or [])[1:]:
        if key.lower() != "charset":
            continue
        if not isinstance(value, str) or not value.strip():
            raise AdapterError("Plain-text artifact has an invalid charset declaration")
        try:
            encoding = codecs.lookup(value.strip()).name
        except LookupError as exc:
            raise AdapterError("Plain-text artifact declares an unsupported charset") from exc
        if encoding not in _ALLOWED_ENCODINGS:
            raise AdapterError("Plain-text artifact declares an unsupported charset")
        declarations.add(encoding)
    if len(declarations) > 1:
        raise AdapterError("Plain-text artifact declares conflicting charsets")
    declared = next(iter(declarations), None)
    bom = None
    decoder = None
    if raw.startswith((codecs.BOM_UTF32_LE, codecs.BOM_UTF32_BE)):
        raise AdapterError("UTF-32 plain-text artifacts are not supported")
    if raw.startswith(codecs.BOM_UTF8):
        bom, decoder = "utf-8", "utf-8-sig"
    elif raw.startswith(codecs.BOM_UTF16_LE):
        bom, decoder = "utf-16-le", "utf-16"
    elif raw.startswith(codecs.BOM_UTF16_BE):
        bom, decoder = "utf-16-be", "utf-16"
    if (
        bom
        and declared
        and not (bom == declared or declared == "utf-16" and bom.startswith("utf-16-"))
    ):
        raise AdapterError("Plain-text charset conflicts with its byte-order mark")
    encoding = bom or declared or "utf-8"
    if encoding.startswith("utf-16") and bom is None:
        raise AdapterError("UTF-16 plain-text artifacts require a byte-order mark")
    try:
        return raw.decode(decoder or encoding, errors="strict"), encoding
    except UnicodeDecodeError as exc:
        raise AdapterError(f"Plain-text artifact is not valid {encoding} text") from exc
