from __future__ import annotations

import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

import fitz  # type: ignore[import-untyped]
import pdfplumber

from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterResult,
    CanonicalSourceUnit,
    ExtractorIdentity,
)
from newsrag.sources import PAGE_LOCATION_TYPE, PDF_MEDIA_TYPE

PDF_EXTRACTOR_AUTO: Literal["auto"] = "auto"
PDF_EXTRACTOR_PYMUPDF: Literal["pymupdf"] = "pymupdf"
PDF_EXTRACTOR_PDFPLUMBER: Literal["pdfplumber"] = "pdfplumber"
PDF_EXTRACTOR_TABLE: Literal["table"] = "table"
PDF_EXTRACTOR_MODES = frozenset(
    {
        PDF_EXTRACTOR_AUTO,
        PDF_EXTRACTOR_PYMUPDF,
        PDF_EXTRACTOR_PDFPLUMBER,
        PDF_EXTRACTOR_TABLE,
    }
)
PdfExtractorMode = Literal["auto", "pymupdf", "pdfplumber", "table"]


@dataclass(frozen=True)
class ExtractedPage:
    """Canonical page text extracted from one PDF page."""

    page_number: int
    text: str
    extractor: str = "unknown"


class OcrRunner(Protocol):
    """Protocol for PDF OCR normalization."""

    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        """Create an OCR-normalized PDF artifact."""


class TextExtractor(Protocol):
    """Protocol for page-text extraction."""

    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        """Extract canonical page text from one PDF."""


