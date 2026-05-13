from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from newsrag.discovery import (
    DiscoveryEvidenceDraft,
    DiscoveryItemRecord,
    DocumentBriefRecord,
    create_discovery_item,
    create_document_brief,
)

ENRICHMENT_EXTRACTOR = "structured-llm-enrichment"
SUMMARY_ITEM_TYPE = "summary"
STORY_LEAD_ITEM_TYPE = "story_lead"
ACTION_ITEM_TYPE = "action"
VALIDATION_STATUS_VALIDATED = "validated"


class EnrichmentError(Exception):
    """Raised when structured enrichment cannot complete."""


@dataclass(frozen=True)
class EvidenceContext:
    """Page or passage text available for quote validation."""

    document_id: str
    page_start: int
    page_end: int
    text: str
    page_id: str | None = None
    passage_id: str | None = None


@dataclass(frozen=True)
class EnrichmentRequest:
    """Input sent to an enrichment provider."""

    document_id: str
    title: str | None
    metadata: dict[str, Any]
    evidence_contexts: tuple[EvidenceContext, ...]


@dataclass(frozen=True)
class EnrichmentResult:
    """Persisted result of one structured enrichment run."""

    document_id: str
    brief: DocumentBriefRecord
    items: tuple[DiscoveryItemRecord, ...]


class EnrichmentProvider(Protocol):
    """Provider interface for structured document enrichment."""

    @property
    def name(self) -> str:
        """Provider name for provenance."""

    @property
    def model(self) -> str:
        """Provider model name for provenance."""

    def enrich(self, request: EnrichmentRequest) -> str:
        """Return structured enrichment JSON for one document."""


@dataclass(frozen=True)
class JsonFileEnrichmentProvider:
    """Structured enrichment provider backed by a local JSON file."""

    path: Path
    name: str = "json-file"
    model: str = "manual-json"

    def enrich(self, request: EnrichmentRequest) -> str:
        del request
        try:
            return self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise EnrichmentError(f"Could not read enrichment response JSON: {self.path}") from exc


def enrich_document(
    database_path: Path,
    document_id: str,
    *,
    provider: EnrichmentProvider,
) -> EnrichmentResult:
    """Run structured enrichment for one document and persist validated output."""

    request = _build_enrichment_request(database_path, document_id)
    raw_response = provider.enrich(request)
    payload = _parse_provider_response(raw_response)
    validated = _validate_payload(payload, request)

    brief = create_document_brief(
        database_path,
        document_id=document_id,
        summary=validated.summary,
        significance=_build_significance(validated),
        open_questions=validated.open_questions,
        extractor=ENRICHMENT_EXTRACTOR,
        provider=provider.name,
        model=provider.model,
        status="validated",
    )

    items: list[DiscoveryItemRecord] = []
    items.append(
        create_discovery_item(
            database_path,
            document_id=document_id,
            item_type=SUMMARY_ITEM_TYPE,
            label="Document summary",
            value={"source": "structured_enrichment"},
            summary=validated.summary,
            confidence=0.8,
            extractor=ENRICHMENT_EXTRACTOR,
            provider=provider.name,
            model=provider.model,
            evidence=validated.summary_evidence,
        )
    )
    for action in validated.notable_actions:
        items.append(
            create_discovery_item(
                database_path,
                document_id=document_id,
                item_type=ACTION_ITEM_TYPE,
                label=action.label,
                value={"source": "structured_enrichment"},
                summary=action.summary,
                confidence=0.78,
                extractor=ENRICHMENT_EXTRACTOR,
                provider=provider.name,
                model=provider.model,
                evidence=(action.evidence,),
            )
        )
    for lead in validated.story_leads:
        items.append(
            create_discovery_item(
                database_path,
                document_id=document_id,
                item_type=STORY_LEAD_ITEM_TYPE,
                label=lead.label,
                value={"source": "structured_enrichment"},
                summary=lead.summary,
                confidence=0.7,
                extractor=ENRICHMENT_EXTRACTOR,
                provider=provider.name,
                model=provider.model,
                evidence=(lead.evidence,),
            )
        )

    return EnrichmentResult(document_id=document_id, brief=brief, items=tuple(items))


