from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from newsrag.discovery import (
    DiscoveryItemRecord,
    DocumentBriefRecord,
    create_document_brief,
    list_discovery_items,
)
from newsrag.facts import extract_document_facts

BRIEF_EXTRACTOR = "deterministic-document-brief"
BRIEF_PROVIDER = "rules"
BRIEF_MODEL = "rules-v1"
MIN_BRIEF_TEXT_LENGTH = 40
_MAX_NOTABLE_ITEMS = 8

_ITEM_PRIORITY = {
    "topic": 0,
    "action": 1,
    "deadline": 2,
    "money": 3,
    "civic_identifier": 4,
    "date": 5,
    "percentage": 6,
    "entity": 7,
    "url": 8,
}


class BriefError(Exception):
    """Raised when a document brief cannot be generated."""


@dataclass(frozen=True)
class BriefDocumentContext:
    """Source metadata for one document brief."""

    id: str
    title: str | None
    metadata: dict[str, Any]
    page_count: int
    text_length: int


@dataclass(frozen=True)
class BriefEvidenceLine:
    """One evidence line selected for a document brief."""

    item_type: str
    label: str
    summary: str
    page_start: int
    page_end: int
    quote: str


@dataclass(frozen=True)
class GeneratedBrief:
    """A generated brief plus its supporting evidence selections."""

    document: BriefDocumentContext
    record: DocumentBriefRecord
    notable_items: tuple[DiscoveryItemRecord, ...]
    evidence_lines: tuple[BriefEvidenceLine, ...]


def generate_document_brief(database_path: Path, document_id: str) -> GeneratedBrief:
    """Generate and persist an evidence-backed deterministic brief for one document."""

    document = _load_document_context(database_path, document_id)
    if document.text_length < MIN_BRIEF_TEXT_LENGTH:
        raise BriefError(
            f"Document {document_id} has too little extracted text for a brief; "
            "ingest/OCR may need review."
        )

    items = _ensure_discovery_items(database_path, document_id)
    notable_items = _select_notable_items(items)
    if not notable_items:
        raise BriefError(
            f"Document {document_id} has no evidence-backed discovery items for a brief; "
            "run discovery extraction or review extracted text."
        )

    summary = _build_summary(document, notable_items)
    significance = _build_significance(notable_items)
    open_questions = _build_open_questions(notable_items)
    record = create_document_brief(
        database_path,
        document_id=document_id,
        summary=summary,
        significance=significance,
        open_questions=open_questions,
        extractor=BRIEF_EXTRACTOR,
        provider=BRIEF_PROVIDER,
        model=BRIEF_MODEL,
        status="validated",
    )

    return GeneratedBrief(
        document=document,
        record=record,
        notable_items=notable_items,
        evidence_lines=tuple(_item_to_evidence_line(item) for item in notable_items),
    )


def format_generated_brief(brief: GeneratedBrief) -> str:
    """Format one generated document brief for terminal output."""

    document = brief.document
    metadata = document.metadata
    lines = [
        "NewsRAG Document Brief",
        f"document_id: {document.id}",
        f"title: {_display_value(document.title)}",
        f"meeting_date: {_display_value(_metadata_string(metadata, 'meeting_date'))}",
        f"body: {_display_value(_metadata_string(metadata, 'body'))}",
        f"pages: {document.page_count}",
        "",
        "Summary:",
        brief.record.summary,
        "",
        "Significance:",
        brief.record.significance,
        "",
        "Notable Evidence:",
    ]

    for line in brief.evidence_lines:
        page_label = f"p. {line.page_start}"
        if line.page_end != line.page_start:
            page_label = f"pp. {line.page_start}-{line.page_end}"
        lines.append(f"- {line.item_type}: {line.label} — {page_label} — {line.quote}")

    lines.extend(["", "Open Questions:"])
    if brief.record.open_questions:
        for question in brief.record.open_questions:
            lines.append(f"- {question}")
    else:
        lines.append("- none")

    lines.extend(
        [
            "",
            "Brief Record:",
            f"id: {brief.record.id}",
            f"extractor: {brief.record.extractor}",
            f"status: {brief.record.status}",
        ]
    )
    return "\n".join(lines)


