from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from newsrag.acquisition import AcquisitionError, safe_url_reference, validate_url_submission
from newsrag.ingest import IngestError, normalize_source_type_hint
from newsrag.sources import normalize_url_reference

ALLOWED_DOCUMENT_FIELDS = {
    "source",
    "type",
    "title",
    "meeting_date",
    "body",
    "document_type",
    "jurisdiction",
}


@dataclass(frozen=True)
class ManifestDocument:
    """One validated document entry from a YAML manifest."""

    source: str
    source_type: str | None
    is_url: bool
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

    seen_sources: set[str] = set()
    documents: list[ManifestDocument] = []
    for index, raw_document in enumerate(raw_documents, start=1):
        documents.append(
            _validate_document(
                raw_document,
                index=index,
                manifest_directory=resolved_path.parent,
                seen_sources=seen_sources,
            )
        )

    return Manifest(documents=tuple(documents))


def _validate_document(
    raw_document: object,
    *,
    index: int,
    manifest_directory: Path,
    seen_sources: set[str],
) -> ManifestDocument:
    if not isinstance(raw_document, dict):
        raise ManifestError(f"Manifest document #{index} must be a mapping")

    extra_fields = set(raw_document) - ALLOWED_DOCUMENT_FIELDS
    if extra_fields:
        raise ManifestError(
            f"Manifest document #{index} has unsupported fields: " + ", ".join(sorted(extra_fields))
        )

    raw_source = raw_document.get("source")
    if not isinstance(raw_source, str) or not raw_source.strip():
        raise ManifestError(f"Manifest document #{index} is missing a non-empty 'source'")
    source = raw_source.strip()
    is_url, identity = _manifest_source_identity(
        source,
        index=index,
        manifest_directory=manifest_directory,
    )
    if identity in seen_sources:
        display_source = safe_url_reference(source) if is_url else source
        raise ManifestError(f"Manifest contains duplicate source: {display_source}")
    seen_sources.add(identity)

    raw_source_type = raw_document.get("type")
    if raw_source_type is not None and not isinstance(raw_source_type, str):
        raise ManifestError(f"Manifest document #{index} field 'type' must be a string")
    try:
        source_type = normalize_source_type_hint(raw_source_type)
    except IngestError as exc:
        raise ManifestError(f"Manifest document #{index}: {exc}") from exc

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

    return ManifestDocument(
        source=source,
        source_type=source_type,
        is_url=is_url,
        metadata=metadata,
    )


def _manifest_source_identity(
    source: str,
    *,
    index: int,
    manifest_directory: Path,
) -> tuple[bool, str]:
    try:
        parsed = urlsplit(source)
        _ = parsed.port
    except ValueError as exc:
        raise ManifestError(f"Manifest document #{index} has a malformed source URL") from exc

    if parsed.scheme.lower() in {"http", "https"}:
        try:
            validate_url_submission(source)
        except AcquisitionError as exc:
            raise ManifestError(f"Manifest document #{index}: {exc}") from exc
        if parsed.hostname is None:
            raise ManifestError(f"Manifest document #{index} URL must include a host")
        return True, f"url:{normalize_url_reference(source)}"

    if parsed.scheme:
        raise ManifestError(
            f"Manifest document #{index} uses unsupported source scheme {parsed.scheme!r}"
        )

    local_path = Path(os.path.expanduser(source))
    if not local_path.is_absolute():
        local_path = manifest_directory / local_path
    return False, f"path:{os.path.abspath(local_path)}"
