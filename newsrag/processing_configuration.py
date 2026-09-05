"""Stable, credential-free identities for reproducible processing requests."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import fields, is_dataclass
from functools import lru_cache
from importlib.metadata import PackageNotFoundError, version
from typing import Any

from newsrag.embeddings import EmbeddingMetadata

# Bump when derived text/chunk/index semantics change, independently of releases.
PROCESSING_VERSION = "1"
INDEX_VERSION = "1"


def processing_configuration(
    *, adapter: object, chunker: object, embedding_provider: object, options: dict[str, object]
) -> dict[str, Any]:
    """Describe processing inputs without capturing endpoints, keys, or source text."""

    metadata = getattr(embedding_provider, "metadata", None)
    embedding = (
        {"provider": metadata.provider, "model": metadata.model, "version": metadata.version}
        if isinstance(metadata, EmbeddingMetadata)
        else {"implementation": _class_name(embedding_provider)}
    )
    endpoint = getattr(embedding_provider, "base_url", None)
    if isinstance(endpoint, str):
        embedding["endpoint_fingerprint"] = hashlib.sha256(endpoint.encode()).hexdigest()
    packages = {}
    for name in ("ocrmypdf", "pymupdf", "pdfplumber", "beautifulsoup4", "lxml", "lancedb"):
        try:
            packages[name] = version(name)
        except PackageNotFoundError:
            packages[name] = "not-installed"
    return {
        "processing_version": PROCESSING_VERSION,
        "index_version": INDEX_VERSION,
        "adapter": _settings(adapter),
        "chunker": _settings(chunker),
        "embedding": embedding,
        "options": options,
        "packages": packages,
        "ocr_tools": _ocr_tool_versions(),
    }


def configuration_fingerprint(configuration: dict[str, Any]) -> str:
    """Hash a canonical processing configuration."""

    return hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@lru_cache(maxsize=1)
def _ocr_tool_versions() -> dict[str, str]:
    """Pin local OCR tool versions for the lifetime of a processing worker."""

    result = {}
    for tool in ("ocrmypdf", "tesseract", "gs"):
        try:
            completed = subprocess.run(
                [tool, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            lines = (completed.stdout or completed.stderr).strip().splitlines()
            result[tool] = lines[0] if completed.returncode == 0 and lines else "unavailable"
        except (OSError, subprocess.TimeoutExpired):
            result[tool] = "unavailable"
    return result


def _class_name(value: object) -> str:
    return f"{type(value).__module__}.{type(value).__qualname__}"


def _settings(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (tuple, list)):
        return [_settings(item) for item in value]
    if is_dataclass(value) and not isinstance(value, type):
        return {
            "implementation": _class_name(value),
            "settings": {
                field.name: _settings(getattr(value, field.name)) for field in fields(value)
            },
        }
    return {"implementation": _class_name(value)}
