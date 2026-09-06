from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass

from lxml import etree  # type: ignore[import-untyped]

from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterResult,
    CanonicalSourceUnit,
    ExtractorIdentity,
)
from newsrag.docx_package import DOCX_PACKAGE_VERSION, WORD_NS, DocxPackage, load_docx_package
from newsrag.sources import DOCX_BLOCK_LOCATION_TYPE, DOCX_MEDIA_TYPE

DOCX_EXTRACTOR = ExtractorIdentity("docx-xml", "1")
MAX_DOCX_TEXT_CHARS = 10 * 1024 * 1024
MAX_DOCX_UNITS = 100_000
W = "{" + WORD_NS + "}"
_HEADING_STYLE = re.compile(r"heading\s*([1-9])$", re.I)


@dataclass(frozen=True)
class DocxSourceAdapter:
    """Extract bounded WordprocessingML evidence without opening Office or resources."""

    format_version: str = "1"
    package_version: str = DOCX_PACKAGE_VERSION
    max_text_chars: int = MAX_DOCX_TEXT_CHARS
    max_units: int = MAX_DOCX_UNITS
    max_output_chars: int = 20 * 1024 * 1024

    @property
    def media_types(self) -> Sequence[str]:
        return (DOCX_MEDIA_TYPE,)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        if artifact.media_type.partition(";")[0].strip().lower() != DOCX_MEDIA_TYPE:
            raise AdapterError("DOCX adapter requires the DOCX media type")
        package = load_docx_package(artifact.artifact_path)
        builder = _DocumentBuilder(package, self)
        body = package.xml_parts[package.document_part].find(W + "body")
        if body is None:
            raise AdapterError("DOCX document is missing its body")
        builder.blocks(body)
        if not any(unit.normalized_text.strip() for unit in builder.units):
            raise AdapterError(
                "DOCX document contains no text evidence (image OCR is not supported)"
            )
        return AdapterResult(
            media_type=DOCX_MEDIA_TYPE,
            units=tuple(builder.units),
            extractor=DOCX_EXTRACTOR,
            metadata_candidates=_metadata(package),
        )


