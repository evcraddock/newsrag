from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from newsrag.discovery import DiscoveryEvidenceRecord, DiscoveryItemRecord

DEFAULT_DISCOVERY_LIST_LIMIT = 50
MAX_DISCOVERY_LIST_LIMIT = 500
TOPIC_ITEM_TYPES = ("topic",)
ENTITY_ITEM_TYPES = ("entity",)
LEAD_ITEM_TYPES = ("story_lead",)
TIMELINE_ITEM_TYPES = ("date", "deadline", "action")


class DiscoveryBrowseError(Exception):
    """Raised when discovery browsing commands cannot complete."""


@dataclass(frozen=True)
class DiscoveryBrowseFilters:
    """Filters for corpus discovery browsing."""

    body: str | None = None
    document_type: str | None = None
    jurisdiction: str | None = None
    source_url: str | None = None
    since: str | None = None
    until: str | None = None
    item_type: str | None = None
    min_confidence: float | None = None


@dataclass(frozen=True)
class DiscoveryBrowseItem:
    """One discovery record with document metadata for browsing."""

    item: DiscoveryItemRecord
    document_title: str | None
    source_path: str | None
    source_url: str | None
    metadata: dict[str, Any]

    @property
    def meeting_date(self) -> str | None:
        """Return the document meeting date when present."""

        return _metadata_string(self.metadata, "meeting_date")

    @property
    def display_date(self) -> str | None:
        """Return the best date-like value for timeline display."""

        for key in ("date", "date_text", "deadline", "meeting_date"):
            value = _metadata_string(self.item.value, key)
            if value is not None:
                return value
        if self.item.item_type in {"date", "deadline"}:
            return self.item.label
        return self.meeting_date


@dataclass(frozen=True)
class DiscoveryBrowsePage:
    """A bounded page of discovery browsing results."""

    items: tuple[DiscoveryBrowseItem, ...]
    total: int
    limit: int
    offset: int
    filters: DiscoveryBrowseFilters


@dataclass(frozen=True)
class _QueryParts:
    where_sql: str
    parameters: tuple[object, ...]


@dataclass(frozen=True)
class _SelectParts:
    sql: str
    parameters: tuple[object, ...]


def list_browse_items(
    database_path: Path,
    *,
    item_types: tuple[str, ...],
    filters: DiscoveryBrowseFilters | None = None,
    limit: int = DEFAULT_DISCOVERY_LIST_LIMIT,
    offset: int = 0,
    order_by: str = "label",
) -> DiscoveryBrowsePage:
    """List a bounded page of discovery records with document metadata."""

    _validate_item_types(item_types)
    _validate_pagination(limit=limit, offset=offset)
    resolved_filters = filters or DiscoveryBrowseFilters()
    _validate_filters(resolved_filters)
    query = _build_filter_query(item_types=item_types, filters=resolved_filters)
    order_sql = _order_sql(order_by)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        total_row = connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM discovery_items
            JOIN documents ON documents.id = discovery_items.document_id
            {query.where_sql}
            """,
            query.parameters,
        ).fetchone()
        item_rows = connection.execute(
            f"""
            {_select_browse_columns()}
            FROM discovery_items
            JOIN documents ON documents.id = discovery_items.document_id
            {query.where_sql}
            {order_sql}
            LIMIT ? OFFSET ?
            """,
            (*query.parameters, limit, offset),
        ).fetchall()

        evidence_by_item_id = _load_evidence_for_rows(connection, item_rows)

    total = int(total_row["total"]) if total_row is not None else 0
    return DiscoveryBrowsePage(
        items=tuple(_row_to_browse_item(row, evidence_by_item_id) for row in item_rows),
        total=total,
        limit=limit,
        offset=offset,
        filters=resolved_filters,
    )


def get_browse_item(database_path: Path, item_id: str) -> DiscoveryBrowseItem:
    """Load one discovery record with evidence and document metadata."""

    resolved_item_id = _normalized_optional_string(item_id)
    if resolved_item_id is None:
        raise DiscoveryBrowseError("item_id must be non-empty")

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            {_select_browse_columns()}
            FROM discovery_items
            JOIN documents ON documents.id = discovery_items.document_id
            WHERE discovery_items.id = ?
            """,
            (resolved_item_id,),
        ).fetchall()
        evidence_by_item_id = _load_evidence_for_rows(connection, rows)

    if not rows:
        raise DiscoveryBrowseError(f"Unknown discovery item: {resolved_item_id}")
    return _row_to_browse_item(rows[0], evidence_by_item_id)


