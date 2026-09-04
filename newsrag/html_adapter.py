from __future__ import annotations

import codecs
import re
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from lxml import etree, html  # type: ignore[import-untyped]

from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterResult,
    CanonicalSourceUnit,
    ExtractorIdentity,
)
from newsrag.sources import HTML_BLOCK_LOCATION_TYPE, HTML_MAX_SOURCE_BYTES, HTML_MEDIA_TYPES

MAX_HTML_BYTES = HTML_MAX_SOURCE_BYTES
MAX_HTML_ELEMENTS = 100_000
MAX_HTML_NESTING_DEPTH = 256
MAX_EXTRACTED_TEXT_CHARS = 10 * 1024 * 1024
HTML_EXTRACTOR = ExtractorIdentity(name="static-html", version="1")

_EXCLUDED_TAGS = frozenset(
    {
        "applet",
        "audio",
        "base",
        "button",
        "canvas",
        "embed",
        "footer",
        "form",
        "frame",
        "frameset",
        "head",
        "header",
        "iframe",
        "input",
        "link",
        "nav",
        "noscript",
        "object",
        "script",
        "select",
        "source",
        "style",
        "svg",
        "template",
        "textarea",
        "video",
    }
)
_EXCLUDED_ROLES = frozenset(
    {"banner", "complementary", "contentinfo", "form", "navigation", "search"}
)
_EXCLUDED_CLASS_TOKENS = frozenset({"footer", "header", "nav", "navigation", "sidebar"})
_HEADING_TAGS = frozenset({f"h{level}" for level in range(1, 7)})
_ALLOWED_ENCODINGS = frozenset({"ascii", "cp1252", "iso8859-1", "utf-8", "utf-16-le", "utf-16-be"})
_CHARSET_PARAMETER = re.compile(r"(?:^|;)\s*charset\s*=\s*[\"']?([^;\s\"']+)", re.I)
_DECLARED_CHARSET = re.compile(rb"charset\s*=\s*[\"']?([A-Za-z0-9._-]+)", re.I)
_XML_ENCODING = re.compile(rb"^\s*<\?xml[^>]*\bencoding\s*=\s*[\"']([^\"']+)", re.I)
_XML_DECLARATION = re.compile(r"^\s*<\?xml[^>]*\?>", re.I)
_HTML_SIGNATURE = re.compile(r"^(?:<!doctype\s+html(?:\s[^>]*)?>\s*)?<html(?:\s|>)", re.I)
_EXTERNAL_DECLARATION = re.compile(
    r"<!\s*(?:entity\b|doctype\b[^>]*(?:system|public)\b)", re.I | re.S
)


@dataclass(frozen=True)
class _HtmlBlock:
    element_kind: str
    source_tag: str
    text: str
    heading_level: int | None = None


@dataclass(frozen=True)
class StaticHtmlSourceAdapter:
    """Extract deterministic evidence blocks from one preserved static HTML artifact."""

    @property
    def media_types(self) -> Sequence[str]:
        return HTML_MEDIA_TYPES

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        media_type = _normalized_media_type(artifact.media_type)
        raw_html = _read_bounded_artifact(artifact.artifact_path)
        decoded_html = _decode_html(raw_html, artifact.media_type)
        _validate_unsafe_declarations(decoded_html)
        _validate_signature(decoded_html)
        document = _parse_document(decoded_html, media_type)
        _validate_tree_limits(document)
        metadata_candidates = _extract_metadata_candidates(document)
        _remove_non_evidentiary_elements(document)
        content_root = _select_content_root(document)
        units = _build_source_units(content_root)
        if not units:
            raise AdapterError("HTML artifact contains no evidentiary content")
        return AdapterResult(
            media_type=media_type,
            units=units,
            extractor=HTML_EXTRACTOR,
            metadata_candidates=metadata_candidates,
        )


def _normalized_media_type(value: str) -> str:
    media_type = value.partition(";")[0].strip().lower()
    if media_type not in HTML_MEDIA_TYPES:
        accepted = ", ".join(HTML_MEDIA_TYPES)
        raise AdapterError(
            f"Static HTML adapter does not accept media type {value!r}; expected: {accepted}"
        )
    return media_type


