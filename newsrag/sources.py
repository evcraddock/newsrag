from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

SOURCE_KIND_LOCAL_PATH = "local_path"
SOURCE_KIND_URL = "url"
SOURCE_TYPE_PDF = "pdf"
SOURCE_TYPE_HTML = "html"
SUPPORTED_SOURCE_TYPES = frozenset({SOURCE_TYPE_HTML, SOURCE_TYPE_PDF})
PDF_MEDIA_TYPE = "application/pdf"
HTML_MEDIA_TYPES = ("text/html", "application/xhtml+xml")
HTML_MAX_SOURCE_BYTES = 10 * 1024 * 1024
PAGE_LOCATION_TYPE = "page"
HTML_BLOCK_LOCATION_TYPE = "html_block"


def source_type_for_media_type(media_type: str | None) -> str | None:
    """Return the registered source type for one persisted artifact media type."""

    if media_type is None:
        return None
    normalized_media_type = media_type.partition(";")[0].strip().lower()
    if normalized_media_type == PDF_MEDIA_TYPE:
        return SOURCE_TYPE_PDF
    if normalized_media_type in HTML_MEDIA_TYPES:
        return SOURCE_TYPE_HTML
    return None


def media_types_for_source_type(source_type: str) -> tuple[str, ...]:
    """Return persisted media types represented by one registered source type."""

    if source_type == SOURCE_TYPE_PDF:
        return (PDF_MEDIA_TYPE,)
    if source_type == SOURCE_TYPE_HTML:
        return HTML_MEDIA_TYPES
    return ()


@dataclass(frozen=True)
class SourceIdentity:
    """Stable identity fields for one submitted source reference."""

    id: str
    kind: str
    submitted_reference: str
    normalized_reference: str
    resolved_reference: str | None


def build_source_identity(
    *,
    source_path: Path,
    source_url: str | None,
    resolved_reference: str | None = None,
) -> SourceIdentity:
    """Build the corpus-local identity fields for a URL or local path."""

    if source_url is not None:
        submitted_reference = source_url.strip()
        normalized_reference = normalize_url_reference(submitted_reference)
        return SourceIdentity(
            id=_stable_id("source", SOURCE_KIND_URL, normalized_reference),
            kind=SOURCE_KIND_URL,
            submitted_reference=submitted_reference,
            normalized_reference=normalized_reference,
            resolved_reference=resolved_reference or normalized_reference,
        )

    submitted_reference = str(source_path)
    normalized_reference = str(Path(os.path.abspath(os.path.expanduser(submitted_reference))))
    return SourceIdentity(
        id=_stable_id("source", SOURCE_KIND_LOCAL_PATH, normalized_reference),
        kind=SOURCE_KIND_LOCAL_PATH,
        submitted_reference=submitted_reference,
        normalized_reference=normalized_reference,
        resolved_reference=resolved_reference or str(Path(normalized_reference).resolve()),
    )


def normalize_url_reference(url: str) -> str:
    """Normalize only the URL components defined by the source identity policy."""

    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"

    port = parsed.port
    if port is not None and not (
        (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    ):
        host = f"{host}:{port}"

    normalized = SplitResult(
        scheme=scheme,
        netloc=host,
        path=parsed.path,
        query=parsed.query,
        fragment="",
    )
    return urlunsplit(normalized)


def artifact_id_for_hash(content_hash: str) -> str:
    """Return a stable artifact ID for one raw content hash."""

    return _stable_id("artifact", content_hash)


def source_unit_id_for_page(page_id: str) -> str:
    """Return a stable source-unit ID for one existing page record."""

    return _stable_id("source-unit", page_id)


def source_unit_id_for_ordinal(document_id: str, ordinal: int) -> str:
    """Return a stable source-unit ID for a non-page canonical unit."""

    return _stable_id("source-unit", document_id, str(ordinal))


def _stable_id(prefix: str, *parts: str) -> str:
    identity = "\0".join(parts).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(identity).hexdigest()}"