def format_topics_list(page: DiscoveryBrowsePage) -> str:
    """Format corpus topics for terminal output."""

    return _format_browse_list(page, title="NewsRAG Topics", empty_label="topics")


def format_entities_list(page: DiscoveryBrowsePage) -> str:
    """Format corpus entities for terminal output."""

    return _format_browse_list(page, title="NewsRAG Entities", empty_label="entities")


def format_leads_list(page: DiscoveryBrowsePage) -> str:
    """Format story leads for terminal output."""

    return _format_browse_list(page, title="NewsRAG Story Leads", empty_label="leads")


def format_timeline(page: DiscoveryBrowsePage) -> str:
    """Format timeline-oriented discovery items for terminal output."""

    return _format_browse_list(
        page,
        title="NewsRAG Timeline",
        empty_label="timeline items",
        include_display_date=True,
    )


def format_browse_detail(
    item: DiscoveryBrowseItem, *, title: str = "NewsRAG Discovery Item"
) -> str:
    """Format one discovery item with supporting evidence for terminal output."""

    record = item.item
    lines = [
        title,
        f"id: {record.id}",
        f"type: {record.item_type}",
        f"label: {record.label}",
        f"document_id: {record.document_id}",
        f"document_title: {_display_value(item.document_title)}",
        f"meeting_date: {_display_value(item.meeting_date)}",
        f"source: {_display_value(_best_source(item))}",
        f"confidence: {_format_confidence(record.confidence)}",
        f"summary: {_display_value(record.summary or None)}",
        "value:",
    ]
    if record.value:
        for key in sorted(record.value):
            lines.append(f"  {key}: {_format_json_value(record.value[key])}")
    else:
        lines.append("  none")

    lines.append("evidence:")
    if not record.evidence:
        lines.append("  none")
        return "\n".join(lines)

    for evidence in record.evidence:
        lines.append(f"  - {_format_evidence_reference(evidence)}")
        lines.append(f'    quote: "{evidence.quote}"')
    return "\n".join(lines)


def _format_browse_list(
    page: DiscoveryBrowsePage,
    *,
    title: str,
    empty_label: str,
    include_display_date: bool = False,
) -> str:
    lines = [title]
    if not page.items:
        if page.total == 0:
            lines.append(f"{empty_label}: none")
        else:
            lines.append(
                f"{empty_label}: none at offset {page.offset}; total={page.total} limit={page.limit}"
            )
        return "\n".join(lines)

    shown = len(page.items)
    lines.append(
        f"showing {shown} of {page.total} item(s); limit={page.limit} offset={page.offset}"
    )
    if page.offset + shown < page.total:
        lines.append("more: use --limit/--offset or filters to narrow results")

    for browse_item in page.items:
        record = browse_item.item
        parts = [
            record.id,
            record.item_type,
            record.label,
            f"confidence={_format_confidence(record.confidence)}",
            f"document={record.document_id}",
            f"title={_display_value(browse_item.document_title)}",
            f"meeting_date={_display_value(browse_item.meeting_date)}",
        ]
        if include_display_date:
            parts.insert(2, f"date={_display_value(browse_item.display_date)}")
        citation = _first_citation(record)
        if citation is not None:
            parts.append(f"citation={citation}")
        lines.append(" | ".join(parts))

    return "\n".join(lines)


