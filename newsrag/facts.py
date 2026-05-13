from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from newsrag.discovery import (
    DiscoveryEvidenceDraft,
    DiscoveryItemRecord,
    create_discovery_item,
    list_discovery_items,
)

DETERMINISTIC_FACT_EXTRACTOR = "deterministic-civic-facts"
DETERMINISTIC_FACT_PROVIDER = "rules"
DETERMINISTIC_FACT_MODEL = "rules-v1"
VALIDATION_STATUS_VALIDATED = "validated"

_MONEY_PATTERN = re.compile(
    r"\$\s?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?:\s?(?:million|billion|thousand))?",
    re.IGNORECASE,
)
_PERCENT_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\s?(?:%|percent\b)", re.IGNORECASE)
_ISO_DATE_PATTERN = re.compile(r"\b20\d{2}-\d{2}-\d{2}\b")
_LONG_DATE_PATTERN = re.compile(
    r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\s+\d{1,2},\s+20\d{2}\b",
    re.IGNORECASE,
)
_URL_PATTERN = re.compile(r"https?://[^\s)>,]+", re.IGNORECASE)
_ORDINANCE_PATTERN = re.compile(
    r"\b(?:Ordinance|Resolution)(?:\s+(?:No\.|Number))?\s+[A-Za-z0-9-]+\b",
    re.IGNORECASE,
)
_ENTITY_PATTERN = re.compile(r"\b(?:[A-Z][A-Za-z&'-]+)(?:\s+(?:[A-Z][A-Za-z&'-]+|of|and|&)){1,5}\b")
_SENTENCE_PATTERN = re.compile(r"[^.!?\n]*(?:[.!?]|$)")
_DEADLINE_PATTERN = re.compile(
    r"\b(?:deadline|due|no later than|must be submitted by|must begin by|must be completed by|by\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec))\b",
    re.IGNORECASE,
)
_ACTION_PATTERN = re.compile(
    r"\b(?:approved|awarded|authorized|adopted|denied|postponed|tabled|voted|passed|rejected|accepted|amended)\b",
    re.IGNORECASE,
)

_TOPIC_KEYWORDS = {
    "budget": ("budget", "fund", "funding", "appropriation"),
    "contract": ("contract", "bid", "vendor", "procurement"),
    "development": ("development", "developer", "subdivision"),
    "grant": ("grant",),
    "housing": ("housing", "affordable housing"),
    "infrastructure": ("infrastructure", "road", "sidewalk", "bridge"),
    "ordinance": ("ordinance",),
    "parks": ("park", "parks", "recreation"),
    "public hearing": ("public hearing",),
    "resolution": ("resolution",),
    "sewer": ("sewer", "wastewater", "treatment plant"),
    "stormwater": ("stormwater", "runoff", "drainage"),
    "zoning": ("zoning", "rezoning", "variance"),
}
_ENTITY_STOP_PHRASES = {
    "City Manager",
    "City Manager Report",
    "City Manager's Report",
    "City Manager’s Report",
    "Page",
    "Staff Report",
}
_ENTITY_SUFFIX_WORDS = {
    "Agency",
    "Authority",
    "Board",
    "Bureau",
    "City",
    "Commission",
    "Committee",
    "Company",
    "Construction",
    "Corp",
    "Corporation",
    "Council",
    "County",
    "Department",
    "Inc",
    "Library",
    "LLC",
    "Mayor",
    "Office",
}
_ENTITY_REJECT_START_WORDS = {
    "All",
    "Join",
    "Monday",
    "Open",
    "Page",
    "The",
}
_MONTH_NAMES = {
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
}


class FactExtractionError(Exception):
    """Raised when deterministic fact extraction cannot complete."""


@dataclass(frozen=True)
class FactSource:
    """Canonical source text for deterministic fact extraction."""

    document_id: str
    text: str
    page_start: int
    page_end: int
    page_id: str | None = None
    passage_id: str | None = None


@dataclass(frozen=True)
class FactDraft:
    """One deterministic discovery item ready to persist."""

    item_type: str
    label: str
    value: dict[str, Any]
    summary: str
    confidence: float
    evidence: DiscoveryEvidenceDraft


