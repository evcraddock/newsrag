from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from newsrag.search import SearchFilters, SearchResult
from newsrag.sources import SOURCE_TYPE_HTML, source_type_for_media_type


class PacketError(Exception):
    """Raised when a source packet cannot be generated or written."""


@dataclass(frozen=True)
class PacketSourceProvenance:
    """Durable source and immutable artifact identity for one packet document."""

    document_id: str
    source_type: str
    source_kind: str
    submitted_reference: str
    resolved_reference: str
    retrieved_at: str | None
    artifact_hash: str
    source_id: str | None = None
    revision_id: str | None = None
    revision_number: int | None = None
    is_current_snapshot: bool | None = None
    artifact_id: str | None = None
    acquired_at: str | None = None


def load_packet_source_provenance(
    database_path: Path,
    results: Sequence[SearchResult],
) -> dict[str, PacketSourceProvenance]:
    """Load authoritative source and artifact provenance for packet results."""

    document_ids = sorted({result.document_id for result in results})
    if not document_ids:
        return {}

    placeholders = ", ".join("?" for _ in document_ids)
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT
                documents.id AS document_id,
                source_revisions.id AS revision_id,
                source_revisions.source_id,
                source_revisions.revision_number,
                sources.kind AS source_kind,
                sources.submitted_reference AS submitted_reference,
                sources.resolved_reference AS resolved_reference,
                source_artifacts.id AS artifact_id,
                source_artifacts.media_type AS media_type,
                source_artifacts.content_hash AS artifact_hash,
                source_artifacts.acquired_at AS acquired_at,
                source_artifacts.provenance_json AS provenance_json
            FROM documents
            JOIN source_artifacts ON source_artifacts.id = documents.artifact_id
            JOIN source_revisions ON source_revisions.document_id = documents.id
            JOIN sources ON sources.id = source_revisions.source_id
            WHERE documents.id IN ({placeholders})
                AND source_artifacts.state = 'published'
            """,
            tuple(document_ids),
        ).fetchall()

    provenance: dict[str, PacketSourceProvenance] = {}
    for row in rows:
        document_id = str(row["document_id"])
        source_kind = str(row["source_kind"])
        source_type = source_type_for_media_type(str(row["media_type"]))
        if source_type is None or source_kind not in {"local_path", "url"}:
            raise PacketError(f"Unsupported source provenance for document: {document_id}")
        artifact_provenance = _load_json_object(row["provenance_json"])
        submitted_reference = str(row["submitted_reference"])
        if source_kind == "url":
            submitted_reference = (
                _optional_string(artifact_provenance.get("submitted_url")) or submitted_reference
            )
            resolved_reference = (
                _optional_string(artifact_provenance.get("resolved_url"))
                or _optional_string(row["resolved_reference"])
                or submitted_reference
            )
            retrieved_at = _optional_string(artifact_provenance.get("retrieved_at")) or str(
                row["acquired_at"]
            )
        else:
            submitted_reference = (
                _optional_string(artifact_provenance.get("submitted_path")) or submitted_reference
            )
            resolved_reference = (
                _optional_string(artifact_provenance.get("resolved_path"))
                or _optional_string(row["resolved_reference"])
                or submitted_reference
            )
            retrieved_at = None
        matching_result = next(result for result in results if result.document_id == document_id)
        revision_id = str(row["revision_id"])
        source_id = str(row["source_id"])
        if matching_result.revision_id is not None and matching_result.revision_id != revision_id:
            raise PacketError(f"Revision identity changed for packet document: {document_id}")
        if matching_result.source_id is not None and matching_result.source_id != source_id:
            raise PacketError(f"Source identity changed for packet document: {document_id}")
        revision_number = int(row["revision_number"])
        if (
            matching_result.revision_number is not None
            and matching_result.revision_number != revision_number
        ):
            raise PacketError(f"Revision number changed for packet document: {document_id}")
        provenance[document_id] = PacketSourceProvenance(
            document_id=document_id,
            source_type=source_type,
            source_kind=source_kind,
            submitted_reference=submitted_reference,
            resolved_reference=resolved_reference,
            retrieved_at=retrieved_at,
            artifact_hash=str(row["artifact_hash"]),
            source_id=source_id,
            revision_id=revision_id,
            revision_number=revision_number,
            is_current_snapshot=matching_result.is_current_snapshot,
            artifact_id=str(row["artifact_id"]),
            acquired_at=str(row["acquired_at"]),
        )

    missing_document_ids = sorted(set(document_ids) - provenance.keys())
    if missing_document_ids:
        raise PacketError(
            "Cannot load immutable source provenance for document(s): "
            + ", ".join(missing_document_ids)
        )
    return provenance


def format_source_packet(
    *,
    query: str,
    results: Sequence[SearchResult],
    filters: SearchFilters | None = None,
    source_provenance: Mapping[str, PacketSourceProvenance] | None = None,
    include_history: bool = False,
) -> str:
    """Format retrieved evidence as a fixed Markdown source packet."""

    resolved_filters = filters or SearchFilters()
    lines = [f"# Source Packet: {query}", ""]
    if resolved_filters.is_active:
        lines.extend([f"Filters: {', '.join(resolved_filters.labels())}", ""])

    lines.extend(["## Key Evidence", ""])
    if results:
        for index, result in enumerate(results, start=1):
            lines.extend(
                [
                    f"{index}. **{result.citation}**"
                    + (_history_label(result) if include_history else ""),
                    f"   > {_normalize_text(result.text)}",
                    "",
                ]
            )
    else:
        lines.extend(["No evidence found.", ""])

    lines.extend(["## Timeline", ""])
    dated_results = [result for result in results if result.meeting_date is not None]
    if dated_results:
        for result in sorted(
            dated_results, key=lambda item: (item.meeting_date or "", item.citation)
        ):
            lines.append(f"- {result.meeting_date} — {result.citation}")
    else:
        lines.append("- No dated evidence found.")
    lines.append("")

    lines.extend(["## Open Questions", ""])
    lines.extend(
        [
            "- What additional source documents should be reviewed?",
            "- Are there related agenda items, minutes, or staff reports that corroborate this evidence?",
            "",
        ]
    )

    lines.extend(["## Source List", ""])
    if results:
        for result in results:
            provenance = None
            if source_provenance is not None:
                provenance = source_provenance.get(result.document_id)
                if provenance is None:
                    raise PacketError(
                        "Missing source provenance for packet document: " + result.document_id
                    )
            lines.append(f"- {format_source_list_entry(result, provenance=provenance)}")
    else:
        lines.append("- No sources found.")
    lines.append("")

    return "\n".join(lines)


def write_source_packet(path: Path, content: str, *, overwrite: bool = False) -> None:
    """Write a source packet to disk, refusing accidental overwrites by default."""

    if path.exists() and not overwrite:
        raise PacketError(f"Output file already exists: {path}. Use --overwrite to replace it.")
    path.write_text(content, encoding="utf-8")


def format_source_list_entry(
    result: SearchResult,
    *,
    provenance: PacketSourceProvenance | None = None,
) -> str:
    """Format one source-list entry with available metadata and provenance."""

    source_type = provenance.source_type if provenance is not None else result.source_type
    details = []
    if source_type != SOURCE_TYPE_HTML:
        details.append(f"page {result.page_start}")
    for label, value in (
        ("title", result.title),
        ("body", result.body),
        ("meeting date", result.meeting_date),
        ("document type", result.document_type),
        ("jurisdiction", result.jurisdiction),
        ("source file", result.source_path),
        ("source URL", result.source_url),
    ):
        if value is not None and value.strip():
            details.append(f"{label}: {value.strip()}")

    if provenance is not None:
        if provenance.source_id is not None:
            details.append(f"source ID: {provenance.source_id}")
        if provenance.revision_id is not None:
            details.append(f"revision ID: {provenance.revision_id}")
        if provenance.revision_number is not None:
            state = _snapshot_state_label(provenance.is_current_snapshot)
            details.append(f"revision: {provenance.revision_number} ({state} at retrieval)")
        details.append(f"document ID: {provenance.document_id}")
        if provenance.artifact_id is not None:
            details.append(f"artifact ID: {provenance.artifact_id}")
        if provenance.acquired_at is not None:
            details.append(f"artifact acquired at: {provenance.acquired_at}")
        details.append(f"source type: {provenance.source_type}")
        if provenance.source_kind == "url":
            details.append(f"supplied URL: {provenance.submitted_reference}")
            if provenance.resolved_reference != provenance.submitted_reference:
                details.append(f"final URL: {provenance.resolved_reference}")
            if provenance.retrieved_at is not None:
                details.append(f"retrieved at: {provenance.retrieved_at}")
        else:
            details.append(f"supplied path: {provenance.submitted_reference}")
        details.append(f"artifact SHA-256: {provenance.artifact_hash}")

    if not details:
        return result.citation
    return f"{result.citation} ({'; '.join(details)})"


def _history_label(result: SearchResult) -> str:
    state = _snapshot_state_label(result.is_current_snapshot)
    number = result.revision_number if result.revision_number is not None else "unknown"
    return f" [revision {number}, {state} at retrieval]"


def _snapshot_state_label(is_current_snapshot: bool | None) -> str:
    if is_current_snapshot is None:
        return "state unknown"
    return "current" if is_current_snapshot else "historical"


def _load_json_object(raw_value: object) -> dict[str, object]:
    try:
        value = json.loads(str(raw_value))
    except json.JSONDecodeError:
        return {}
    if not isinstance(value, dict):
        return {}
    return value


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalize_text(value: str) -> str:
    return " ".join(value.split())
