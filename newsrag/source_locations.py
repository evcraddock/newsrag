from __future__ import annotations

import html
import json
import re
import sqlite3
from dataclasses import dataclass

from newsrag.sources import (
    DOCX_BLOCK_LOCATION_TYPE,
    HTML_BLOCK_LOCATION_TYPE,
    MARKDOWN_BLOCK_LOCATION_TYPE,
    PAGE_LOCATION_TYPE,
    SOURCE_TYPE_CSV,
    SOURCE_TYPE_DOCX,
    SOURCE_TYPE_HTML,
    SOURCE_TYPE_MARKDOWN,
    SOURCE_TYPE_PDF,
    SOURCE_TYPE_TEXT,
    TABLE_ROW_LOCATION_TYPE,
    TEXT_LINE_LOCATION_TYPE,
    source_type_for_media_type,
)
from newsrag.tabular import TableError, TableRegion
from newsrag.tabular_evidence import (
    TableEvidence,
    load_passage_table_evidence,
    validate_table_quote,
)
from newsrag.tabular_storage import resolve_table_region


class SourceLocationError(Exception):
    """Raised when typed source evidence cannot be resolved or validated."""


@dataclass(frozen=True)
class DocumentExtent:
    """Source-appropriate extent and text size for one document."""

    source_type: str
    extent_type: str
    extent_count: int
    text_length: int


@dataclass(frozen=True)
class ResolvedSourceRange:
    """Validated canonical source-unit range for one evidence reference."""

    document_id: str
    source_unit_start_id: str
    source_unit_end_id: str
    location_type: str
    location_label: str
    text: str
    page_id: str | None = None
    passage_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    processing_generation_id: str | None = None
    table_evidence: TableEvidence | None = None


@dataclass(frozen=True)
class _DocxLocation:
    """Validated machine and human location details for one DOCX block."""

    block_number: int
    kind: str
    paragraph_number: int | None = None
    table_number: int | None = None
    row_number: int | None = None
    footnote_id: str | None = None

    @property
    def label(self) -> str:
        if self.kind == "paragraph":
            return f"paragraph {self.paragraph_number}"
        if self.kind == "table_row":
            return f"table {self.table_number}, row {self.row_number}"
        if self.kind == "footnote_paragraph":
            return f"footnote ID {self.footnote_id}, paragraph {self.paragraph_number}"
        return f"footnote ID {self.footnote_id}, table {self.table_number}, row {self.row_number}"


def load_document_extent(
    connection: sqlite3.Connection,
    document_id: str,
) -> DocumentExtent:
    """Load a typed document extent from its immutable source units."""

    row = connection.execute(
        """
        SELECT
            source_artifacts.media_type,
            documents.current_processing_generation_id
        FROM documents
        JOIN source_artifacts ON source_artifacts.id = documents.artifact_id
        WHERE documents.id = ?
        """,
        (document_id,),
    ).fetchone()
    if row is None:
        raise SourceLocationError(f"Unknown document or published artifact: {document_id}")

    source_type = source_type_for_media_type(str(row[0]))
    if source_type == SOURCE_TYPE_PDF:
        location_type = PAGE_LOCATION_TYPE
        extent_type = "pages"
    elif source_type == SOURCE_TYPE_HTML:
        location_type = HTML_BLOCK_LOCATION_TYPE
        extent_type = "blocks"
    elif source_type == SOURCE_TYPE_TEXT:
        location_type = TEXT_LINE_LOCATION_TYPE
        extent_type = "lines"
    elif source_type == SOURCE_TYPE_MARKDOWN:
        location_type = MARKDOWN_BLOCK_LOCATION_TYPE
        extent_type = "lines"
    elif source_type == SOURCE_TYPE_DOCX:
        location_type = DOCX_BLOCK_LOCATION_TYPE
        extent_type = "blocks"
    elif source_type == SOURCE_TYPE_CSV:
        location_type = TABLE_ROW_LOCATION_TYPE
        extent_type = "rows"
    else:
        raise SourceLocationError(f"Unsupported source type for document: {document_id}")

    extent_expression = (
        "COALESCE(MAX(json_extract(location_json, '$.line_end')), 0)"
        if source_type == SOURCE_TYPE_MARKDOWN
        else "COUNT(*)"
    )
    extent_row = connection.execute(
        f"""
        SELECT {extent_expression}, COALESCE(SUM(LENGTH(normalized_text)), 0)
        FROM source_units
        WHERE document_id = ?
            AND location_type = ?
            AND processing_generation_id IS ?
        """,
        (document_id, location_type, row[1]),
    ).fetchone()
    extent_count = int(extent_row[0]) if extent_row is not None else 0
    text_length = int(extent_row[1]) if extent_row is not None else 0
    return DocumentExtent(
        source_type=source_type,
        extent_type=extent_type,
        extent_count=extent_count,
        text_length=text_length,
    )