@dataclass(frozen=True)
class FactExtractionResult:
    """Result from extracting and optionally persisting deterministic facts."""

    document_id: str
    drafts: tuple[FactDraft, ...]
    created: tuple[DiscoveryItemRecord, ...]
    skipped_existing: int

    @property
    def total(self) -> int:
        """Return the total number of extracted fact drafts."""

        return len(self.drafts)


def extract_facts_from_sources(sources: Sequence[FactSource]) -> list[FactDraft]:
    """Extract high-confidence civic fact drafts from source text."""

    drafts: list[FactDraft] = []
    seen: set[tuple[str, str, int, str]] = set()
    for source in sources:
        source_text = _normalize_space(source.text)
        if not source_text:
            continue

        for draft in _extract_regex_facts(source):
            _append_unique(drafts, seen, draft)
        for sentence in _iter_sentences(source):
            for draft in _extract_sentence_facts(source, sentence):
                _append_unique(drafts, seen, draft)
        for draft in _extract_topic_facts(source):
            _append_unique(drafts, seen, draft)
        for draft in _extract_entity_facts(source):
            _append_unique(drafts, seen, draft)

    return drafts


def extract_document_facts(
    database_path: Path,
    document_id: str,
    *,
    persist: bool = True,
) -> FactExtractionResult:
    """Extract deterministic facts for one document and optionally persist new items."""

    sources = _load_document_sources(database_path, document_id)
    drafts = tuple(extract_facts_from_sources(sources))
    if not persist:
        return FactExtractionResult(
            document_id=document_id,
            drafts=drafts,
            created=(),
            skipped_existing=0,
        )

    existing_keys = _existing_fact_keys(database_path, document_id)
    created: list[DiscoveryItemRecord] = []
    skipped = 0
    for draft in drafts:
        key = _fact_key(draft)
        if key in existing_keys:
            skipped += 1
            continue
        created.append(
            create_discovery_item(
                database_path,
                document_id=document_id,
                item_type=draft.item_type,
                label=draft.label,
                value=draft.value,
                summary=draft.summary,
                confidence=draft.confidence,
                extractor=DETERMINISTIC_FACT_EXTRACTOR,
                provider=DETERMINISTIC_FACT_PROVIDER,
                model=DETERMINISTIC_FACT_MODEL,
                evidence=(draft.evidence,),
            )
        )
        existing_keys.add(key)

    return FactExtractionResult(
        document_id=document_id,
        drafts=drafts,
        created=tuple(created),
        skipped_existing=skipped,
    )


def format_fact_extraction_result(result: FactExtractionResult) -> str:
    """Format a deterministic fact extraction result for terminal output."""

    lines = [
        "NewsRAG Discovery",
        f"document_id: {result.document_id}",
        f"extracted: {result.total}",
        f"created: {len(result.created)}",
        f"skipped_existing: {result.skipped_existing}",
    ]

    items = result.created if result.created else ()
    if not items:
        lines.append("items: none")
        return "\n".join(lines)

    lines.append("items:")
    for item in items:
        citation = ""
        if item.evidence:
            evidence = item.evidence[0]
            citation = f" p.{evidence.page_start}"
        lines.append(f"- {item.item_type}: {item.label} confidence={item.confidence:.2f}{citation}")
    return "\n".join(lines)


def _load_document_sources(database_path: Path, document_id: str) -> tuple[FactSource, ...]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        document_row = connection.execute(
            "SELECT id FROM documents WHERE id = ?",
            (document_id,),
        ).fetchone()
        if document_row is None:
            raise FactExtractionError(f"Unknown document: {document_id}")
        rows = connection.execute(
            """
            SELECT id, document_id, page_number, text
            FROM pages
            WHERE document_id = ?
            ORDER BY page_number ASC, id ASC
            """,
            (document_id,),
        ).fetchall()

    return tuple(
        FactSource(
            document_id=str(row["document_id"]),
            text=str(row["text"]),
            page_start=int(row["page_number"]),
            page_end=int(row["page_number"]),
            page_id=str(row["id"]),
        )
        for row in rows
    )


