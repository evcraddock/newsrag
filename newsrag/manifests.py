from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path

import yaml

from newsrag.ingest import IngestError

ALLOWED_DOCUMENT_FIELDS = {
    "url",
    "title",
    "meeting_date",
    "body",
    "document_type",
    "jurisdiction",
}


@dataclass(frozen=True)
class ManifestDocument:
    """One validated document entry from a YAML manifest."""

    url: str
    metadata: dict[str, str]


@dataclass(frozen=True)
class Manifest:
    """Validated YAML manifest content."""

    documents: tuple[ManifestDocument, ...]


class ManifestError(IngestError):
    """Raised when a YAML ingest manifest is invalid."""


def load_manifest(path: Path) -> Manifest:
    """Load and validate one YAML ingest manifest."""

    resolved_path = path.expanduser().resolve()
    if not resolved_path.exists():
        raise ManifestError(f"Manifest path does not exist: {resolved_path}")
    if not resolved_path.is_file():
        raise ManifestError(f"Manifest path is not a file: {resolved_path}")

    raw_content = resolved_path.read_text(encoding="utf-8")
    try:
        loaded = yaml.safe_load(raw_content)
    except yaml.YAMLError as exc:
        raise ManifestError(f"Invalid YAML in {resolved_path}: {exc}") from exc

    if not isinstance(loaded, dict):
        raise ManifestError("Manifest must contain a top-level mapping")

    extra_top_level_keys = set(loaded) - {"documents"}
    if extra_top_level_keys:
        raise ManifestError(
            "Unsupported top-level manifest fields: " + ", ".join(sorted(extra_top_level_keys))
        )

    raw_documents = loaded.get("documents")
    if not isinstance(raw_documents, list):
        raise ManifestError("Manifest field 'documents' must be a list")
    if not raw_documents:
        raise ManifestError("Manifest field 'documents' must not be empty")

    seen_urls: set[str] = set()
    documents: list[ManifestDocument] = []
    for index, raw_document in enumerate(raw_documents, start=1):
        documents.append(_validate_document(raw_document, index=index, seen_urls=seen_urls))

    return Manifest(documents=tuple(documents))


def _validate_document(
    raw_document: object,
    *,
    index: int,
    seen_urls: set[str],
) -> ManifestDocument:
    if not isinstance(raw_document, dict):
        raise ManifestError(f"Manifest document #{index} must be a mapping")

    extra_fields = set(raw_document) - ALLOWED_DOCUMENT_FIELDS
    if extra_fields:
        raise ManifestError(
            f"Manifest document #{index} has unsupported fields: " + ", ".join(sorted(extra_fields))
        )

    raw_url = raw_document.get("url")
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise ManifestError(f"Manifest document #{index} is missing a non-empty 'url'")
    url = raw_url.strip()
    if url in seen_urls:
        raise ManifestError(f"Manifest contains duplicate URL: {url}")
    seen_urls.add(url)

    metadata: dict[str, str] = {}
    for key in ("title", "body", "document_type", "jurisdiction"):
        value = raw_document.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value.strip():
            raise ManifestError(
                f"Manifest document #{index} field '{key}' must be a non-empty string"
            )
        metadata[key] = value.strip()

    meeting_date = raw_document.get("meeting_date")
    if meeting_date is not None:
        if isinstance(meeting_date, date):
            normalized_meeting_date = meeting_date.isoformat()
        elif isinstance(meeting_date, str) and meeting_date.strip():
            normalized_meeting_date = meeting_date.strip()
            try:
                date.fromisoformat(normalized_meeting_date)
            except ValueError as exc:
                raise ManifestError(
                    f"Manifest document #{index} field 'meeting_date' must be YYYY-MM-DD"
                ) from exc
        else:
            raise ManifestError(
                f"Manifest document #{index} field 'meeting_date' must be a non-empty string"
            )

        metadata["meeting_date"] = normalized_meeting_date

    return ManifestDocument(url=url, metadata=metadata)