def _read_bounded_artifact(path: Path) -> bytes:
    try:
        if path.stat().st_size > MAX_HTML_BYTES:
            raise AdapterError(f"HTML artifact exceeds the {MAX_HTML_BYTES}-byte input limit")
        raw_html = path.read_bytes()
    except AdapterError:
        raise
    except OSError as exc:
        raise AdapterError(f"Failed reading HTML artifact {path} ({type(exc).__name__})") from exc
    if len(raw_html) > MAX_HTML_BYTES:
        raise AdapterError(f"HTML artifact exceeds the {MAX_HTML_BYTES}-byte input limit")
    if not raw_html:
        raise AdapterError("HTML artifact is empty")
    return raw_html


def _decode_html(raw_html: bytes, media_type: str) -> str:
    bom_encoding, decoder = _bom_encoding(raw_html)
    declared_encodings = _declared_encodings(raw_html, media_type)
    if len(declared_encodings) > 1:
        raise AdapterError("HTML artifact declares conflicting encodings")
    declared_encoding = next(iter(declared_encodings), None)
    if bom_encoding is not None and declared_encoding is not None:
        if not _encodings_compatible(bom_encoding, declared_encoding):
            raise AdapterError(
                "HTML artifact encoding declaration conflicts with its byte-order mark"
            )
    encoding = bom_encoding or declared_encoding or "utf-8"
    if encoding.startswith("utf-16") and bom_encoding is None:
        raise AdapterError("UTF-16 HTML artifacts must include a byte-order mark")
    try:
        decoded = raw_html.decode(decoder or encoding, errors="strict")
    except (LookupError, UnicodeDecodeError) as exc:
        raise AdapterError(f"HTML artifact is not valid {encoding} text") from exc
    if "\x00" in decoded:
        raise AdapterError("HTML artifact contains unsafe null characters")
    return decoded


def _bom_encoding(raw_html: bytes) -> tuple[str | None, str | None]:
    if raw_html.startswith(codecs.BOM_UTF8):
        return "utf-8", "utf-8-sig"
    if raw_html.startswith(codecs.BOM_UTF16_LE):
        return "utf-16-le", "utf-16"
    if raw_html.startswith(codecs.BOM_UTF16_BE):
        return "utf-16-be", "utf-16"
    return None, None


def _declared_encodings(raw_html: bytes, media_type: str) -> set[str]:
    candidates: list[str] = []
    media_match = _CHARSET_PARAMETER.search(media_type)
    if media_match is not None:
        candidates.append(media_match.group(1))
    header = raw_html[:8192]
    xml_match = _XML_ENCODING.search(header)
    if xml_match is not None:
        candidates.append(xml_match.group(1).decode("ascii", errors="strict"))
    candidates.extend(
        match.decode("ascii", errors="strict") for match in _DECLARED_CHARSET.findall(header)
    )
    return {_normalize_encoding(candidate) for candidate in candidates}


def _normalize_encoding(value: str) -> str:
    try:
        normalized = codecs.lookup(value).name
    except LookupError as exc:
        raise AdapterError(f"HTML artifact declares unsupported encoding {value!r}") from exc
    if normalized not in _ALLOWED_ENCODINGS:
        raise AdapterError(f"HTML artifact declares unsupported encoding {value!r}")
    return normalized


def _encodings_compatible(first: str, second: str) -> bool:
    if first == second:
        return True
    return first.startswith("utf-16") and second.startswith("utf-16")


def _validate_signature(decoded_html: str) -> None:
    without_xml_declaration = _XML_DECLARATION.sub("", decoded_html, count=1).lstrip()
    if _HTML_SIGNATURE.match(without_xml_declaration) is None:
        raise AdapterError("HTML artifact has no conservative HTML document signature")


def _validate_unsafe_declarations(decoded_html: str) -> None:
    if _EXTERNAL_DECLARATION.search(decoded_html) is not None:
        raise AdapterError("HTML artifact contains an unsafe external declaration")


def _parse_document(decoded_html: str, media_type: str) -> etree._Element:
    source = _XML_DECLARATION.sub("", decoded_html, count=1)
    try:
        if media_type == "application/xhtml+xml":
            parser = etree.XMLParser(
                no_network=True,
                recover=False,
                resolve_entities=False,
                huge_tree=False,
                remove_comments=True,
                remove_pis=True,
            )
            document = etree.fromstring(source.encode("utf-8"), parser=parser)
        else:
            parser = etree.HTMLParser(
                no_network=True,
                recover=False,
                huge_tree=False,
                remove_comments=True,
                remove_pis=True,
            )
            document = html.document_fromstring(source, parser=parser)
    except (etree.ParserError, etree.XMLSyntaxError, ValueError) as exc:
        raise AdapterError("HTML artifact is malformed") from exc
    if _tag_name(document) != "html":
        raise AdapterError("HTML artifact root element must be html")
    return document