def _load_document_context(database_path: Path, document_id: str) -> BriefDocumentContext:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT
                documents.id,
                documents.title,
                documents.metadata_json,
                COUNT(pages.id) AS page_count,
                COALESCE(SUM(LENGTH(pages.text)), 0) AS text_length
            FROM documents
            LEFT JOIN pages ON pages.document_id = documents.id
            WHERE documents.id = ?
            GROUP BY documents.id
            """,
            (document_id,),
        ).fetchone()

    if row is None:
        raise BriefError(f"Unknown document: {document_id}")

    return BriefDocumentContext(
        id=str(row["id"]),
        title=_optional_string(row["title"]),
        metadata=_load_metadata(row["metadata_json"]),
        page_count=int(row["page_count"]),
        text_length=int(row["text_length"]),
    )


def _ensure_discovery_items(database_path: Path, document_id: str) -> list[DiscoveryItemRecord]:
    existing_items = list_discovery_items(database_path, document_id=document_id)
    if existing_items:
        return existing_items
    extract_document_facts(database_path, document_id, persist=True)
    return list_discovery_items(database_path, document_id=document_id)


def _select_notable_items(items: list[DiscoveryItemRecord]) -> tuple[DiscoveryItemRecord, ...]:
    supported_items = [item for item in items if item.evidence]
    supported_items.sort(
        key=lambda item: (
            _ITEM_PRIORITY.get(item.item_type, 99),
            -(item.confidence or 0.0),
            item.evidence[0].page_start,
            item.label.casefold(),
        )
    )

    selected: list[DiscoveryItemRecord] = []
    type_counts: dict[str, int] = {}
    for item in supported_items:
        if len(selected) >= _MAX_NOTABLE_ITEMS:
            break
        max_for_type = 3 if item.item_type == "topic" else 2 if item.item_type == "entity" else 99
        current_count = type_counts.get(item.item_type, 0)
        if current_count >= max_for_type:
            continue
        selected.append(item)
        type_counts[item.item_type] = current_count + 1
    return tuple(selected)


def _build_summary(
    document: BriefDocumentContext,
    notable_items: tuple[DiscoveryItemRecord, ...],
) -> str:
    title = document.title or document.id
    metadata_parts = []
    body = _metadata_string(document.metadata, "body")
    meeting_date = _metadata_string(document.metadata, "meeting_date")
    if body is not None:
        metadata_parts.append(body)
    if meeting_date is not None:
        metadata_parts.append(meeting_date)
    context = f" ({', '.join(metadata_parts)})" if metadata_parts else ""

    type_labels = _labels_by_type(notable_items)
    topic_text = _join_labels(type_labels.get("topic", ()), fallback="document activity")
    signal_parts = []
    if type_labels.get("action"):
        signal_parts.append("action or vote language")
    if type_labels.get("deadline"):
        signal_parts.append("deadline or schedule references")
    if type_labels.get("money"):
        signal_parts.append(_join_labels(type_labels["money"], fallback="financial references"))
    if type_labels.get("civic_identifier"):
        signal_parts.append("ordinance or resolution references")
    if type_labels.get("date"):
        signal_parts.append("dated events")
    if type_labels.get("percentage"):
        signal_parts.append("percentage-based progress or rate signals")
    signal_text = _join_labels(tuple(signal_parts), fallback="evidence-backed document signals")

    return (
        f"{title}{context} contains evidence about {topic_text}. "
        f"Notable extracted signals include {signal_text}."
    )


def _build_significance(notable_items: tuple[DiscoveryItemRecord, ...]) -> str:
    labels_by_type = _labels_by_type(notable_items)
    parts = []
    for item_type, label in (
        ("deadline", "deadlines or schedule changes"),
        ("money", "financial commitments"),
        ("action", "formal actions or votes"),
        ("civic_identifier", "ordinances or resolutions"),
        ("percentage", "progress or rate changes"),
    ):
        if labels_by_type.get(item_type):
            parts.append(label)
    if not parts:
        return "The selected evidence provides a starting point for document review."
    return "The selected evidence highlights " + ", ".join(parts) + "."


def _build_open_questions(notable_items: tuple[DiscoveryItemRecord, ...]) -> tuple[str, ...]:
    labels_by_type = _labels_by_type(notable_items)
    questions = []
    if labels_by_type.get("money"):
        questions.append(
            "What funding source, vendor, or approval history explains the money mentioned?"
        )
    if labels_by_type.get("deadline") or labels_by_type.get("date"):
        questions.append("What happens before or after the cited date or deadline?")
    if labels_by_type.get("action"):
        questions.append("Who took the cited action, and what was the vote or decision context?")
    if labels_by_type.get("entity"):
        questions.append("What role do the named people or organizations play in the document?")
    return tuple(questions[:4])


def _labels_by_type(items: tuple[DiscoveryItemRecord, ...]) -> dict[str, tuple[str, ...]]:
    labels: dict[str, list[str]] = {}
    for item in items:
        labels.setdefault(item.item_type, [])
        if item.label not in labels[item.item_type]:
            labels[item.item_type].append(item.label)
    return {key: tuple(value) for key, value in labels.items()}


def _item_to_evidence_line(item: DiscoveryItemRecord) -> BriefEvidenceLine:
    evidence = item.evidence[0]
    return BriefEvidenceLine(
        item_type=item.item_type,
        label=item.label,
        summary=item.summary,
        page_start=evidence.page_start,
        page_end=evidence.page_end,
        quote=evidence.quote,
    )


def _join_labels(labels: tuple[str, ...], *, fallback: str) -> str:
    if not labels:
        return fallback
    if len(labels) == 1:
        return labels[0]
    if len(labels) == 2:
        return f"{labels[0]} and {labels[1]}"
    return ", ".join(labels[:-1]) + f", and {labels[-1]}"


def _metadata_string(metadata: dict[str, Any], key: str) -> str | None:
    return _optional_string(metadata.get(key))


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _display_value(value: str | None) -> str:
    return value or "-"


def _load_metadata(raw_metadata: object) -> dict[str, Any]:
    try:
        metadata = json.loads(str(raw_metadata))
    except json.JSONDecodeError:
        return {}
    if not isinstance(metadata, dict):
        return {}
    return metadata
