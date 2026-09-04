from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import pytest

from newsrag.adapters import (
    AdapterError,
    AdapterInput,
    AdapterSelectionError,
    RegisteredSourceAdapter,
    SourceAdapterRegistry,
)
from newsrag.pdf_adapter import ExtractedPage, PdfSourceAdapter


@dataclass(frozen=True)
class FakeOcrRunner:
    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(source_path.read_bytes())


@dataclass(frozen=True)
class FakeTextExtractor:
    pages: tuple[ExtractedPage, ...]
    extractor_name: str = "fake-pdf"

    def extract_pages(self, pdf_path: Path) -> list[ExtractedPage]:
        assert pdf_path.read_bytes().startswith(b"%PDF-")
        return list(self.pages)


def test_pdf_adapter_emits_ordered_page_source_units(tmp_path: Path) -> None:
    artifact_path = tmp_path / "packet.pdf"
    artifact_path.write_bytes(b"%PDF-1.4\nmock")
    adapter = PdfSourceAdapter(
        ocr_runner=FakeOcrRunner(),
        text_extractor=FakeTextExtractor(
            pages=(
                ExtractedPage(page_number=1, text="Agenda", extractor="pymupdf"),
                ExtractedPage(page_number=2, text="Public comment", extractor="pdfplumber"),
            )
        ),
    )

    result = adapter.extract(
        AdapterInput(
            artifact_path=artifact_path,
            content_hash="hash-1",
            media_type="application/pdf",
            work_dir=tmp_path / "normalized",
            options={"pdf_extractor": "auto"},
        )
    )

    assert result.media_type == "application/pdf"
    assert result.derived_artifact_path == tmp_path / "normalized" / "hash-1.pdf"
    assert result.extractor.name == "fake-pdf"
    assert result.extractor.version is None
    assert [unit.ordinal for unit in result.units] == [1, 2]
    assert [unit.location_type for unit in result.units] == ["page", "page"]
    assert [unit.location for unit in result.units] == [
        {"page_number": 1},
        {"page_number": 2},
    ]
    assert [unit.human_label for unit in result.units] == ["p. 1", "p. 2"]
    assert [unit.normalized_text for unit in result.units] == ["Agenda", "Public comment"]
    assert [unit.extractor.name for unit in result.units] == ["pymupdf", "pdfplumber"]


@pytest.mark.parametrize(
    ("media_type", "content"),
    [
        ("text/html", b"%PDF-1.4\nmock"),
        ("application/pdf", b"not a pdf"),
    ],
)
def test_pdf_adapter_rejects_invalid_artifacts(
    tmp_path: Path,
    media_type: str,
    content: bytes,
) -> None:
    artifact_path = tmp_path / "input.bin"
    artifact_path.write_bytes(content)
    adapter = PdfSourceAdapter(
        ocr_runner=FakeOcrRunner(),
        text_extractor=FakeTextExtractor(pages=()),
    )

    with pytest.raises(AdapterError, match="PDF"):
        adapter.extract(
            AdapterInput(
                artifact_path=artifact_path,
                content_hash="hash-1",
                media_type=media_type,
                work_dir=tmp_path / "normalized",
                options=_empty_options(),
            )
        )


def test_adapter_registry_uses_hint_media_signature_and_extension_without_extracting(
    tmp_path: Path,
) -> None:
    artifact_path = tmp_path / "artifact"
    artifact_path.write_bytes(b"%PDF-1.4\nmock")
    adapter = PdfSourceAdapter(
        ocr_runner=FakeOcrRunner(),
        text_extractor=FakeTextExtractor(pages=()),
    )
    registration = RegisteredSourceAdapter(
        source_type="pdf",
        media_type="application/pdf",
        extensions=(".pdf",),
        signatures=(b"%PDF-",),
        adapter=adapter,
    )
    registry = SourceAdapterRegistry((registration,))

    assert (
        registry.select(
            artifact_path=artifact_path,
            source_type_hint="pdf",
            reported_media_type="text/plain",
            filename="wrong.txt",
        )
        is registration
    )
    assert (
        registry.select(
            artifact_path=artifact_path,
            source_type_hint=None,
            reported_media_type="application/pdf",
            filename="wrong.txt",
        )
        is registration
    )
    assert (
        registry.select(
            artifact_path=artifact_path,
            source_type_hint=None,
            reported_media_type=None,
            filename="no-extension",
        )
        is registration
    )

    artifact_path.write_bytes(b"unknown")
    assert (
        registry.select(
            artifact_path=artifact_path,
            source_type_hint=None,
            reported_media_type=None,
            filename="packet.PDF",
        )
        is registration
    )


def test_adapter_registry_rejects_unsupported_evidence(tmp_path: Path) -> None:
    artifact_path = tmp_path / "unknown.bin"
    artifact_path.write_bytes(b"unknown")
    adapter = PdfSourceAdapter(
        ocr_runner=FakeOcrRunner(),
        text_extractor=FakeTextExtractor(pages=()),
    )
    registry = SourceAdapterRegistry(
        (
            RegisteredSourceAdapter(
                source_type="pdf",
                media_type="application/pdf",
                extensions=(".pdf",),
                signatures=(b"%PDF-",),
                adapter=adapter,
            ),
        )
    )

    with pytest.raises(AdapterSelectionError, match="provide --type"):
        registry.select(
            artifact_path=artifact_path,
            source_type_hint=None,
            reported_media_type="application/octet-stream",
            filename=artifact_path.name,
        )


def _empty_options() -> Mapping[str, object]:
    return {}