def _validate_tree_limits(document: etree._Element) -> None:
    element_count = 0
    stack: list[tuple[etree._Element, int]] = [(document, 1)]
    while stack:
        element, depth = stack.pop()
        if not isinstance(element.tag, str):
            continue
        element_count += 1
        if element_count > MAX_HTML_ELEMENTS:
            raise AdapterError(f"HTML artifact exceeds the {MAX_HTML_ELEMENTS}-element limit")
        if depth > MAX_HTML_NESTING_DEPTH:
            raise AdapterError(
                f"HTML artifact exceeds the {MAX_HTML_NESTING_DEPTH}-level nesting limit"
            )
        stack.extend((child, depth + 1) for child in element if isinstance(child.tag, str))


def _extract_metadata_candidates(document: etree._Element) -> dict[str, str]:
    candidates: dict[str, str] = {}
    html_element = next(
        (element for element in document.iter() if _tag_name(element) == "html"), None
    )
    if html_element is not None:
        language = html_element.get("lang") or html_element.get(
            "{http://www.w3.org/XML/1998/namespace}lang"
        )
        _set_metadata_candidate(candidates, "language", language)

    for element in document.iter():
        tag = _tag_name(element)
        if tag == "title" and "title" not in candidates:
            _set_metadata_candidate(candidates, "title", _normalized_element_text(element))
        elif tag == "meta":
            key = element.get("name") or element.get("property") or element.get("itemprop") or ""
            normalized_key = key.strip().lower()
            content = element.get("content")
            if normalized_key == "author" and "author" not in candidates:
                _set_metadata_candidate(candidates, "author", content)
            elif (
                normalized_key
                in {
                    "article:published_time",
                    "date",
                    "datepublished",
                    "publication_date",
                }
                and "publication_time" not in candidates
            ):
                _set_metadata_candidate(candidates, "publication_time", content)
        elif tag == "time" and "publication_time" not in candidates:
            itemprop = (element.get("itemprop") or "").strip().lower()
            if element.get("pubdate") is not None or itemprop == "datepublished":
                _set_metadata_candidate(candidates, "publication_time", element.get("datetime"))
    return candidates


def _set_metadata_candidate(candidates: dict[str, str], key: str, value: str | None) -> None:
    if value is None:
        return
    normalized = _normalize_text(value)
    if normalized:
        candidates[key] = normalized


def _remove_non_evidentiary_elements(document: etree._Element) -> None:
    for element in list(document.iter()):
        if element is document or not isinstance(element.tag, str):
            continue
        if _is_non_evidentiary(element):
            _remove_element_preserving_tail(element)


def _is_non_evidentiary(element: etree._Element) -> bool:
    if _tag_name(element) in _EXCLUDED_TAGS:
        return True
    if element.get("hidden") is not None:
        return True
    if (element.get("aria-hidden") or "").strip().lower() == "true":
        return True
    roles = {token.lower() for token in (element.get("role") or "").split()}
    if roles & _EXCLUDED_ROLES:
        return True
    class_tokens = {token.lower() for token in (element.get("class") or "").split()}
    return bool(class_tokens & _EXCLUDED_CLASS_TOKENS)


def _remove_element_preserving_tail(element: etree._Element) -> None:
    parent = element.getparent()
    if parent is None:
        return
    tail = element.tail
    previous = element.getprevious()
    parent.remove(element)
    if not tail:
        return
    if previous is not None:
        previous.tail = (previous.tail or "") + tail
    else:
        parent.text = (parent.text or "") + tail


def _select_content_root(document: etree._Element) -> etree._Element:
    elements = [element for element in document.iter() if isinstance(element.tag, str)]
    articles = [element for element in elements if _tag_name(element) == "article"]
    if len(articles) > 1:
        raise AdapterError("HTML artifact contains multiple article content roots")
    if articles:
        return articles[0]

    mains = [
        element
        for element in elements
        if _tag_name(element) == "main"
        or "main" in {token.lower() for token in (element.get("role") or "").split()}
    ]
    if len(mains) > 1:
        raise AdapterError("HTML artifact contains multiple main content roots")
    if mains:
        return mains[0]

    bodies = [element for element in elements if _tag_name(element) == "body"]
    if len(bodies) != 1:
        raise AdapterError("HTML artifact must contain one body content root")
    return bodies[0]