def format_enrichment_result(result: EnrichmentResult) -> str:
    """Format one enrichment result for terminal output."""

    lines = [
        "NewsRAG Enrichment",
        f"document_id: {result.document_id}",
        f"brief_id: {result.brief.id}",
        f"items_created: {len(result.items)}",
        "summary:",
        result.brief.summary,
        "items:",
    ]
    for item in result.items:
        page_label = ""
        if item.evidence:
            page_label = f" p.{item.evidence[0].page_start}"
        lines.append(f"- {item.item_type}: {item.label}{page_label}")
    return "\n".join(lines)


@dataclass(frozen=True)
class _ValidatedClaim:
    label: str
    summary: str
    evidence: DiscoveryEvidenceDraft


@dataclass(frozen=True)
class _ValidatedPayload:
    summary: str
    summary_evidence: tuple[DiscoveryEvidenceDraft, ...]
    notable_actions: tuple[_ValidatedClaim, ...]
    story_leads: tuple[_ValidatedClaim, ...]
    open_questions: tuple[str, ...]


def _build_enrichment_request(database_path: Path, document_id: str) -> EnrichmentRequest:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        document_row = connection.execute(
            """
            SELECT id, title, metadata_json
            FROM documents
            WHERE id = ?
            """,
            (document_id,),
        ).fetchone()
        if document_row is None:
            raise EnrichmentError(f"Unknown document: {document_id}")

        page_rows = connection.execute(
            """
            SELECT id, page_number, text
            FROM pages
            WHERE document_id = ?
            ORDER BY page_number ASC, id ASC
            """,
            (document_id,),
        ).fetchall()
        passage_rows = connection.execute(
            """
            SELECT id, page_start, page_end, text
            FROM passages
            WHERE document_id = ?
            ORDER BY page_start ASC, ordinal ASC, id ASC
            """,
            (document_id,),
        ).fetchall()

    contexts = [
        EvidenceContext(
            document_id=document_id,
            page_id=str(row["id"]),
            page_start=int(row["page_number"]),
            page_end=int(row["page_number"]),
            text=str(row["text"]),
        )
        for row in page_rows
        if str(row["text"]).strip()
    ]
    contexts.extend(
        EvidenceContext(
            document_id=document_id,
            passage_id=str(row["id"]),
            page_start=int(row["page_start"]),
            page_end=int(row["page_end"]),
            text=str(row["text"]),
        )
        for row in passage_rows
        if str(row["text"]).strip()
    )
    if not contexts:
        raise EnrichmentError(f"Document {document_id} has no text available for enrichment")

    return EnrichmentRequest(
        document_id=document_id,
        title=_optional_string(document_row["title"]),
        metadata=_load_metadata(document_row["metadata_json"]),
        evidence_contexts=tuple(contexts),
    )