def resolve_source_range(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    source_unit_start_id: str | None,
    source_unit_end_id: str | None = None,
    passage_id: str | None = None,
    processing_generation_id: str | None = None,
    table_region: TableRegion | None = None,
) -> ResolvedSourceRange:
    """Resolve and validate a typed source-unit range, optionally through a passage."""

    try:
        table_evidence = (
            load_passage_table_evidence(
                connection,
                passage_id=passage_id,
                document_id=document_id,
                generation_id=processing_generation_id,
            )
            if passage_id
            else None
        )
        if table_evidence is not None:
            focus = table_evidence.focus
            if table_region is not None and table_region != focus.region:
                raise TableError("Explicit selector does not match passage focus")
            if (
                source_unit_start_id is not None
                and source_unit_start_id != focus.source_unit_start_id
                or source_unit_end_id is not None
                and source_unit_end_id != focus.source_unit_end_id
            ):
                raise TableError("Table focus does not match supplied row endpoints")
            return _resolved_table_range(table_evidence, passage_id)
        if table_region is not None:
            generation = processing_generation_id
            if generation is None and source_unit_start_id:
                row = connection.execute(
                    "SELECT processing_generation_id FROM source_units WHERE id = ? AND document_id = ?",
                    (source_unit_start_id, document_id),
                ).fetchone()
                generation = str(row[0]) if row and row[0] is not None else None
            if generation is None:
                raise TableError(
                    "Table evidence requires an explicit generation or owned row anchor"
                )
            focus = resolve_table_region(
                connection,
                document_id=document_id,
                generation_id=generation,
                region=table_region,
                source_unit_start_id=source_unit_start_id,
                source_unit_end_id=source_unit_end_id,
            )
            if passage_id is not None:
                raise TableError("Passage has no persisted tabular selector")
            return _resolved_table_range(TableEvidence(focus), None)
    except TableError as exc:
        raise SourceLocationError(str(exc)) from exc
    passage_text: str | None = None
    resolved_start_id = _optional_string(source_unit_start_id)
    resolved_end_id = _optional_string(source_unit_end_id)
    resolved_passage_id = _optional_string(passage_id)
    resolved_generation_id = _optional_string(processing_generation_id)
    if resolved_passage_id is not None:
        passage_row = connection.execute(
            """
            SELECT
                document_id,
                source_unit_start_id,
                source_unit_end_id,
                text,
                processing_generation_id
            FROM passages
            WHERE id = ?
            """,
            (resolved_passage_id,),
        ).fetchone()
        if passage_row is None or str(passage_row[0]) != document_id:
            raise SourceLocationError("Evidence passage does not belong to the document")
        passage_start_id = _optional_string(passage_row[1])
        passage_end_id = _optional_string(passage_row[2]) or passage_start_id
        if passage_start_id is None or passage_end_id is None:
            raise SourceLocationError("Evidence passage has no typed source-unit range")
        if resolved_start_id is not None and resolved_start_id != passage_start_id:
            raise SourceLocationError("Evidence source-unit range does not match its passage")
        if resolved_end_id is not None and resolved_end_id != passage_end_id:
            raise SourceLocationError("Evidence source-unit range does not match its passage")
        passage_generation_id = _optional_string(passage_row[4])
        if resolved_generation_id is not None and resolved_generation_id != passage_generation_id:
            raise SourceLocationError("Evidence processing generation does not match its passage")
        resolved_start_id = passage_start_id
        resolved_end_id = passage_end_id
        resolved_generation_id = passage_generation_id
        passage_text = str(passage_row[3])

    if resolved_start_id is None:
        raise SourceLocationError("Evidence requires a source-unit start ID or typed passage")
    if resolved_end_id is None:
        resolved_end_id = resolved_start_id

    unit_rows = connection.execute(
        """
        SELECT
            id,
            ordinal,
            location_type,
            location_json,
            structure_json,
            processing_generation_id
        FROM source_units
        WHERE document_id = ? AND id IN (?, ?)
        """,
        (document_id, resolved_start_id, resolved_end_id),
    ).fetchall()
    units = {str(row[0]): row for row in unit_rows}
    start_unit = units.get(resolved_start_id)
    end_unit = units.get(resolved_end_id)
    if start_unit is None or end_unit is None:
        raise SourceLocationError("Evidence source units do not belong to the document")

    start_generation_id = _optional_string(start_unit[5])
    end_generation_id = _optional_string(end_unit[5])
    if start_generation_id != end_generation_id:
        raise SourceLocationError("Evidence source-unit range mixes processing generations")
    if resolved_generation_id is not None and resolved_generation_id != start_generation_id:
        raise SourceLocationError(
            "Evidence source units do not belong to the processing generation"
        )
    resolved_generation_id = start_generation_id

    start_ordinal = int(start_unit[1])
    end_ordinal = int(end_unit[1])
    if end_ordinal < start_ordinal:
        raise SourceLocationError("Evidence source-unit range is reversed")
    location_type = str(start_unit[2])
    if str(end_unit[2]) != location_type:
        raise SourceLocationError("Evidence source-unit range mixes location types")

    range_rows = connection.execute(
        """
        SELECT id, location_type, normalized_text, location_json, ordinal, structure_json
        FROM source_units
        WHERE document_id = ?
            AND processing_generation_id IS ?
            AND ordinal BETWEEN ? AND ?
        ORDER BY ordinal ASC
        """,
        (document_id, resolved_generation_id, start_ordinal, end_ordinal),
    ).fetchall()
    expected_unit_count = end_ordinal - start_ordinal + 1
    if len(range_rows) != expected_unit_count or any(
        str(row[1]) != location_type for row in range_rows
    ):
        raise SourceLocationError("Evidence source-unit range is incomplete")
    range_text = "\n".join(str(row[2]) for row in range_rows)

    page_id: str | None = None
    page_start: int | None = None
    page_end: int | None = None
    if location_type == PAGE_LOCATION_TYPE:
        page_start = _positive_location_number(start_unit[3], "page_number")
        page_end = _positive_location_number(end_unit[3], "page_number")
        if page_end < page_start:
            raise SourceLocationError("Evidence page range is reversed")
        page_rows = connection.execute(
            """
            SELECT pages.id, pages.page_number, pages.source_unit_id
            FROM pages
            JOIN source_units ON source_units.id = pages.source_unit_id
            WHERE pages.document_id = ?
                AND pages.processing_generation_id IS ?
                AND source_units.processing_generation_id IS ?
                AND source_units.ordinal BETWEEN ? AND ?
            ORDER BY source_units.ordinal ASC
            """,
            (
                document_id,
                resolved_generation_id,
                resolved_generation_id,
                start_ordinal,
                end_ordinal,
            ),
        ).fetchall()
        if len(page_rows) != len(range_rows):
            raise SourceLocationError("PDF source-unit range is missing linked page records")
        if int(page_rows[0][1]) != page_start or int(page_rows[-1][1]) != page_end:
            raise SourceLocationError("PDF source-unit locations do not match linked pages")
        location_label = (
            f"p. {page_start}" if page_start == page_end else f"pp. {page_start}-{page_end}"
        )
        if resolved_start_id == resolved_end_id:
            page_id = str(page_rows[0][0])
    elif location_type == HTML_BLOCK_LOCATION_TYPE:
        block_start = _positive_location_number(start_unit[3], "block_number")
        block_end = _positive_location_number(end_unit[3], "block_number")
        if block_end < block_start:
            raise SourceLocationError("Evidence HTML block range is reversed")
        structure = _load_json_object(start_unit[4])
        heading_path = _string_tuple(structure.get("heading_path"))
        block_label = (
            f"block {block_start}"
            if block_start == block_end
            else f"blocks {block_start}–{block_end}"
        )
        location_label = " — ".join((*heading_path, block_label))
    elif location_type == TEXT_LINE_LOCATION_TYPE:
        line_start = _text_line_number(start_unit[3])
        line_end = _text_line_number(end_unit[3])
        if line_end < line_start:
            raise SourceLocationError("Evidence text line range is reversed")
        location_label = _line_location_label(line_start, line_end)
    elif location_type == MARKDOWN_BLOCK_LOCATION_TYPE:
        line_ranges = [_markdown_line_range(row[3]) for row in range_rows]
        for previous, current in zip(line_ranges, line_ranges[1:], strict=False):
            if current[0] != previous[1] + 1:
                raise SourceLocationError(
                    "Evidence Markdown source-unit line ranges are not consecutive"
                )
        line_start = line_ranges[0][0]
        line_end = line_ranges[-1][1]
        structure = _load_json_object(start_unit[4])
        heading_path = _string_tuple(structure.get("heading_path"))
        location_label = " — ".join((*heading_path, _line_location_label(line_start, line_end)))
    elif location_type == DOCX_BLOCK_LOCATION_TYPE:
        docx_locations = []
        heading_paths = []
        for row in range_rows:
            ordinal = int(row[4])
            location = _docx_location(row[3])
            if location.block_number != ordinal:
                raise SourceLocationError("Evidence DOCX block_number does not match source order")
            docx_locations.append(location)
            heading_paths.append(_docx_heading_path(row[5], location=location))
        location_label = " — ".join(
            (
                *heading_paths[0],
                _format_docx_location_range(docx_locations[0], docx_locations[-1]),
            )
        )
    else:
        raise SourceLocationError(f"Unsupported evidence location type: {location_type}")

    return ResolvedSourceRange(
        document_id=document_id,
        source_unit_start_id=resolved_start_id,
        source_unit_end_id=resolved_end_id,
        location_type=location_type,
        location_label=location_label,
        text=passage_text or range_text,
        page_id=page_id,
        passage_id=resolved_passage_id,
        page_start=page_start,
        page_end=page_end,
        processing_generation_id=resolved_generation_id,
    )


