from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from markdown_it import MarkdownIt, __version__

from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterResult,
    CanonicalSourceUnit,
    ExtractorIdentity,
)
from newsrag.sources import MARKDOWN_BLOCK_LOCATION_TYPE, MARKDOWN_MEDIA_TYPE
from newsrag.text_adapter import read_text_lines

MARKDOWN_EXTRACTOR = ExtractorIdentity("markdown-it-py", __version__)
_BLOCK_KINDS = {
    "heading_open": "heading",
    "paragraph_open": "paragraph",
    "table_open": "table",
    "fence": "code_block",
    "code_block": "code_block",
    "html_block": "html_block",
    "hr": "thematic_break",
}
_CONTAINERS = {"bullet_list", "ordered_list", "list_item", "blockquote"}


@dataclass(frozen=True)
class MarkdownSourceAdapter:
    """Parse block structure without rendering or altering authoritative source lines."""

    parser_version: str = MARKDOWN_EXTRACTOR.version or "unknown"
    max_nesting: int = 64
    format_version: str = "1"

    @property
    def media_types(self) -> Sequence[str]:
        return (MARKDOWN_MEDIA_TYPE,)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        if artifact.media_type.partition(";")[0].strip().lower() != MARKDOWN_MEDIA_TYPE:
            raise AdapterError("Markdown adapter requires text/markdown media type")
        try:
            lines, encoding = read_text_lines(
                artifact.artifact_path, artifact.media_type, allow_markup=True
            )
        except AdapterError as exc:
            raise AdapterError(
                str(exc).replace("Plain-text", "Markdown").replace("plain-text", "Markdown")
            ) from exc
        parser = MarkdownIt("commonmark", {"maxNesting": self.max_nesting}).enable("table")
        # Only source block maps are needed. Never render, parse inline links, or
        # resolve resources; keep inline syntax and HTML literal in source text.
        parser.disable("inline")
        tokens = parser.parse("\n".join(lines) + "\n")
        units: list[CanonicalSourceUnit] = []
        headings: list[tuple[int, str]] = []
        containers: list[str] = []
        cursor = 0

        def append(start: int, end: int, kind: str, **extra: object) -> None:
            path = [heading for _, heading in headings]
            label = f"line {start + 1}" if end == start + 1 else f"lines {start + 1}–{end}"
            units.append(
                CanonicalSourceUnit(
                    ordinal=len(units) + 1,
                    location_type=MARKDOWN_BLOCK_LOCATION_TYPE,
                    location={"line_start": start + 1, "line_end": end},
                    human_label=" — ".join((*path, label)),
                    normalized_text="\n".join(lines[start:end]),
                    structure={
                        "kind": kind,
                        "heading_path": path,
                        "containers": list(containers),
                        **extra,
                    },
                    extractor=MARKDOWN_EXTRACTOR,
                )
            )

        for index, token in enumerate(tokens):
            container, _, direction = token.type.rpartition("_")
            if container in _CONTAINERS:
                if direction == "open":
                    containers.append(container)
                elif direction == "close":
                    containers.pop()
            kind = _BLOCK_KINDS.get(token.type)
            if kind is None or token.map is None:
                continue
            start, end = token.map
            if start < cursor:
                continue  # A table owns all of its nested cell tokens.
            if not (0 <= start < end <= len(lines)):
                raise AdapterError("Markdown parser returned an invalid source line range")
            if start > cursor:
                append(
                    cursor,
                    start,
                    "blank" if not any(line.strip() for line in lines[cursor:start]) else "source",
                )
            extra: dict[str, object] = {}
            if kind == "heading":
                level = int(token.tag[1:])
                title = tokens[index + 1].content.strip()
                headings[:] = [(depth, text) for depth, text in headings if depth < level]
                if title:
                    headings.append((level, title))
                extra["heading_level"] = level
            if kind == "code_block":
                extra["info"] = token.info.strip()
                extra["fenced"] = token.type == "fence"
            append(start, end, kind, **extra)
            cursor = end
        if cursor < len(lines):
            append(
                cursor,
                len(lines),
                "blank" if not any(line.strip() for line in lines[cursor:]) else "source",
            )
        return AdapterResult(
            media_type=MARKDOWN_MEDIA_TYPE,
            units=tuple(units),
            extractor=MARKDOWN_EXTRACTOR,
            metadata_candidates={"text_encoding": encoding},
        )