class _DocumentBuilder:
    def __init__(self, package: DocxPackage, adapter: DocxSourceAdapter) -> None:
        self.package = package
        self.adapter = adapter
        self.units: list[CanonicalSourceUnit] = []
        self.headings: list[tuple[int, str]] = []
        self.paragraph_counts: dict[str, int] = {}
        self.table_count = 0
        self.text_chars = 0
        self.output_chars = 0
        self.emitted_notes: set[str] = set()
        styles = package.related_part(package.document_part, "styles")
        self.styles = _indexed_elements(styles, "style", "styleId")
        numbering = package.related_part(package.document_part, "numbering")
        self.numbers = _indexed_elements(numbering, "num", "numId")
        self.abstract_numbers = _indexed_elements(numbering, "abstractNum", "abstractNumId")
        footnotes = package.related_part(package.document_part, "footnotes")
        self.notes = _indexed_elements(footnotes, "footnote", "id")

    def blocks(self, parent: etree._Element, note_id: str | None = None) -> None:
        for child in parent:
            if child.tag == W + "p":
                self.paragraph(child, note_id)
            elif child.tag == W + "tbl":
                self.table(child, note_id)
            elif child.tag in {
                W + "sdt",
                W + "sdtContent",
                W + "customXml",
                W + "ins",
                W + "moveTo",
            }:
                self.blocks(child, note_id)
            elif child.tag in {
                W + "sectPr",
                W + "sdtPr",
                W + "sdtEndPr",
                W + "del",
                W + "moveFrom",
            }:
                continue
            elif child.tag in {
                W + "bookmarkStart",
                W + "bookmarkEnd",
                W + "proofErr",
                W + "permStart",
                W + "permEnd",
            }:
                continue
            else:
                raise AdapterError("DOCX body contains unsupported block structure")

    def paragraph(self, paragraph: etree._Element, note_id: str | None) -> None:
        key = note_id or "body"
        self.paragraph_counts[key] = self.paragraph_counts.get(key, 0) + 1
        number = self.paragraph_counts[key]
        text, references = _paragraph_text(paragraph)
        structure = self.paragraph_structure(paragraph)
        level = structure.get("heading_level")
        if note_id is None and isinstance(level, int):
            self.headings[:] = [(depth, title) for depth, title in self.headings if depth < level]
            if text.strip():
                self.headings.append((level, " ".join(text.split())))
        location: dict[str, object] = {"paragraph_number": number}
        label = f"paragraph {number}"
        if note_id is not None:
            location["footnote_id"] = note_id
            label = f"footnote ID {note_id}, {label}"
        self.append(text, location, label, structure)
        self.emit_notes(references, note_id)

    def paragraph_structure(self, paragraph: etree._Element) -> dict[str, object]:
        properties = paragraph.find(W + "pPr")
        style_id = _val(properties, "pStyle")
        layers = [properties] if properties is not None else []
        seen: set[str] = set()
        style_names = [style_id] if style_id else []
        current = style_id
        while current is not None and current in self.styles:
            if current in seen:
                raise AdapterError("DOCX paragraph style inheritance is cyclic")
            if len(seen) >= 64:
                raise AdapterError("DOCX style inheritance exceeds the 64-level limit")
            seen.add(current)
            style = self.styles[current]
            style_names.append(_val(style, "name") or current)
            ppr = style.find(W + "pPr")
            if ppr is not None:
                layers.append(ppr)
            current = _val(style, "basedOn")
        structure: dict[str, object] = {"kind": "paragraph"}
        if style_id:
            structure["style_id"] = style_id
        outline = next(
            (value for layer in layers if (value := _val(layer, "outlineLvl")) is not None), None
        )
        if outline is not None:
            depth = _number(outline, "outline level", minimum=0, maximum=9)
            if depth < 9:
                structure.update(kind="heading", heading_level=depth + 1)
        elif style_id:
            for style_name in style_names:
                match = _HEADING_STYLE.fullmatch(style_name)
                if match:
                    structure.update(kind="heading", heading_level=int(match[1]))
                    break
        num_properties = [np for layer in layers if (np := layer.find(W + "numPr")) is not None]
        num_id = next(
            (value for np in num_properties if (value := _val(np, "numId")) is not None), None
        )
        level_value = next(
            (value for np in num_properties if (value := _val(np, "ilvl")) is not None), "0"
        )
        if num_id is not None and _number(num_id, "numbering ID", minimum=0) != 0:
            level = _number(level_value, "list level", minimum=0, maximum=8)
            num = self.numbers.get(num_id)
            abstract_id = _val(num, "abstractNumId")
            abstract = self.abstract_numbers.get(abstract_id or "")
            if num is None or abstract is None:
                raise AdapterError("DOCX list references missing numbering definitions")
            definition = next(
                (
                    node
                    for node in abstract.findall(W + "lvl")
                    if node.get(W + "ilvl") == str(level)
                ),
                None,
            )
            override = next(
                (
                    node
                    for node in num.findall(W + "lvlOverride")
                    if node.get(W + "ilvl") == str(level)
                ),
                None,
            )
            if override is not None and override.find(W + "lvl") is not None:
                definition = override.find(W + "lvl")
            if definition is None:
                raise AdapterError("DOCX list references a missing numbering level")
            structure.update(
                list_id=num_id,
                list_level=level,
                numbering_format=_val(definition, "numFmt") or "decimal",
                numbering_text=_val(definition, "lvlText") or "",
                numbering_start=_number(
                    _val(override, "startOverride") or _val(definition, "start") or "1",
                    "numbering start",
                    minimum=0,
                ),
            )
            if structure["kind"] != "heading":
                structure["kind"] = "list_item"
        return structure

    def table(self, table: etree._Element, note_id: str | None) -> None:
        self.table_count += 1
        number = self.table_count
        rows = table.findall(W + "tr")
        if not rows:
            raise AdapterError("DOCX table has no rows")
        for row_number, row in enumerate(rows, 1):
            cells: list[dict[str, object]] = []
            texts: list[str] = []
            references: list[str] = []
            column = 1 + _number(
                _val(row.find(W + "trPr"), "gridBefore") or "0",
                "table leading grid columns",
                minimum=0,
                maximum=10000,
            )
            for cell in row.findall(W + "tc"):
                text, refs = _cell_text(cell)
                references.extend(refs)
                texts.append(text)
                props = cell.find(W + "tcPr")
                span = _number(
                    _val(props, "gridSpan") or "1", "table grid span", minimum=1, maximum=10000
                )
                merge = props.find(W + "vMerge") if props is not None else None
                cells.append(
                    {
                        "column": column,
                        "grid_span": span,
                        "vertical_merge": (merge.get(W + "val") or "continue")
                        if merge is not None
                        else None,
                    }
                )
                column += span
            if not cells:
                raise AdapterError("DOCX table row has no cells")
            location: dict[str, object] = {"table_number": number, "row_number": row_number}
            label = f"table {number}, row {row_number}"
            if note_id is not None:
                location["footnote_id"] = note_id
                label = f"footnote ID {note_id}, {label}"
            self.append("\t".join(texts), location, label, {"kind": "table_row", "cells": cells})
            self.emit_notes(references, note_id)

    def emit_notes(self, references: list[str], note_id: str | None) -> None:
        if references and note_id is not None:
            raise AdapterError("DOCX footnotes cannot reference other footnotes")
        for reference in references:
            _number(reference, "footnote ID", minimum=0)
            if reference not in self.notes:
                raise AdapterError("DOCX paragraph references a missing footnote")
            if self.notes[reference].get(W + "type", "normal") != "normal":
                raise AdapterError("DOCX paragraph references a non-evidentiary footnote separator")
            if reference in self.emitted_notes:
                continue
            self.emitted_notes.add(reference)
            self.blocks(self.notes[reference], reference)

    def append(
        self, text: str, location: dict[str, object], label: str, structure: dict[str, object]
    ) -> None:
        self.text_chars += len(text)
        if self.text_chars > self.adapter.max_text_chars:
            raise AdapterError("DOCX extracted text exceeds the character limit")
        if len(self.units) >= self.adapter.max_units:
            raise AdapterError("DOCX document exceeds the source-unit limit")
        for character in text:
            if (
                unicodedata.category(character) in {"Cc", "Cf"}
                and character not in "\t\n\r\u200c\u200d"
            ):
                raise AdapterError("DOCX text contains unsupported control characters")
        ordinal = len(self.units) + 1
        path = [title for _, title in self.headings]
        full_location = {"block_number": ordinal, **location}
        full_structure = {**structure, "heading_path": path}
        human_label = " — ".join((*path, label))
        self.output_chars += (
            len(text)
            + len(human_label)
            + len(json.dumps([full_location, full_structure], ensure_ascii=False))
        )
        if self.output_chars > self.adapter.max_output_chars:
            raise AdapterError("DOCX canonical output exceeds the text and metadata limit")
        self.units.append(
            CanonicalSourceUnit(
                ordinal=ordinal,
                location_type=DOCX_BLOCK_LOCATION_TYPE,
                location=full_location,
                human_label=human_label,
                normalized_text=text,
                structure=full_structure,
                extractor=DOCX_EXTRACTOR,
            )
        )