def _resolved_table_range(evidence: TableEvidence, passage_id: str | None) -> ResolvedSourceRange:
    focus = evidence.focus
    return ResolvedSourceRange(
        document_id=focus.document_id,
        source_unit_start_id=focus.source_unit_start_id,
        source_unit_end_id=focus.source_unit_end_id,
        location_type="table_region",
        location_label=focus.region.label,
        text=focus.text,
        passage_id=passage_id,
        processing_generation_id=focus.processing_generation_id,
        table_evidence=evidence,
    )


def validate_evidence_quote(resolved: ResolvedSourceRange, quote: str) -> None:
    """Require one quote to occur in its cited canonical source or passage text."""

    if resolved.table_evidence is not None:
        try:
            validate_table_quote(resolved.table_evidence, quote)
        except TableError as exc:
            raise SourceLocationError(str(exc)) from exc
        return
    normalized_quote = _normalize_for_match(quote)
    if not normalized_quote or normalized_quote not in _normalize_for_match(resolved.text):
        raise SourceLocationError("Evidence quote was not found in cited source text")


def format_inert_tabular_text(value: str) -> str:
    """Entity-escape source punctuation without normalizing literal cell whitespace."""
    return "".join(
        character if character.isalnum() or character == " " else f"&#{ord(character)};"
        for character in value
    )


