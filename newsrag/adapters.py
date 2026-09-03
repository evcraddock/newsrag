from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class AdapterError(Exception):
    """Raised when a source adapter cannot validate or extract an artifact."""


@dataclass(frozen=True)
class ExtractorIdentity:
    """Stable identity metadata for one source extractor."""

    name: str
    version: str | None = None


@dataclass(frozen=True)
class AdapterInput:
    """One immutable raw artifact supplied to a format adapter."""

    artifact_path: Path
    content_hash: str
    media_type: str
    work_dir: Path
    options: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class CanonicalSourceUnit:
    """One ordered, source-neutral unit emitted by an adapter."""

    ordinal: int
    location_type: str
    location: dict[str, object]
    human_label: str
    normalized_text: str
    structure: dict[str, object]
    extractor: ExtractorIdentity


@dataclass(frozen=True)
class AdapterResult:
    """Validated canonical output from one source adapter."""

    media_type: str
    units: tuple[CanonicalSourceUnit, ...]
    extractor: ExtractorIdentity
    derived_artifact_path: Path | None = None


class SourceAdapter(Protocol):
    """Validate one raw artifact and return ordered canonical source units."""

    @property
    def media_types(self) -> Sequence[str]:
        """Return the validated media types accepted by this adapter."""

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        """Validate and extract one immutable raw artifact."""
