from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from newsrag.search import SearchFilters, SearchResult


class PacketError(Exception):
    """Raised when a source packet cannot be generated or written."""


def format_source_packet(
    *,
    query: str,
    results: Sequence[SearchResult],
    filters: SearchFilters | None = None,
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
                    f"{index}. **{result.citation}**",
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
            lines.append(f"- {format_source_list_entry(result)}")
    else:
        lines.append("- No sources found.")
    lines.append("")

    return "\n".join(lines)


def write_source_packet(path: Path, content: str, *, overwrite: bool = False) -> None:
    """Write a source packet to disk, refusing accidental overwrites by default."""

    if path.exists() and not overwrite:
        raise PacketError(f"Output file already exists: {path}. Use --overwrite to replace it.")
    path.write_text(content, encoding="utf-8")


def format_source_list_entry(result: SearchResult) -> str:
    """Format one source-list entry with available metadata."""

    details = [f"page {result.page_start}"]
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
    return f"{result.citation} ({'; '.join(details)})"


def _normalize_text(value: str) -> str:
    return " ".join(value.split())
