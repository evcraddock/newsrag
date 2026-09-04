from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass

from newsrag.sources import (
    HTML_BLOCK_LOCATION_TYPE,
    PAGE_LOCATION_TYPE,
    SOURCE_TYPE_HTML,
    SOURCE_TYPE_PDF,
    source_type_for_media_type,
)


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


def load_document_extent(
    connection: sqlite3.Connection,
    document_id: str,
) -> DocumentExtent:
    """Load a typed document extent from its immutable source units."""

    row = connection.execute(
        """
        SELECT source_artifacts.media_type
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
    else:
        raise SourceLocationError(f"Unsupported source type for document: {document_id}")

    extent_row = connection.execute(
        """
        SELECT COUNT(*), COALESCE(SUM(LENGTH(normalized_text)), 0)
        FROM source_units
        WHERE document_id = ? AND location_type = ?
        """,
        (document_id, location_type),
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
) -> ResolvedSourceRange:
    """Resolve and validate a typed source-unit range, optionally through a passage."""

    passage_text: str | None = None
    resolved_start_id = _optional_string(source_unit_start_id)
    resolved_end_id = _optional_string(source_unit_end_id)
    resolved_passage_id = _optional_string(passage_id)
    if resolved_passage_id is not None:
        passage_row = connection.execute(
            """
            SELECT document_id, source_unit_start_id, source_unit_end_id, text
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
        resolved_start_id = passage_start_id
        resolved_end_id = passage_end_id
        passage_text = str(passage_row[3])

    if resolved_start_id is None:
        raise SourceLocationError("Evidence requires a source-unit start ID or typed passage")
    if resolved_end_id is None:
        resolved_end_id = resolved_start_id

    unit_rows = connection.execute(
        """
        SELECT id, ordinal, location_type, location_json, structure_json
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

    start_ordinal = int(start_unit[1])
    end_ordinal = int(end_unit[1])
    if end_ordinal < start_ordinal:
        raise SourceLocationError("Evidence source-unit range is reversed")
    location_type = str(start_unit[2])
    if str(end_unit[2]) != location_type:
        raise SourceLocationError("Evidence source-unit range mixes location types")

    range_rows = connection.execute(
        """
        SELECT id, location_type, normalized_text
        FROM source_units
        WHERE document_id = ? AND ordinal BETWEEN ? AND ?
        ORDER BY ordinal ASC
        """,
        (document_id, start_ordinal, end_ordinal),
    ).fetchall()
    if not range_rows or any(str(row[1]) != location_type for row in range_rows):
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
            WHERE pages.document_id = ? AND source_units.ordinal BETWEEN ? AND ?
            ORDER BY source_units.ordinal ASC
            """,
            (document_id, start_ordinal, end_ordinal),
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
    )


def validate_evidence_quote(resolved: ResolvedSourceRange, quote: str) -> None:
    """Require one quote to occur in its cited canonical source or passage text."""

    normalized_quote = _normalize_for_match(quote)
    if not normalized_quote or normalized_quote not in _normalize_for_match(resolved.text):
        raise SourceLocationError("Evidence quote was not found in cited source text")


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


def _load_json_object(raw_value: object) -> dict[str, object]:
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
