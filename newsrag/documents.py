from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

DEFAULT_DOCUMENT_LIST_LIMIT = 50
MAX_DOCUMENT_LIST_LIMIT = 500


class DocumentError(Exception):
    """Raised when document inventory commands cannot complete."""


class DocumentNotFoundError(DocumentError):
    """Raised when a requested document does not exist."""


@dataclass(frozen=True)
class DocumentFilters:
    """Filters for document inventory listings."""

    body: str | None = None
    document_type: str | None = None
    jurisdiction: str | None = None
    source_url: str | None = None
    since: str | None = None
    until: str | None = None
    query: str | None = None


@dataclass(frozen=True)
class DocumentSummary:
    """One row in the document inventory."""

    id: str
    title: str | None
    source_path: str | None
    source_url: str | None
    metadata: dict[str, Any]
    page_count: int
    created_at: str


@dataclass(frozen=True)
class DocumentDetail:
    """Detailed read-only document metadata."""

    id: str
    title: str | None
    source_path: str | None
    source_url: str | None
    source_hash: str | None
    normalized_path: str | None
    metadata: dict[str, Any]
    page_count: int
    created_at: str


@dataclass(frozen=True)
class DocumentListPage:
    """One bounded page of document inventory results."""

    documents: tuple[DocumentSummary, ...]
    total: int
    limit: int
    offset: int
    filters: DocumentFilters


@dataclass(frozen=True)
class _QueryParts:
    where_sql: str
    parameters: tuple[object, ...]


def list_document_summaries(
    database_path: Path,
    *,
    filters: DocumentFilters | None = None,
    limit: int = DEFAULT_DOCUMENT_LIST_LIMIT,
    offset: int = 0,
) -> DocumentListPage:
    """List a bounded page of document summaries from durable storage."""

    _validate_pagination(limit=limit, offset=offset)
    resolved_filters = filters or DocumentFilters()
    _validate_filters(resolved_filters)
    query = _build_filter_query(resolved_filters)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        total_row = connection.execute(
            f"""
            SELECT COUNT(*) AS total
            FROM documents
            {query.where_sql}
            """,
            query.parameters,
        ).fetchone()
        rows = connection.execute(
            f"""
            SELECT
                documents.id,
                documents.source_path,
                documents.source_url,
                documents.title,
                documents.metadata_json,
                documents.created_at,
                COUNT(pages.id) AS page_count
            FROM documents
            LEFT JOIN pages ON pages.document_id = documents.id
            {query.where_sql}
            GROUP BY documents.id
            ORDER BY documents.created_at DESC, documents.id ASC
            LIMIT ? OFFSET ?
            """,
            (*query.parameters, limit, offset),
        ).fetchall()

    total = int(total_row["total"]) if total_row is not None else 0
    return DocumentListPage(
        documents=tuple(_row_to_summary(row) for row in rows),
        total=total,
        limit=limit,
        offset=offset,
        filters=resolved_filters,
    )


def get_document_detail(database_path: Path, document_id: str) -> DocumentDetail:
    """Return detailed metadata for one document."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                documents.id,
                documents.source_path,
                documents.source_url,
                documents.title,
                documents.source_hash,
                documents.normalized_path,
                documents.metadata_json,
                documents.created_at,
                COUNT(pages.id) AS page_count
            FROM documents
            LEFT JOIN pages ON pages.document_id = documents.id
            WHERE documents.id = ?
            GROUP BY documents.id
            """,
            (document_id,),
        ).fetchone()

    if row is None:
        raise DocumentNotFoundError(f"Unknown document: {document_id}")

    return DocumentDetail(
        id=str(row["id"]),
        title=_optional_string(row["title"]),
        source_path=_optional_string(row["source_path"]),
        source_url=_optional_string(row["source_url"]),
        source_hash=_optional_string(row["source_hash"]),
        normalized_path=_optional_string(row["normalized_path"]),
        metadata=_load_metadata(row["metadata_json"]),
        page_count=int(row["page_count"]),
        created_at=str(row["created_at"]),
    )