def _build_source_units(content_root: etree._Element) -> tuple[CanonicalSourceUnit, ...]:
    units: list[CanonicalSourceUnit] = []
    heading_levels: dict[int, str] = {}
    extracted_characters = 0
    for block in _iter_blocks(content_root):
        if block.heading_level is not None:
            heading_levels = {
                level: heading
                for level, heading in heading_levels.items()
                if level < block.heading_level
            }
            heading_levels[block.heading_level] = block.text
        heading_path = [heading_levels[level] for level in sorted(heading_levels)]
        extracted_characters += len(block.text)
        if extracted_characters > MAX_EXTRACTED_TEXT_CHARS:
            raise AdapterError(
                f"HTML extraction exceeds the {MAX_EXTRACTED_TEXT_CHARS}-character text limit"
            )
        ordinal = len(units) + 1
        structure: dict[str, object] = {
            "element_kind": block.element_kind,
            "source_tag": block.source_tag,
            "heading_path": heading_path,
        }
        if block.heading_level is not None:
            structure["heading_level"] = block.heading_level
        label_prefix = " — ".join(heading_path)
        human_label = f"{label_prefix} — block {ordinal}" if label_prefix else f"block {ordinal}"
        units.append(
            CanonicalSourceUnit(
                ordinal=ordinal,
                location_type=HTML_BLOCK_LOCATION_TYPE,
                location={"block_number": ordinal},
                human_label=human_label,
                normalized_text=block.text,
                structure=structure,
                extractor=HTML_EXTRACTOR,
            )
        )
    return tuple(units)


def _iter_blocks(element: etree._Element) -> Iterator[_HtmlBlock]:
    for child in element:
        if not isinstance(child.tag, str):
            continue
        tag = _tag_name(child)
        if tag in _HEADING_TAGS:
            yield from _single_block(child, "heading", heading_level=int(tag[1]))
        elif tag == "p":
            yield from _single_block(child, "paragraph")
        elif tag == "li":
            text = _normalize_text(_text_excluding_nested_lists(child))
            if text:
                yield _HtmlBlock("list_item", tag, text)
            for nested_list in _top_level_nested_lists(child):
                yield from _iter_blocks(nested_list)
        elif tag == "blockquote":
            yield from _single_block(child, "quotation")
        elif tag == "pre":
            text = _normalize_preformatted_text("".join(child.itertext()))
            if text:
                yield _HtmlBlock("preformatted", tag, text)
        elif tag in {"caption", "figcaption"}:
            yield from _single_block(child, "caption")
        elif tag == "tr":
            cells = [
                _normalized_element_text(cell) for cell in child if _tag_name(cell) in {"td", "th"}
            ]
            text = " | ".join(cell for cell in cells if cell)
            if text:
                yield _HtmlBlock("table_row", tag, text)
        else:
            yield from _iter_blocks(child)


def _single_block(
    element: etree._Element,
    element_kind: str,
    *,
    heading_level: int | None = None,
) -> Iterator[_HtmlBlock]:
    text = _normalized_element_text(element)
    if text:
        yield _HtmlBlock(element_kind, _tag_name(element), text, heading_level)


def _top_level_nested_lists(element: etree._Element) -> Iterator[etree._Element]:
    for child in element:
        if not isinstance(child.tag, str):
            continue
        if _tag_name(child) in {"ol", "ul"}:
            yield child
        else:
            yield from _top_level_nested_lists(child)


def _text_excluding_nested_lists(element: etree._Element) -> str:
    parts: list[str] = [element.text or ""]
    for child in element:
        if not isinstance(child.tag, str):
            continue
        if _tag_name(child) not in {"ol", "ul"}:
            parts.append(_text_excluding_nested_lists(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _normalized_element_text(element: etree._Element) -> str:
    return _normalize_text("".join(element.itertext()))


def _normalize_text(value: str) -> str:
    safe_value = "".join(
        character
        for character in value
        if character in {"\t", "\n", "\r"} or unicodedata.category(character) not in {"Cc", "Cf"}
    )
    return " ".join(safe_value.split())


def _normalize_preformatted_text(value: str) -> str:
    safe_value = "".join(
        character
        for character in value.replace("\r\n", "\n").replace("\r", "\n")
        if character in {"\t", "\n"} or unicodedata.category(character) not in {"Cc", "Cf"}
    )
    lines = [re.sub(r"[\t ]+", " ", line).strip() for line in safe_value.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines)


def _tag_name(element: etree._Element) -> str:
    if not isinstance(element.tag, str):
        return ""
    return str(etree.QName(element.tag).localname).lower()