def format_inert_markdown_evidence(value: str) -> str:
    """Format untrusted source evidence as inert Markdown-visible text."""

    normalized = " ".join(value.split())
    escaped_html = html.escape(normalized, quote=False)
    escaped_inline = re.sub(r"([\\`*_{}\[\]()!])", r"\\\1", escaped_html)
    escaped_block = re.sub(r"^(\s*)([>#+\-~])", r"\1\\\2", escaped_inline)
    return re.sub(r"^(\s*\d+)\.", r"\1\\.", escaped_block)


def format_evidence_location(
    *,
    location_type: str,
    location_label: str,
    page_start: int | None,
    page_end: int | None,
    compact_pdf: bool = False,
) -> str:
    """Format a stored typed evidence location while preserving PDF styles."""

    if location_type != PAGE_LOCATION_TYPE:
        return location_label
    if page_start is None or page_end is None:
        return location_label
    separator = "" if compact_pdf else " "
    if page_start == page_end:
        return f"p.{separator}{page_start}"
    return f"pp.{separator}{page_start}-{page_end}"


def _positive_location_number(raw_json: object, key: str) -> int:
    value = _load_json_object(raw_json).get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SourceLocationError(f"Invalid source-unit {key}")
    return value


def _text_line_number(raw_json: object) -> int:
    line_start = _positive_location_number(raw_json, "line_start")
    line_end = _positive_location_number(raw_json, "line_end")
    if line_start != line_end:
        raise SourceLocationError("Text source units must identify one physical line")
    return line_start


def _markdown_line_range(raw_json: object) -> tuple[int, int]:
    line_start = _positive_location_number(raw_json, "line_start")
    line_end = _positive_location_number(raw_json, "line_end")
    if line_end < line_start:
        raise SourceLocationError("Markdown source-unit line range is reversed")
    return line_start, line_end


def _line_location_label(line_start: int, line_end: int) -> str:
    if line_start == line_end:
        return f"line {line_start}"
    return f"lines {line_start}–{line_end}"


def format_docx_location_range(start_location: object, end_location: object) -> str:
    """Validate and format one DOCX block range without inventing page locations."""

    return _format_docx_location_range(
        _docx_location(start_location),
        _docx_location(end_location),
    )