def format_document_list(page: DocumentListPage) -> str:
    """Format a document inventory page for terminal output."""

    lines = ["NewsRAG Documents"]
    if not page.documents:
        if page.total == 0:
            lines.append("documents: none")
        else:
            lines.append(
                f"documents: none at offset {page.offset}; total={page.total} limit={page.limit}"
            )
        return "\n".join(lines)

    shown = len(page.documents)
    lines.append(
        f"showing {shown} of {page.total} document(s); limit={page.limit} offset={page.offset}"
    )
    if page.offset + shown < page.total:
        lines.append("more: use --limit/--offset or filters to narrow results")

    for document in page.documents:
        metadata = document.metadata
        parts = [
            document.id,
            _display_value(document.title),
            f"meeting_date={_display_value(_metadata_string(metadata, 'meeting_date'))}",
            f"body={_display_value(_metadata_string(metadata, 'body'))}",
            f"document_type={_display_value(_metadata_string(metadata, 'document_type'))}",
            f"pages={document.page_count}",
            f"source={_display_value(_best_source(document.source_url, document.source_path, metadata))}",
            f"created_at={document.created_at}",
        ]
        lines.append(" | ".join(parts))

    return "\n".join(lines)


def format_document_detail(document: DocumentDetail) -> str:
    """Format detailed document metadata for terminal output."""

    lines = [
        "NewsRAG Document",
        f"id: {document.id}",
        f"title: {_display_value(document.title)}",
        f"created_at: {document.created_at}",
        f"pages: {document.page_count}",
        f"source_url: {_display_value(document.source_url)}",
        f"source_path: {_display_value(document.source_path)}",
        f"source_hash: {_display_value(document.source_hash)}",
        f"normalized_path: {_display_value(document.normalized_path)}",
        "metadata:",
    ]

    if not document.metadata:
        lines.append("  none")
        return "\n".join(lines)

    for key in sorted(document.metadata):
        value = document.metadata[key]
        lines.append(f"  {key}: {_format_metadata_value(value)}")

    return "\n".join(lines)


def _build_filter_query(filters: DocumentFilters) -> _QueryParts:
    clauses: list[str] = []
    parameters: list[object] = []

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

    query = _normalized_optional_string(filters.query)
    if query is not None:
        like_query = f"%{query.lower()}%"
        clauses.append(
            "("
            "lower(coalesce(documents.id, '')) LIKE ? OR "
            "lower(coalesce(documents.title, '')) LIKE ? OR "
            "lower(coalesce(documents.source_path, '')) LIKE ? OR "
            "lower(coalesce(documents.source_url, '')) LIKE ? OR "
            "lower(coalesce(json_extract(documents.metadata_json, '$.source_filename'), '')) LIKE ?"
            ")"
        )
        parameters.extend((like_query, like_query, like_query, like_query, like_query))

    where_sql = ""
    if clauses:
        where_sql = "WHERE " + " AND ".join(clauses)
    return _QueryParts(where_sql=where_sql, parameters=tuple(parameters))


def _validate_pagination(*, limit: int, offset: int) -> None:
    if limit < 1 or limit > MAX_DOCUMENT_LIST_LIMIT:
        raise DocumentError(f"--limit must be between 1 and {MAX_DOCUMENT_LIST_LIMIT}")
    if offset < 0:
        raise DocumentError("--offset must be zero or greater")


def _validate_filters(filters: DocumentFilters) -> None:
    since_date = _parse_filter_date(filters.since, option_name="--since")
    until_date = _parse_filter_date(filters.until, option_name="--until")
    if since_date is not None and until_date is not None and since_date > until_date:
        raise DocumentError("Invalid date range: --since must be on or before --until")


def _parse_filter_date(value: str | None, *, option_name: str) -> date | None:
    resolved_value = _normalized_optional_string(value)
    if resolved_value is None:
        return None
    try:
        return date.fromisoformat(resolved_value)
    except ValueError as exc:
        raise DocumentError(f"Invalid {option_name} date: expected YYYY-MM-DD") from exc


def _row_to_summary(row: sqlite3.Row) -> DocumentSummary:
    return DocumentSummary(
        id=str(row["id"]),
        title=_optional_string(row["title"]),
        source_path=_optional_string(row["source_path"]),
        source_url=_optional_string(row["source_url"]),
        metadata=_load_metadata(row["metadata_json"]),
        page_count=int(row["page_count"]),
        created_at=str(row["created_at"]),
    )


def _load_metadata(raw_metadata: object) -> dict[str, Any]:
    try:
        metadata = json.loads(str(raw_metadata))
    except json.JSONDecodeError:
        return {}
    if not isinstance(metadata, dict):
        return {}
    return metadata


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    return _optional_string(metadata.get(key))


def _best_source(
    source_url: str | None,
    source_path: str | None,
    metadata: dict[str, Any],
) -> str | None:
    return (
        source_url
        or source_path
        or _metadata_string(metadata, "stored_source_path")
        or _metadata_string(metadata, "source_filename")
    )


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


def _format_metadata_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)