def _paragraph_text(paragraph: etree._Element) -> tuple[str, list[str]]:
    parts: list[str] = []
    references: list[str] = []

    def walk(element: etree._Element) -> None:
        if element.tag in {
            W + "del",
            W + "moveFrom",
            W + "drawing",
            W + "pict",
            W + "pPr",
            W + "rPr",
            W + "instrText",
        }:
            return
        if element.tag == W + "t":
            parts.append(element.text or "")
        elif element.tag in {W + "tab", W + "ptab"}:
            parts.append("\t")
        elif element.tag in {W + "br", W + "cr"}:
            parts.append("\n")
        elif element.tag == W + "noBreakHyphen":
            parts.append("\u2011")
        elif element.tag == W + "softHyphen":
            parts.append("-")
        elif element.tag == W + "footnoteReference":
            reference = element.get(W + "id")
            if reference is None:
                raise AdapterError("DOCX footnote reference has no ID")
            references.append(reference)
            parts.append(f" [footnote ID {reference}]")
        elif element.tag == W + "endnoteReference":
            raise AdapterError("DOCX endnote extraction is not supported")
        else:
            for child in element:
                walk(child)

    walk(paragraph)
    return "".join(parts), references


def _cell_text(cell: etree._Element) -> tuple[str, list[str]]:
    texts: list[str] = []
    references: list[str] = []
    for element in cell:
        if element.tag == W + "p":
            text, refs = _paragraph_text(element)
            texts.append(text)
            references.extend(refs)
        elif element.tag == W + "tbl":
            for row in element.findall(W + "tr"):
                row_text: list[str] = []
                for nested_cell in row.findall(W + "tc"):
                    text, refs = _cell_text(nested_cell)
                    row_text.append(text)
                    references.extend(refs)
                texts.append("\t".join(row_text))
        elif element.tag in {W + "sdt", W + "sdtContent", W + "customXml"}:
            text, refs = _cell_text(element)
            texts.append(text)
            references.extend(refs)
        elif element.tag != W + "tcPr":
            raise AdapterError("DOCX table cell contains unsupported block structure")
    return "\n".join(texts), references


def _val(parent: etree._Element | None, name: str) -> str | None:
    child = parent.find(W + name) if parent is not None else None
    return child.get(W + "val") if child is not None else None


def _number(value: str, label: str, *, minimum: int, maximum: int = 2**31 - 1) -> int:
    try:
        number = int(value)
    except ValueError as exc:
        raise AdapterError(f"DOCX {label} is invalid") from exc
    if not minimum <= number <= maximum:
        raise AdapterError(f"DOCX {label} is out of range")
    return number


def _indexed_elements(
    root: etree._Element | None, tag: str, attribute: str
) -> dict[str, etree._Element]:
    result: dict[str, etree._Element] = {}
    if root is not None:
        for node in root.findall(W + tag):
            key = node.get(W + attribute)
            if key is None or key in result:
                raise AdapterError(f"DOCX {tag} has a missing or duplicate identifier")
            result[key] = node
    return result


def _metadata(package: DocxPackage) -> dict[str, str]:
    result: dict[str, str] = {}
    for root in package.xml_parts.values():
        if (
            root.tag
            != "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}coreProperties"
        ):
            continue
        for key, tag in {"title": "title", "author": "creator", "language": "language"}.items():
            value = root.findtext("{http://purl.org/dc/elements/1.1/}" + tag)
            if value and value.strip():
                normalized = " ".join(value.split())
                if len(normalized) > 4096 or any(
                    unicodedata.category(c) in {"Cc", "Cf"} for c in normalized
                ):
                    raise AdapterError("DOCX metadata contains unsupported content")
                result[key] = normalized
    return result
