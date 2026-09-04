from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


class AdapterError(Exception):
    """Raised when a source adapter cannot validate or extract an artifact."""


class AdapterSelectionError(AdapterError):
    """Raised when an acquired artifact cannot select one registered adapter."""


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


@dataclass(frozen=True)
class RegisteredSourceAdapter:
    """One source type and the evidence that may select its adapter."""

    source_type: str
    media_type: str
    extensions: tuple[str, ...]
    signatures: tuple[bytes, ...]
    adapter: SourceAdapter


class SourceAdapterRegistry:
    """Select one registered adapter from explicit or acquired format evidence."""

    def __init__(self, registrations: Sequence[RegisteredSourceAdapter]) -> None:
        self._registrations = tuple(registrations)
        source_types = [registration.source_type for registration in self._registrations]
        if len(source_types) != len(set(source_types)):
            raise ValueError("Source adapter registrations must use unique source types")
        if not self._registrations:
            raise ValueError("At least one source adapter must be registered")

    @property
    def source_types(self) -> tuple[str, ...]:
        """Return registered source-type names in registration order."""

        return tuple(registration.source_type for registration in self._registrations)

    def select(
        self,
        *,
        artifact_path: Path,
        source_type_hint: str | None,
        reported_media_type: str | None,
        filename: str,
    ) -> RegisteredSourceAdapter:
        """Select an adapter using hint, media type, signature, then extension."""

        if source_type_hint is not None:
            normalized_hint = source_type_hint.strip().lower()
            for registration in self._registrations:
                if registration.source_type == normalized_hint:
                    return registration
            raise AdapterSelectionError(
                f"Unsupported source type {source_type_hint!r}; expected one of: "
                + ", ".join(self.source_types)
            )

        normalized_media_type = (reported_media_type or "").partition(";")[0].strip().lower()
        media_matches = tuple(
            registration
            for registration in self._registrations
            if normalized_media_type == registration.media_type
        )
        selected = _one_adapter_match(media_matches, evidence="reported media type")
        if selected is not None:
            return selected

        maximum_signature_bytes = max(
            (
                len(signature)
                for registration in self._registrations
                for signature in registration.signatures
            ),
            default=0,
        )
        try:
            with artifact_path.open("rb") as artifact_file:
                header = artifact_file.read(maximum_signature_bytes)
        except OSError as exc:
            raise AdapterSelectionError(
                f"Could not inspect acquired artifact ({type(exc).__name__})"
            ) from exc
        signature_matches = tuple(
            registration
            for registration in self._registrations
            if any(header.startswith(signature) for signature in registration.signatures)
        )
        selected = _one_adapter_match(signature_matches, evidence="content signature")
        if selected is not None:
            return selected

        extension = Path(filename).suffix.lower()
        extension_matches = tuple(
            registration
            for registration in self._registrations
            if extension in registration.extensions
        )
        selected = _one_adapter_match(extension_matches, evidence="filename extension")
        if selected is not None:
            return selected

        raise AdapterSelectionError(
            "Unsupported source type; provide --type with one of: " + ", ".join(self.source_types)
        )


def _one_adapter_match(
    matches: Sequence[RegisteredSourceAdapter],
    *,
    evidence: str,
) -> RegisteredSourceAdapter | None:
    if len(matches) > 1:
        source_types = ", ".join(registration.source_type for registration in matches)
        raise AdapterSelectionError(f"Ambiguous {evidence}; matched: {source_types}")
    return matches[0] if matches else None