def _extract_regex_facts(source: FactSource) -> Iterable[FactDraft]:
    yield from _extract_pattern_facts(
        source,
        pattern=_MONEY_PATTERN,
        item_type="money",
        confidence=0.95,
        value_key="amount_text",
    )
    yield from _extract_pattern_facts(
        source,
        pattern=_PERCENT_PATTERN,
        item_type="percentage",
        confidence=0.92,
        value_key="percentage_text",
    )
    yield from _extract_date_facts(source)
    yield from _extract_pattern_facts(
        source,
        pattern=_URL_PATTERN,
        item_type="url",
        confidence=0.98,
        value_key="url",
    )
    yield from _extract_pattern_facts(
        source,
        pattern=_ORDINANCE_PATTERN,
        item_type="civic_identifier",
        confidence=0.9,
        value_key="identifier",
    )


def _extract_pattern_facts(
    source: FactSource,
    *,
    pattern: re.Pattern[str],
    item_type: str,
    confidence: float,
    value_key: str,
) -> Iterable[FactDraft]:
    for match in pattern.finditer(source.text):
        label = _normalize_space(match.group(0).rstrip(".,;:"))
        if not label:
            continue
        yield _fact_draft(
            source,
            item_type=item_type,
            label=label,
            value={value_key: label},
            summary=f"{label} appears in document text.",
            confidence=confidence,
            quote=_quote_for_match(source.text, match.start(), match.end()),
        )


def _extract_date_facts(source: FactSource) -> Iterable[FactDraft]:
    for pattern in (_ISO_DATE_PATTERN, _LONG_DATE_PATTERN):
        for match in pattern.finditer(source.text):
            label = _normalize_space(match.group(0).rstrip(".,;:"))
            if not _looks_like_valid_date(label):
                continue
            yield _fact_draft(
                source,
                item_type="date",
                label=label,
                value={"date_text": label},
                summary=f"{label} appears in document text.",
                confidence=0.9,
                quote=_quote_for_match(source.text, match.start(), match.end()),
            )


def _extract_sentence_facts(source: FactSource, sentence: str) -> Iterable[FactDraft]:
    if _DEADLINE_PATTERN.search(sentence):
        label = _sentence_label(sentence, fallback="deadline")
        yield _fact_draft(
            source,
            item_type="deadline",
            label=label,
            value={"phrase": label},
            summary="Deadline-related language appears in document text.",
            confidence=0.82,
            quote=sentence,
        )

    action_match = _ACTION_PATTERN.search(sentence)
    if action_match is not None:
        verb = action_match.group(0).lower()
        yield _fact_draft(
            source,
            item_type="action",
            label=verb,
            value={"verb": verb},
            summary="Action or vote language appears in document text.",
            confidence=0.75,
            quote=sentence,
        )


def _extract_topic_facts(source: FactSource) -> Iterable[FactDraft]:
    lowered_text = source.text.lower()
    for topic, keywords in sorted(_TOPIC_KEYWORDS.items()):
        matched_keyword = next(
            (
                keyword
                for keyword in keywords
                if re.search(rf"\b{re.escape(keyword)}\b", lowered_text)
            ),
            None,
        )
        if matched_keyword is None:
            continue
        quote = _quote_for_keyword(source.text, matched_keyword) or _short_quote(source.text)
        yield _fact_draft(
            source,
            item_type="topic",
            label=topic,
            value={"keyword": matched_keyword},
            summary=f"Topic candidate from keyword '{matched_keyword}'.",
            confidence=0.6,
            quote=quote,
        )


def _extract_entity_facts(source: FactSource) -> Iterable[FactDraft]:
    for match in _ENTITY_PATTERN.finditer(source.text):
        label = _normalize_space(match.group(0).strip(".,;:()[]"))
        if not _is_entity_candidate(label):
            continue
        yield _fact_draft(
            source,
            item_type="entity",
            label=label,
            value={"name": label},
            summary="Entity candidate from capitalized phrase.",
            confidence=0.55,
            quote=_quote_for_match(source.text, match.start(), match.end()),
        )


def _fact_draft(
    source: FactSource,
    *,
    item_type: str,
    label: str,
    value: dict[str, Any],
    summary: str,
    confidence: float,
    quote: str,
) -> FactDraft:
    return FactDraft(
        item_type=item_type,
        label=label,
        value=value,
        summary=summary,
        confidence=confidence,
        evidence=DiscoveryEvidenceDraft(
            document_id=source.document_id,
            page_id=source.page_id,
            passage_id=source.passage_id,
            page_start=source.page_start,
            page_end=source.page_end,
            quote=_normalize_space(quote),
            validation_status=VALIDATION_STATUS_VALIDATED,
        ),
    )