@dataclass(frozen=True)
class SubprocessOcrRunner:
    """OCR normalization backed by the `ocrmypdf` CLI."""

    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.run(
                [
                    "ocrmypdf",
                    "--skip-text",
                    "--quiet",
                    str(source_path),
                    str(output_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or exc.stdout or str(exc)).strip()
            raise AdapterError(f"ocrmypdf failed for {source_path}: {detail}") from exc


@dataclass(frozen=True)
class PyMuPdfTextExtractor:
    """Text extraction backed by PyMuPDF."""

    extractor_name: str = PDF_EXTRACTOR_PYMUPDF

    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        try:
            pages: list[ExtractedPage] = []
            with fitz.open(pdf_path) as document:
                for index, page in enumerate(document, start=1):
                    pages.append(
                        ExtractedPage(
                            page_number=index,
                            text=page.get_text().strip(),
                            extractor=self.extractor_name,
                        )
                    )
            return pages
        except Exception as exc:
            raise AdapterError(f"PyMuPDF extraction failed for {pdf_path}: {exc}") from exc


@dataclass(frozen=True)
class PdfPlumberTextExtractor:
    """Text extraction backed by pdfplumber."""

    extractor_name: str = PDF_EXTRACTOR_PDFPLUMBER

    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        try:
            pages: list[ExtractedPage] = []
            with pdfplumber.open(pdf_path) as document:
                for index, page in enumerate(document.pages, start=1):
                    text = page.extract_text() or ""
                    pages.append(
                        ExtractedPage(
                            page_number=index,
                            text=text.strip(),
                            extractor=self.extractor_name,
                        )
                    )
            return pages
        except Exception as exc:
            raise AdapterError(f"pdfplumber extraction failed for {pdf_path}: {exc}") from exc


@dataclass(frozen=True)
class FallbackTextExtractor:
    """Run a primary extractor and fall back when page text quality is unusable."""

    primary: TextExtractor = field(default_factory=PyMuPdfTextExtractor)
    fallback: TextExtractor = field(default_factory=PdfPlumberTextExtractor)

    @property
    def extractor_name(self) -> str:
        return f"{_extractor_name(self.primary)}+{_extractor_name(self.fallback)}"

    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        primary_pages = _extract_with_stage_context(
            self.primary,
            pdf_path,
            stage="primary",
        )
        if not _has_usable_page_text(primary_pages):
            return _extract_with_stage_context(
                self.fallback,
                pdf_path,
                stage="fallback",
            )
        return primary_pages


def build_pdf_text_extractor(mode: PdfExtractorMode = PDF_EXTRACTOR_AUTO) -> TextExtractor:
    """Build the configured PDF text extraction path."""

    if mode == PDF_EXTRACTOR_AUTO:
        return FallbackTextExtractor()
    if mode == PDF_EXTRACTOR_PYMUPDF:
        return PyMuPdfTextExtractor()
    if mode in {PDF_EXTRACTOR_PDFPLUMBER, PDF_EXTRACTOR_TABLE}:
        return PdfPlumberTextExtractor()
    raise AdapterError(f"Unsupported PDF extractor mode: {mode}")


def normalize_pdf_extractor_mode(value: str | None) -> PdfExtractorMode:
    """Normalize and validate a PDF extractor mode option."""

    if value is None or not value.strip():
        return PDF_EXTRACTOR_AUTO

    normalized = value.strip().lower()
    if normalized == PDF_EXTRACTOR_AUTO:
        return PDF_EXTRACTOR_AUTO
    if normalized == PDF_EXTRACTOR_PYMUPDF:
        return PDF_EXTRACTOR_PYMUPDF
    if normalized == PDF_EXTRACTOR_PDFPLUMBER:
        return PDF_EXTRACTOR_PDFPLUMBER
    if normalized == PDF_EXTRACTOR_TABLE:
        return PDF_EXTRACTOR_TABLE

    allowed = ", ".join(sorted(PDF_EXTRACTOR_MODES))
    raise AdapterError(f"Unsupported PDF extractor mode: {value}; expected one of: {allowed}")


@dataclass(frozen=True)
class PdfSourceAdapter:
    """Validate a raw PDF, normalize it through OCR, and emit page source units."""

    ocr_runner: OcrRunner = field(default_factory=SubprocessOcrRunner)
    text_extractor: TextExtractor | None = None

    @property
    def media_types(self) -> Sequence[str]:
        return (PDF_MEDIA_TYPE,)

    def extract(self, artifact: AdapterInput) -> AdapterResult:
        _validate_pdf_artifact(artifact)
        normalized_path = artifact.work_dir / f"{artifact.content_hash}.pdf"
        try:
            self.ocr_runner.normalize_pdf(artifact.artifact_path, normalized_path)
        except AdapterError:
            raise
        except Exception as exc:
            raise AdapterError(
                f"PDF OCR normalization failed for {artifact.artifact_path}: {exc}"
            ) from exc

        extractor = self.text_extractor or build_pdf_text_extractor(
            _extractor_mode_from_options(artifact.options)
        )
        try:
            pages = extractor.extract_pages(normalized_path)
        except AdapterError as exc:
            raise AdapterError(f"Failed extracting PDF text from {normalized_path}: {exc}") from exc
        except Exception as exc:
            raise AdapterError(f"Failed extracting PDF text from {normalized_path}: {exc}") from exc

        _validate_page_order(pages)
        units = tuple(
            CanonicalSourceUnit(
                ordinal=page.page_number,
                location_type=PAGE_LOCATION_TYPE,
                location={"page_number": page.page_number},
                human_label=f"p. {page.page_number}",
                normalized_text=page.text,
                structure={},
                extractor=ExtractorIdentity(name=page.extractor),
            )
            for page in pages
        )
        return AdapterResult(
            media_type=PDF_MEDIA_TYPE,
            units=units,
            extractor=ExtractorIdentity(name=_extractor_name(extractor)),
            derived_artifact_path=normalized_path,
        )


def _validate_pdf_artifact(artifact: AdapterInput) -> None:
    normalized_media_type = artifact.media_type.partition(";")[0].strip().lower()
    if normalized_media_type != PDF_MEDIA_TYPE:
        raise AdapterError(f"PDF adapter does not accept media type {artifact.media_type!r}")
    try:
        with artifact.artifact_path.open("rb") as artifact_file:
            signature = artifact_file.read(5)
    except OSError as exc:
        raise AdapterError(f"Failed reading PDF artifact {artifact.artifact_path}: {exc}") from exc
    if signature != b"%PDF-":
        raise AdapterError(f"PDF artifact has an invalid signature: {artifact.artifact_path}")


def _extractor_mode_from_options(options: Mapping[str, object]) -> PdfExtractorMode:
    value = options.get("pdf_extractor")
    if value is None:
        return PDF_EXTRACTOR_AUTO
    if not isinstance(value, str):
        raise AdapterError("PDF adapter option pdf_extractor must be a string")
    return normalize_pdf_extractor_mode(value)


def _extract_with_stage_context(
    extractor: TextExtractor,
    pdf_path: Path,
    *,
    stage: str,
) -> list[ExtractedPage]:
    extractor_name = _extractor_name(extractor)
    try:
        return extractor.extract_pages(pdf_path)
    except AdapterError as exc:
        raise AdapterError(
            f"{stage} PDF text extraction with {extractor_name} failed for {pdf_path}: {exc}"
        ) from exc
    except Exception as exc:
        raise AdapterError(
            f"{stage} PDF text extraction with {extractor_name} failed for {pdf_path}: {exc}"
        ) from exc


def _extractor_name(extractor: TextExtractor) -> str:
    name = getattr(extractor, "extractor_name", extractor.__class__.__name__)
    return str(name)


def _has_usable_page_text(pages: Sequence[ExtractedPage]) -> bool:
    return any(page.text.strip() for page in pages)


def _validate_page_order(pages: Sequence[ExtractedPage]) -> None:
    page_numbers = [page.page_number for page in pages]
    if page_numbers != list(range(1, len(pages) + 1)):
        raise AdapterError("PDF adapter returned pages outside contiguous source order")
