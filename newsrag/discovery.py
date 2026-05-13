from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class DiscoveryError(Exception):
    """Raised when discovery records cannot be persisted or loaded."""


@dataclass(frozen=True)
class DocumentProfileRecord:
    """Durable extraction profile for one document."""

    id: str
    document_id: str
    page_count: int
    text_length: int
    extraction_quality: dict[str, Any]
    extractor: str
    provider: str | None
    model: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DocumentBriefRecord:
    """Durable evidence-oriented brief for one document."""

    id: str
    document_id: str
    summary: str
    significance: str
    open_questions: tuple[str, ...]
    extractor: str
    provider: str | None
    model: str | None
    status: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class DiscoveryEvidenceDraft:
    """Evidence reference to persist for one discovery item."""

    document_id: str
    page_start: int
    page_end: int
    quote: str
    validation_status: str
    page_id: str | None = None
    passage_id: str | None = None


@dataclass(frozen=True)
class DiscoveryEvidenceRecord:
    """Durable provenance reference for one discovery item."""

    id: str
    item_id: str
    document_id: str
    page_id: str | None
    passage_id: str | None
    page_start: int
    page_end: int
    quote: str
    validation_status: str
    created_at: str


@dataclass(frozen=True)
class DiscoveryItemRecord:
    """Durable evidence-backed discovery item."""

    id: str
    document_id: str
    item_type: str
    label: str
    value: dict[str, Any]
    summary: str
    confidence: float | None
    extractor: str
    provider: str | None
    model: str | None
    created_at: str
    evidence: tuple[DiscoveryEvidenceRecord, ...]


def create_document_profile(
    database_path: Path,
    *,
    document_id: str,
    page_count: int,
    text_length: int,
    extraction_quality: dict[str, Any] | None = None,
    extractor: str,
    provider: str | None = None,
    model: str | None = None,
    profile_id: str | None = None,
) -> DocumentProfileRecord:
    """Create or replace a document extraction profile."""

    _validate_non_empty(document_id, field_name="document_id")
    _validate_non_empty(extractor, field_name="extractor")
    if page_count < 0:
        raise DiscoveryError("page_count must be zero or greater")
    if text_length < 0:
        raise DiscoveryError("text_length must be zero or greater")

    resolved_profile_id = profile_id or f"profile-{uuid.uuid4().hex[:8]}"
    quality_json = _dump_json_object(extraction_quality or {}, field_name="extraction_quality")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO document_profiles(
                id,
                document_id,
                page_count,
                text_length,
                extraction_quality_json,
                extractor,
                provider,
                model
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(document_id) DO UPDATE SET
                page_count = excluded.page_count,
                text_length = excluded.text_length,
                extraction_quality_json = excluded.extraction_quality_json,
                extractor = excluded.extractor,
                provider = excluded.provider,
                model = excluded.model,
                updated_at = CURRENT_TIMESTAMP
            """,
            (
                resolved_profile_id,
                document_id,
                page_count,
                text_length,
                quality_json,
                extractor.strip(),
                _optional_string(provider),
                _optional_string(model),
            ),
        )
        connection.commit()

    return get_document_profile(database_path, document_id)


def get_document_profile(database_path: Path, document_id: str) -> DocumentProfileRecord:
    """Load one document profile by document ID."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                id,
                document_id,
                page_count,
                text_length,
                extraction_quality_json,
                extractor,
                provider,
                model,
                created_at,
                updated_at
            FROM document_profiles
            WHERE document_id = ?
            """,
            (document_id,),
        ).fetchone()

    if row is None:
        raise DiscoveryError(f"Unknown document profile: {document_id}")
    return _row_to_document_profile(row)


def create_document_brief(
    database_path: Path,
    *,
    document_id: str,
    summary: str,
    extractor: str,
    significance: str = "",
    open_questions: Sequence[str] = (),
    provider: str | None = None,
    model: str | None = None,
    status: str = "draft",
    brief_id: str | None = None,
) -> DocumentBriefRecord:
    """Persist a document brief and index its searchable text."""

    _validate_non_empty(document_id, field_name="document_id")
    _validate_non_empty(extractor, field_name="extractor")
    _validate_non_empty(status, field_name="status")
    resolved_brief_id = brief_id or f"brief-{uuid.uuid4().hex[:8]}"
    open_questions_json = json.dumps(list(open_questions), sort_keys=True)

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO document_briefs(
                id,
                document_id,
                summary,
                significance,
                open_questions_json,
                extractor,
                provider,
                model,
                status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_brief_id,
                document_id,
                summary,
                significance,
                open_questions_json,
                extractor.strip(),
                _optional_string(provider),
                _optional_string(model),
                status.strip(),
            ),
        )
        connection.execute(
            """
            INSERT INTO document_briefs_fts(brief_id, summary, significance)
            VALUES(?, ?, ?)
            """,
            (resolved_brief_id, summary, significance),
        )
        connection.commit()

    return get_document_brief(database_path, resolved_brief_id)


def get_document_brief(database_path: Path, brief_id: str) -> DocumentBriefRecord:
    """Load one document brief by ID."""

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                id,
                document_id,
                summary,
                significance,
                open_questions_json,
                extractor,
                provider,
                model,
                status,
                created_at,
                updated_at
            FROM document_briefs
            WHERE id = ?
            """,
            (brief_id,),
        ).fetchone()

    if row is None:
        raise DiscoveryError(f"Unknown document brief: {brief_id}")
    return _row_to_document_brief(row)