def _parse_provider_response(raw_response: str) -> Mapping[str, object]:
    try:
        payload = json.loads(raw_response)
    except json.JSONDecodeError as exc:
        raise EnrichmentError(f"Provider returned malformed JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise EnrichmentError("Provider response must be a JSON object")
    return payload


def _validate_payload(
    payload: Mapping[str, object],
    request: EnrichmentRequest,
) -> _ValidatedPayload:
    summary = _required_string(payload, "summary")
    summary_evidence = tuple(
        _validate_evidence_object(item, request=request, field_name="summary_evidence")
        for item in _required_list(payload, "summary_evidence")
    )
    if not summary_evidence:
        raise EnrichmentError("summary_evidence must contain at least one evidence reference")

    notable_actions = tuple(
        _validate_claim(item, request=request, field_name="notable_actions")
        for item in _required_list(payload, "notable_actions")
    )
    story_leads = tuple(
        _validate_claim(item, request=request, field_name="story_leads")
        for item in _required_list(payload, "story_leads")
    )
    open_questions = tuple(
        _validate_string_list_item(item, field_name="open_questions")
        for item in _required_list(payload, "open_questions")
    )

    return _ValidatedPayload(
        summary=summary,
        summary_evidence=summary_evidence,
        notable_actions=notable_actions,
        story_leads=story_leads,
        open_questions=open_questions,
    )


def _validate_claim(
    value: object,
    *,
    request: EnrichmentRequest,
    field_name: str,
) -> _ValidatedClaim:
    if not isinstance(value, dict):
        raise EnrichmentError(f"{field_name} entries must be objects")
    label = _required_string(value, "label")
    summary = _required_string(value, "summary")
    evidence_raw = value.get("evidence")
    if not isinstance(evidence_raw, dict):
        raise EnrichmentError(f"{field_name} entry evidence must be an object")
    evidence = _validate_evidence_object(evidence_raw, request=request, field_name=field_name)
    return _ValidatedClaim(label=label, summary=summary, evidence=evidence)


def _validate_evidence_object(
    value: object,
    *,
    request: EnrichmentRequest,
    field_name: str,
) -> DiscoveryEvidenceDraft:
    if not isinstance(value, dict):
        raise EnrichmentError(f"{field_name} evidence entries must be objects")
    page_start = _required_int(value, "page_start")
    page_end = _optional_int(value, "page_end") or page_start
    if page_start < 1 or page_end < page_start:
        raise EnrichmentError(f"{field_name} evidence page range is invalid")
    quote = _required_string(value, "quote")
    passage_id = _optional_string(value.get("passage_id"))
    context = _find_supporting_context(
        request.evidence_contexts,
        page_start=page_start,
        page_end=page_end,
        quote=quote,
        passage_id=passage_id,
    )
    if context is None:
        raise EnrichmentError(
            f"Unsupported quote for {field_name}: quote was not found in cited source text"
        )

    return DiscoveryEvidenceDraft(
        document_id=request.document_id,
        page_id=context.page_id,
        passage_id=context.passage_id,
        page_start=page_start,
        page_end=page_end,
        quote=quote,
        validation_status=VALIDATION_STATUS_VALIDATED,
    )


def _find_supporting_context(
    contexts: Sequence[EvidenceContext],
    *,
    page_start: int,
    page_end: int,
    quote: str,
    passage_id: str | None,
) -> EvidenceContext | None:
    normalized_quote = _normalize_for_match(quote)
    for context in contexts:
        if passage_id is not None and context.passage_id != passage_id:
            continue
        if context.page_start > page_end or context.page_end < page_start:
            continue
        if normalized_quote in _normalize_for_match(context.text):
            return context
    return None


def _build_significance(payload: _ValidatedPayload) -> str:
    parts = []
    if payload.notable_actions:
        parts.append("notable actions")
    if payload.story_leads:
        parts.append("story leads")
    if not parts:
        return "Structured enrichment produced an evidence-backed summary."
    return "Structured enrichment identified " + " and ".join(parts) + "."


def _required_list(payload: Mapping[str, object], key: str) -> list[object]:
    value = payload.get(key)
    if not isinstance(value, list):
        raise EnrichmentError(f"Provider response field '{key}' must be a list")
    return value


def _required_string(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    resolved = _optional_string(value)
    if resolved is None:
        raise EnrichmentError(f"Provider response field '{key}' must be a non-empty string")
    return resolved


def _required_int(payload: Mapping[str, object], key: str) -> int:
    value = payload.get(key)
    if not isinstance(value, int):
        raise EnrichmentError(f"Provider response field '{key}' must be an integer")
    return value


def _optional_int(payload: Mapping[str, object], key: str) -> int | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise EnrichmentError(f"Provider response field '{key}' must be an integer")
    return value


def _validate_string_list_item(value: object, *, field_name: str) -> str:
    resolved = _optional_string(value)
    if resolved is None:
        raise EnrichmentError(f"{field_name} entries must be non-empty strings")
    return resolved


def _load_metadata(raw_metadata: object) -> dict[str, Any]:
    try:
        metadata = json.loads(str(raw_metadata))
    except json.JSONDecodeError:
        return {}
    if not isinstance(metadata, dict):
        return {}
    return metadata


def _optional_string(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _normalize_for_match(value: str) -> str:
    return " ".join(value.casefold().split())
