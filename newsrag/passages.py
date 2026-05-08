from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

_PAGE_HEADER_PATTERNS = (
    re.compile(r"^Page\s+\d+\s+of\s+\d+$", re.IGNORECASE),
    re.compile(r"^[A-Za-z]+\s+\d{1,2},\s+\d{4}$"),
)


@dataclass(frozen=True)
class PassageDraft:
    """One passage extracted from a larger stored chunk."""

    text: str
    page_start: int
    page_end: int


@dataclass(frozen=True)
class PassageRow:
    """One durable passage row ready for SQLite persistence."""

    id: str
    chunk_id: str
    document_id: str
    page_start: int
    page_end: int
    ordinal: int
    text: str


def build_passage_rows(
    *,
    chunk_id: str,
    document_id: str,
    page_start: int,
    page_end: int,
    text: str,
) -> list[PassageRow]:
    """Split one chunk text into durable passage rows."""

    drafts = split_chunk_text_into_passages(text, page_start=page_start, page_end=page_end)
    return [
        PassageRow(
            id=f"passage-{chunk_id}-{index:03d}",
            chunk_id=chunk_id,
            document_id=document_id,
            page_start=draft.page_start,
            page_end=draft.page_end,
            ordinal=index,
            text=draft.text,
        )
        for index, draft in enumerate(drafts, start=1)
    ]


def split_chunk_text_into_passages(
    text: str,
    *,
    page_start: int,
    page_end: int,
) -> list[PassageDraft]:
    """Heuristically split one stored chunk into searchable passages."""

    lines = [_normalize_line(line) for line in text.splitlines()]
    passages: list[str] = []
    current: list[str] = []

    def flush_current() -> None:
        if not current:
            return
        candidate = _join_lines(current)
        current.clear()
        if _should_keep_passage(candidate):
            passages.append(candidate)

    for line in lines:
        if not line:
            flush_current()
            continue
        if _is_page_header_or_footer(line):
            flush_current()
            continue
        if _starts_new_passage(line):
            flush_current()
            current.append(line)
            continue
        if _is_low_value_heading(line):
            flush_current()
            continue
        current.append(line)

    flush_current()

    if not passages:
        fallback = _join_lines(
            line for line in lines if line and not _is_page_header_or_footer(line)
        )
        if fallback:
            passages = [fallback]

    return [
        PassageDraft(text=passage, page_start=page_start, page_end=page_end) for passage in passages
    ]


def _normalize_line(line: str) -> str:
    return " ".join(line.split()).strip()


def _join_lines(lines: Iterable[str]) -> str:
    return " ".join(line for line in lines if line).strip()


def _is_page_header_or_footer(line: str) -> bool:
    if not line:
        return True
    if line == "-":
        return True
    if line.endswith(" eventbrite.com") or line.startswith("www."):
        return True
    if "City Manager's Report" in line or "City Manager’s Report" in line:
        return True
    return any(pattern.match(line) for pattern in _PAGE_HEADER_PATTERNS)


def _starts_new_passage(line: str) -> bool:
    return line.startswith("•")


def _is_low_value_heading(line: str) -> bool:
    if len(line) <= 20 and line.endswith(":"):
        return True
    if "The following is a brief overview" in line:
        return True
    return False


def _should_keep_passage(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    if len(normalized) >= 40:
        return True
    return normalized.startswith("•")
