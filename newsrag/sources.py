from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import SplitResult, urlsplit, urlunsplit

SOURCE_KIND_LOCAL_PATH = "local_path"
SOURCE_KIND_URL = "url"
PDF_MEDIA_TYPE = "application/pdf"
PAGE_LOCATION_TYPE = "page"


@dataclass(frozen=True)
class SourceIdentity:
    """Stable identity fields for one submitted source reference."""

    id: str
    kind: str
    submitted_reference: str
    normalized_reference: str
    resolved_reference: str | None


def build_source_identity(*, source_path: Path, source_url: str | None) -> SourceIdentity:
    """Build the corpus-local identity fields for a URL or local path."""

    if source_url is not None:
        submitted_reference = source_url.strip()
        normalized_reference = normalize_url_reference(submitted_reference)
        return SourceIdentity(
            id=_stable_id("source", SOURCE_KIND_URL, normalized_reference),
            kind=SOURCE_KIND_URL,
            submitted_reference=submitted_reference,
            normalized_reference=normalized_reference,
            resolved_reference=normalized_reference,
        )

    submitted_reference = str(source_path)
    normalized_reference = str(source_path.expanduser().resolve())
    return SourceIdentity(
        id=_stable_id("source", SOURCE_KIND_LOCAL_PATH, normalized_reference),
        kind=SOURCE_KIND_LOCAL_PATH,
        submitted_reference=submitted_reference,
        normalized_reference=normalized_reference,
        resolved_reference=normalized_reference,
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