def _build_filter_query(
    *,
    item_types: tuple[str, ...],
    filters: DiscoveryBrowseFilters,
) -> _QueryParts:
    clauses: list[str] = []
    parameters: list[object] = []

    placeholders = ", ".join("?" for _ in item_types)
    clauses.append(f"discovery_items.item_type IN ({placeholders})")
    parameters.extend(item_types)

    requested_item_type = _normalized_optional_string(filters.item_type)
    if requested_item_type is not None:
        if requested_item_type not in item_types:
            raise DiscoveryBrowseError(
                f"--item-type must be one of: {', '.join(sorted(item_types))}"
            )
        clauses.append("discovery_items.item_type = ?")
        parameters.append(requested_item_type)

    for key, value in (
        ("body", filters.body),
        ("document_type", filters.document_type),
        ("jurisdiction", filters.jurisdiction),
    ):
        resolved_value = _normalized_optional_string(value)
        if resolved_value is None:
            continue
        clauses.append(f"json_extract(documents.metadata_json, '$.{key}') = ?")
        parameters.append(resolved_value)

    source_url = _normalized_optional_string(filters.source_url)
    if source_url is not None:
        clauses.append(
            "(documents.source_url = ? OR json_extract(documents.metadata_json, '$.source_url') = ?)"
        )
        parameters.extend((source_url, source_url))

    since = _normalized_optional_string(filters.since)
    if since is not None:
        clauses.append("json_extract(documents.metadata_json, '$.meeting_date') >= ?")
        parameters.append(since)

    until = _normalized_optional_string(filters.until)
    if until is not None:
        clauses.append("json_extract(documents.metadata_json, '$.meeting_date') <= ?")
        parameters.append(until)

    if filters.min_confidence is not None:
        clauses.append("discovery_items.confidence IS NOT NULL")
        clauses.append("discovery_items.confidence >= ?")
        parameters.append(filters.min_confidence)

    return _QueryParts(where_sql="WHERE " + " AND ".join(clauses), parameters=tuple(parameters))


def _select_browse_columns() -> str:
    return """
        SELECT
            discovery_items.id,
            discovery_items.document_id,
            discovery_items.item_type,
            discovery_items.label,
            discovery_items.value_json,
            discovery_items.summary,
            discovery_items.confidence,
            discovery_items.extractor,
            discovery_items.provider,
            discovery_items.model,
            discovery_items.created_at,
            documents.title AS document_title,
            documents.source_path,
            documents.source_url,
            documents.metadata_json
        """