def _format_docx_location_range(start: _DocxLocation, end: _DocxLocation) -> str:
    if end.block_number < start.block_number:
        raise SourceLocationError("Evidence DOCX block range is reversed")
    if start.block_number == end.block_number:
        return start.label

    if start.kind == end.kind == "paragraph":
        return _numbered_docx_range(
            "paragraph", "paragraphs", start.paragraph_number, end.paragraph_number
        )
    if start.kind == end.kind == "table_row" and start.table_number == end.table_number:
        rows = _numbered_docx_range("row", "rows", start.row_number, end.row_number)
        return f"table {start.table_number}, {rows}"
    if start.kind == end.kind == "footnote_paragraph" and start.footnote_id == end.footnote_id:
        paragraphs = _numbered_docx_range(
            "paragraph", "paragraphs", start.paragraph_number, end.paragraph_number
        )
        return f"footnote ID {start.footnote_id}, {paragraphs}"
    if (
        start.kind == end.kind == "footnote_table_row"
        and start.footnote_id == end.footnote_id
        and start.table_number == end.table_number
    ):
        rows = _numbered_docx_range("row", "rows", start.row_number, end.row_number)
        return f"footnote ID {start.footnote_id}, table {start.table_number}, {rows}"
    return f"{start.label} – {end.label}"


def _numbered_docx_range(
    singular: str,
    plural: str,
    start: int | None,
    end: int | None,
) -> str:
    if start is None or end is None:  # pragma: no cover - protected by location validation
        raise SourceLocationError("Invalid DOCX source-unit location")
    if end < start:
        raise SourceLocationError(f"Evidence DOCX {singular} range is reversed")
    if start == end:
        return f"{singular} {start}"
    return f"{plural} {start}–{end}"


def _docx_location(raw_json: object) -> _DocxLocation:
    location = _load_json_object(raw_json)
    block_number = _positive_location_number(raw_json, "block_number")
    keys = set(location)
    footnote_id: str | None = None
    if "footnote_id" in location:
        raw_footnote_id = location["footnote_id"]
        if not isinstance(raw_footnote_id, str) or not raw_footnote_id.strip():
            raise SourceLocationError("Invalid source-unit footnote_id")
        footnote_id = raw_footnote_id.strip()

    if "paragraph_number" in location:
        expected_keys = {"block_number", "paragraph_number"}
        if footnote_id is not None:
            expected_keys.add("footnote_id")
        if keys != expected_keys:
            raise SourceLocationError("Invalid DOCX paragraph source-unit location")
        paragraph_number = _positive_location_number(raw_json, "paragraph_number")
        return _DocxLocation(
            block_number=block_number,
            kind="footnote_paragraph" if footnote_id is not None else "paragraph",
            paragraph_number=paragraph_number,
            footnote_id=footnote_id,
        )

    expected_keys = {"block_number", "table_number", "row_number"}
    if footnote_id is not None:
        expected_keys.add("footnote_id")
    if keys != expected_keys:
        raise SourceLocationError("Invalid DOCX table-row source-unit location")
    return _DocxLocation(
        block_number=block_number,
        kind="footnote_table_row" if footnote_id is not None else "table_row",
        table_number=_positive_location_number(raw_json, "table_number"),
        row_number=_positive_location_number(raw_json, "row_number"),
        footnote_id=footnote_id,
    )


def _docx_heading_path(
    raw_structure: object,
    *,
    location: _DocxLocation,
) -> tuple[str, ...]:
    structure = _load_json_object(raw_structure)
    kind = structure.get("kind")
    if not isinstance(kind, str) or not kind.strip():
        raise SourceLocationError("Invalid DOCX source-unit structure kind")
    expected_kinds = (
        {"table_row"}
        if location.kind in {"table_row", "footnote_table_row"}
        else {"heading", "list_item", "paragraph"}
    )
    if kind not in expected_kinds:
        raise SourceLocationError("DOCX source-unit structure kind conflicts with its location")
    heading_path = structure.get("heading_path")
    if not isinstance(heading_path, list) or any(
        not isinstance(heading, str) or not heading.strip() for heading in heading_path
    ):
        raise SourceLocationError("Invalid DOCX source-unit heading_path")
    return tuple(heading.strip() for heading in heading_path)


def _load_json_object(raw_value: object) -> dict[str, object]:
    if isinstance(raw_value, dict):
        return dict(raw_value)
    try:
        value = json.loads(str(raw_value))
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return value


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalize_for_match(value: str) -> str:
    return " ".join(value.casefold().split())