def _iter_sentences(source: FactSource) -> Iterable[str]:
    for match in _SENTENCE_PATTERN.finditer(source.text):
        sentence = _normalize_space(match.group(0))
        if len(sentence) >= 20:
            yield sentence


def _append_unique(
    drafts: list[FactDraft],
    seen: set[tuple[str, str, int, str]],
    draft: FactDraft,
) -> None:
    key = _fact_key(draft)
    if key in seen:
        return
    seen.add(key)
    drafts.append(draft)


def _fact_key(draft: FactDraft) -> tuple[str, str, int, str]:
    if draft.item_type in {"entity", "topic"}:
        return (draft.item_type, draft.label.casefold(), 0, "")
    return (
        draft.item_type,
        draft.label.casefold(),
        draft.evidence.page_start,
        draft.evidence.quote.casefold(),
    )


def _existing_fact_keys(database_path: Path, document_id: str) -> set[tuple[str, str, int, str]]:
    existing = list_discovery_items(database_path, document_id=document_id)
    keys = set()
    for item in existing:
        if item.extractor != DETERMINISTIC_FACT_EXTRACTOR:
            continue
        for evidence in item.evidence:
            if item.item_type in {"entity", "topic"}:
                keys.add((item.item_type, item.label.casefold(), 0, ""))
            else:
                keys.add(
                    (
                        item.item_type,
                        item.label.casefold(),
                        evidence.page_start,
                        evidence.quote.casefold(),
                    )
                )
    return keys


def _quote_for_match(text: str, start: int, end: int) -> str:
    sentence_start = max(text.rfind(".", 0, start), text.rfind("\n", 0, start)) + 1
    sentence_end_candidates = [
        index for index in (text.find(".", end), text.find("\n", end)) if index != -1
    ]
    sentence_end = min(sentence_end_candidates) + 1 if sentence_end_candidates else len(text)
    return _short_quote(text[sentence_start:sentence_end])


def _quote_for_keyword(text: str, keyword: str) -> str | None:
    match = re.search(rf"\b{re.escape(keyword)}\b", text, re.IGNORECASE)
    if match is None:
        return None
    return _quote_for_match(text, match.start(), match.end())


def _short_quote(text: str, *, max_chars: int = 280) -> str:
    normalized = _normalize_space(text)
    if len(normalized) <= max_chars:
        return normalized
    return normalized[: max_chars - 1].rstrip() + "…"


def _sentence_label(sentence: str, *, fallback: str) -> str:
    for pattern in (_ISO_DATE_PATTERN, _LONG_DATE_PATTERN):
        match = pattern.search(sentence)
        if match is not None:
            return _normalize_space(match.group(0))
    return _short_quote(sentence, max_chars=80) or fallback


def _looks_like_valid_date(value: str) -> bool:
    if _ISO_DATE_PATTERN.fullmatch(value):
        try:
            date.fromisoformat(value)
        except ValueError:
            return False
        return True
    return True


def _is_entity_candidate(label: str) -> bool:
    words = label.split()
    if len(words) < 2 or len(words) > 5:
        return False
    if label in _ENTITY_STOP_PHRASES:
        return False
    if words[0] in _ENTITY_REJECT_START_WORDS:
        return False
    if words[-1].lower() in {"and", "of", "from", "with"}:
        return False
    if any(word in _MONTH_NAMES for word in words):
        return False
    if label.lower().startswith("page "):
        return False
    if label.lower().startswith(("resolution ", "ordinance ")):
        return False
    if " report" in label.lower() or " date" in label.lower():
        return False
    if len(label) > 80:
        return False
    if label.isupper():
        return any(word in _ENTITY_SUFFIX_WORDS for word in words)
    if any(word in _ENTITY_SUFFIX_WORDS for word in words):
        return True
    return all(_is_titlelike_word(word) for word in words)


def _is_titlelike_word(word: str) -> bool:
    if word.lower() in {"and", "of", "the", "for"}:
        return True
    return bool(word) and word[0].isupper() and not word.isupper()


def _normalize_space(value: str) -> str:
    return " ".join(value.split()).strip()