def _load_evidence_for_rows(
    connection: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> dict[str, list[DiscoveryEvidenceRecord]]:
    item_ids = tuple(str(row["id"]) for row in rows)
    if not item_ids:
        return {}

    placeholders = ", ".join("?" for _ in item_ids)
    evidence_rows = connection.execute(
        f"""
        SELECT
            id,
            item_id,
            document_id,
            page_id,
            passage_id,
            page_start,
            page_end,
            quote,
            validation_status,
            created_at
        FROM discovery_evidence
        WHERE item_id IN ({placeholders})
        ORDER BY created_at ASC, id ASC
        """,
        item_ids,
    ).fetchall()

    evidence_by_item_id: dict[str, list[DiscoveryEvidenceRecord]] = {}
    for row in evidence_rows:
        evidence = _row_to_evidence(row)
        evidence_by_item_id.setdefault(evidence.item_id, []).append(evidence)
    return evidence_by_item_id


def _row_to_browse_item(
    row: sqlite3.Row,
    evidence_by_item_id: dict[str, list[DiscoveryEvidenceRecord]],
) -> DiscoveryBrowseItem:
    item_id = str(row["id"])
    item = DiscoveryItemRecord(
        id=item_id,
        document_id=str(row["document_id"]),
        item_type=str(row["item_type"]),
        label=str(row["label"]),
        value=_load_json_object(row["value_json"]),
        summary=str(row["summary"]),
        confidence=float(row["confidence"]) if row["confidence"] is not None else None,
        extractor=str(row["extractor"]),
        provider=str(row["provider"]) if row["provider"] is not None else None,
        model=str(row["model"]) if row["model"] is not None else None,
        created_at=str(row["created_at"]),
        evidence=tuple(evidence_by_item_id.get(item_id, [])),
    )
    return DiscoveryBrowseItem(
        item=item,
        document_title=_optional_string(row["document_title"]),
        source_path=_optional_string(row["source_path"]),
        source_url=_optional_string(row["source_url"]),
        metadata=_load_json_object(row["metadata_json"]),
    )


def _row_to_evidence(row: sqlite3.Row) -> DiscoveryEvidenceRecord:
    return DiscoveryEvidenceRecord(
        id=str(row["id"]),
        item_id=str(row["item_id"]),
        document_id=str(row["document_id"]),
        page_id=str(row["page_id"]) if row["page_id"] is not None else None,
        passage_id=str(row["passage_id"]) if row["passage_id"] is not None else None,
        page_start=int(row["page_start"]),
        page_end=int(row["page_end"]),
        quote=str(row["quote"]),
        validation_status=str(row["validation_status"]),
        created_at=str(row["created_at"]),
    )


def _order_sql(order_by: str) -> str:
    if order_by == "timeline":
        return (
            "ORDER BY "
            "coalesce(json_extract(discovery_items.value_json, '$.date_text'), "
            "json_extract(discovery_items.value_json, '$.date'), "
            "json_extract(discovery_items.value_json, '$.deadline'), "
            "json_extract(documents.metadata_json, '$.meeting_date'), "
            "discovery_items.created_at) ASC, "
            "discovery_items.label ASC, discovery_items.id ASC"
        )
    if order_by == "created":
        return "ORDER BY discovery_items.created_at DESC, discovery_items.id ASC"
    return "ORDER BY lower(discovery_items.label) ASC, discovery_items.created_at DESC, discovery_items.id ASC"


def _validate_item_types(item_types: tuple[str, ...]) -> None:
    if not item_types:
        raise DiscoveryBrowseError("At least one item type is required")
    for item_type in item_types:
        if _normalized_optional_string(item_type) is None:
            raise DiscoveryBrowseError("Item types must be non-empty")


def _validate_pagination(*, limit: int, offset: int) -> None:
    if limit < 1 or limit > MAX_DISCOVERY_LIST_LIMIT:
        raise DiscoveryBrowseError(f"--limit must be between 1 and {MAX_DISCOVERY_LIST_LIMIT}")
    if offset < 0:
        raise DiscoveryBrowseError("--offset must be zero or greater")


def _validate_filters(filters: DiscoveryBrowseFilters) -> None:
    since_date = _parse_filter_date(filters.since, option_name="--since")
    until_date = _parse_filter_date(filters.until, option_name="--until")
    if since_date is not None and until_date is not None and since_date > until_date:
        raise DiscoveryBrowseError("Invalid date range: --since must be on or before --until")
    if filters.min_confidence is not None and not 0 <= filters.min_confidence <= 1:
        raise DiscoveryBrowseError("--min-confidence must be between 0 and 1")


def _parse_filter_date(value: str | None, *, option_name: str) -> date | None:
    resolved_value = _normalized_optional_string(value)
    if resolved_value is None:
        return None
    try:
        return date.fromisoformat(resolved_value)
    except ValueError as exc:
        raise DiscoveryBrowseError(f"Invalid {option_name} date: expected YYYY-MM-DD") from exc


def _first_citation(item: DiscoveryItemRecord) -> str | None:
    if not item.evidence:
        return None
    return _format_evidence_reference(item.evidence[0])


def _format_evidence_reference(evidence: DiscoveryEvidenceRecord) -> str:
    if evidence.page_start == evidence.page_end:
        page_label = f"p.{evidence.page_start}"
    else:
        page_label = f"pp.{evidence.page_start}-{evidence.page_end}"
    return f"{evidence.document_id} {page_label}"


def _best_source(item: DiscoveryBrowseItem) -> str | None:
    return (
        item.source_url
        or item.source_path
        or _metadata_string(item.metadata, "stored_source_path")
        or _metadata_string(item.metadata, "source_filename")
    )


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    return _optional_string(metadata.get(key))


def _load_json_object(value: object) -> dict[str, Any]:
    try:
        loaded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalized_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    resolved_value = value.strip()
    if not resolved_value:
        return None
    return resolved_value


def _display_value(value: str | None) -> str:
    if value is None:
        return "-"
    return value


def _format_confidence(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.2f}"


def _format_json_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)