def list_document_briefs(
    database_path: Path,
    *,
    document_id: str | None = None,
) -> list[DocumentBriefRecord]:
    """List document briefs, optionally scoped to one document."""

    clause = ""
    parameters: tuple[object, ...] = ()
    if document_id is not None:
        clause = "WHERE document_id = ?"
        parameters = (document_id,)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            f"""
            SELECT
                id,
                document_id,
                summary,
                significance,
                open_questions_json,
                extractor,
                provider,
                model,
                status,
                created_at,
                updated_at
            FROM document_briefs
            {clause}
            ORDER BY created_at ASC, id ASC
            """,
            parameters,
        ).fetchall()

    return [_row_to_document_brief(row) for row in rows]


def create_discovery_item(
    database_path: Path,
    *,
    document_id: str,
    item_type: str,
    label: str,
    extractor: str,
    value: dict[str, Any] | None = None,
    summary: str = "",
    confidence: float | None = None,
    provider: str | None = None,
    model: str | None = None,
    evidence: Sequence[DiscoveryEvidenceDraft] = (),
    item_id: str | None = None,
) -> DiscoveryItemRecord:
    """Persist one discovery item and its evidence references."""

    _validate_non_empty(document_id, field_name="document_id")
    _validate_non_empty(item_type, field_name="item_type")
    _validate_non_empty(label, field_name="label")
    _validate_non_empty(extractor, field_name="extractor")
    _validate_confidence(confidence)
    for draft in evidence:
        _validate_evidence_draft(draft, item_document_id=document_id)

    resolved_item_id = item_id or f"discovery-{uuid.uuid4().hex[:8]}"
    value_json = _dump_json_object(value or {}, field_name="value")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO discovery_items(
                id,
                document_id,
                item_type,
                label,
                value_json,
                summary,
                confidence,
                extractor,
                provider,
                model
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                resolved_item_id,
                document_id,
                item_type.strip(),
                label.strip(),
                value_json,
                summary,
                confidence,
                extractor.strip(),
                _optional_string(provider),
                _optional_string(model),
            ),
        )
        connection.execute(
            """
            INSERT INTO discovery_items_fts(item_id, label, summary)
            VALUES(?, ?, ?)
            """,
            (resolved_item_id, label, summary),
        )
        connection.executemany(
            """
            INSERT INTO discovery_evidence(
                id,
                item_id,
                document_id,
                page_id,
                passage_id,
                page_start,
                page_end,
                quote,
                validation_status
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    f"evidence-{uuid.uuid4().hex[:8]}",
                    resolved_item_id,
                    draft.document_id,
                    draft.page_id,
                    draft.passage_id,
                    draft.page_start,
                    draft.page_end,
                    draft.quote,
                    draft.validation_status.strip(),
                )
                for draft in evidence
            ],
        )
        connection.commit()

    return get_discovery_item(database_path, resolved_item_id)


def get_discovery_item(database_path: Path, item_id: str) -> DiscoveryItemRecord:
    """Load one discovery item with evidence references."""

    items = list_discovery_items(database_path, item_id=item_id)
    if not items:
        raise DiscoveryError(f"Unknown discovery item: {item_id}")
    return items[0]


def list_discovery_items(
    database_path: Path,
    *,
    document_id: str | None = None,
    item_type: str | None = None,
    item_id: str | None = None,
) -> list[DiscoveryItemRecord]:
    """List discovery items with evidence references."""

    clauses: list[str] = []
    parameters: list[object] = []
    if document_id is not None:
        clauses.append("document_id = ?")
        parameters.append(document_id)
    if item_type is not None:
        clauses.append("item_type = ?")
        parameters.append(item_type)
    if item_id is not None:
        clauses.append("id = ?")
        parameters.append(item_id)

    where_sql = ""
    if clauses:
        where_sql = "WHERE " + " AND ".join(clauses)

    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        item_rows = connection.execute(
            f"""
            SELECT
                id,
                document_id,
                item_type,
                label,
                value_json,
                summary,
                confidence,
                extractor,
                provider,
                model,
                created_at
            FROM discovery_items
            {where_sql}
            ORDER BY created_at ASC, id ASC
            """,
            tuple(parameters),
        ).fetchall()
        item_ids = tuple(str(row["id"]) for row in item_rows)
        if item_ids:
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
        else:
            evidence_rows = []

    evidence_by_item_id: dict[str, list[DiscoveryEvidenceRecord]] = {}
    for row in evidence_rows:
        evidence = _row_to_discovery_evidence(row)
        evidence_by_item_id.setdefault(evidence.item_id, []).append(evidence)

    return [
        _row_to_discovery_item(row, evidence_by_item_id.get(str(row["id"]), []))
        for row in item_rows
    ]


def _validate_non_empty(value: str, *, field_name: str) -> None:
    if not value.strip():
        raise DiscoveryError(f"{field_name} must be non-empty")


def _validate_confidence(confidence: float | None) -> None:
    if confidence is None:
        return
    if confidence < 0 or confidence > 1:
        raise DiscoveryError("confidence must be between 0 and 1")


def _validate_evidence_draft(
    draft: DiscoveryEvidenceDraft,
    *,
    item_document_id: str,
) -> None:
    _validate_non_empty(draft.document_id, field_name="evidence.document_id")
    _validate_non_empty(draft.quote, field_name="evidence.quote")
    _validate_non_empty(draft.validation_status, field_name="evidence.validation_status")
    if draft.document_id != item_document_id:
        raise DiscoveryError("evidence.document_id must match discovery item document_id")
    if draft.page_start < 1:
        raise DiscoveryError("evidence.page_start must be 1 or greater")
    if draft.page_end < draft.page_start:
        raise DiscoveryError("evidence.page_end must be greater than or equal to page_start")


def _dump_json_object(value: dict[str, Any], *, field_name: str) -> str:
    if not isinstance(value, dict):
        raise DiscoveryError(f"{field_name} must be a JSON object")
    return json.dumps(value, sort_keys=True)


def _optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _load_json_object(value: object) -> dict[str, Any]:
    try:
        loaded = json.loads(str(value))
    except json.JSONDecodeError:
        return {}
    if not isinstance(loaded, dict):
        return {}
    return loaded


def _load_string_tuple(value: object) -> tuple[str, ...]:
    try:
        loaded = json.loads(str(value))
    except json.JSONDecodeError:
        return ()
    if not isinstance(loaded, list):
        return ()
    return tuple(item for item in loaded if isinstance(item, str))


def _row_to_document_profile(row: sqlite3.Row) -> DocumentProfileRecord:
    return DocumentProfileRecord(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        page_count=int(row["page_count"]),
        text_length=int(row["text_length"]),
        extraction_quality=_load_json_object(row["extraction_quality_json"]),
        extractor=str(row["extractor"]),
        provider=str(row["provider"]) if row["provider"] is not None else None,
        model=str(row["model"]) if row["model"] is not None else None,
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_document_brief(row: sqlite3.Row) -> DocumentBriefRecord:
    return DocumentBriefRecord(
        id=str(row["id"]),
        document_id=str(row["document_id"]),
        summary=str(row["summary"]),
        significance=str(row["significance"]),
        open_questions=_load_string_tuple(row["open_questions_json"]),
        extractor=str(row["extractor"]),
        provider=str(row["provider"]) if row["provider"] is not None else None,
        model=str(row["model"]) if row["model"] is not None else None,
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        updated_at=str(row["updated_at"]),
    )


def _row_to_discovery_evidence(row: sqlite3.Row) -> DiscoveryEvidenceRecord:
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


def _row_to_discovery_item(
    row: sqlite3.Row,
    evidence: Sequence[DiscoveryEvidenceRecord],
) -> DiscoveryItemRecord:
    return DiscoveryItemRecord(
        id=str(row["id"]),
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
        evidence=tuple(evidence),
    )
