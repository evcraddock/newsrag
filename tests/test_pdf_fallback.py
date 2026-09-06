from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fitz  # type: ignore[import-untyped]
import pytest

from newsrag import pdf_adapter as pdf
from newsrag.adapters import AdapterError, AdapterInput


@dataclass
class FailedOcr:
    returncode: int = 4

    def normalize_pdf(self, source_path: Path, output_path: Path) -> None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"Partial OCR output must never become evidence")
        raise pdf.OcrNormalizationError(source_path, self.returncode, "output validation failed")


@dataclass
class RecordingExtractor:
    pages: list[pdf.ExtractedPage] = field(default_factory=list)
    error: Exception | None = None
    extractor_name: str = "recording"
    paths: list[Path] = field(default_factory=list)

    def extract_pages(self, pdf_path: Path) -> list[pdf.ExtractedPage]:
        self.paths.append(pdf_path)
        if self.error is not None:
            raise self.error
        return self.pages


def make_pdf(path: Path, text: str = "Council approved the drainage project.") -> Path:
    with fitz.open() as document:
        page = document.new_page()
        if text:
            page.insert_text((72, 72), text)
        document.save(path)
    return path


def artifact(
    tmp_path: Path, *, mode: str = "auto", text: str = "Council approved the drainage project."
) -> AdapterInput:
    return AdapterInput(
        artifact_path=make_pdf(tmp_path / "original.pdf", text),
        content_hash="source-hash",
        media_type="application/pdf",
        work_dir=tmp_path / "work",
        options={"pdf_extractor": mode},
    )


@pytest.mark.parametrize("returncode", [4, 5])
def test_subprocess_ocr_failure_preserves_exit_code_and_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, returncode: int
) -> None:
    def run(command: list[str], **kwargs: Any) -> None:
        assert kwargs["check"] is True
        raise subprocess.CalledProcessError(returncode, command, stderr="validation diagnostic")

    monkeypatch.setattr("newsrag.pdf_adapter.subprocess.run", run)
    with pytest.raises(AdapterError) as caught:
        pdf.SubprocessOcrRunner().normalize_pdf(
            tmp_path / "original.pdf", tmp_path / "work" / "output.pdf"
        )
    assert isinstance(caught.value, pdf.OcrNormalizationError)
    assert caught.value.returncode == returncode
    assert "validation diagnostic" in str(caught.value)
    assert f"exit status {returncode}" in str(caught.value)
    assert isinstance(caught.value.__cause__, subprocess.CalledProcessError)


@pytest.mark.parametrize("mode", ["auto", "pymupdf", "pdfplumber", "table"])
def test_ocr_exit_four_extracts_real_original_pdf_with_selected_mode(
    tmp_path: Path, mode: str, caplog: pytest.LogCaptureFixture
) -> None:
    source = artifact(tmp_path, mode=mode)
    with caplog.at_level(logging.WARNING):
        result = pdf.PdfSourceAdapter(ocr_runner=FailedOcr()).extract(source)
    assert "Council approved" in result.units[0].normalized_text
    assert result.units[0].location == {"page_number": 1}
    assert result.units[0].extractor.name == (
        "pymupdf" if mode in {"auto", "pymupdf"} else "pdfplumber"
    )
    assert result.derived_artifact_path is None
    assert result.units[0].structure == {"text_source": "original", "ocr_exit_code": 4}
    assert "pdf_ocr_fallback" in caplog.text
    assert "Council approved" not in caplog.text
    assert source.artifact_path.read_bytes().startswith(b"%PDF-")


def test_fallback_never_reads_partial_ocr_output(tmp_path: Path) -> None:
    source = artifact(tmp_path)
    extractor = RecordingExtractor(pages=[pdf.ExtractedPage(1, "Original evidence", "recording")])
    result = pdf.PdfSourceAdapter(ocr_runner=FailedOcr(), text_extractor=extractor).extract(source)
    assert extractor.paths == [source.artifact_path]
    assert result.derived_artifact_path is None


@pytest.mark.parametrize("returncode", [3, 5, 130])
def test_other_ocr_exit_codes_do_not_trigger_fallback(tmp_path: Path, returncode: int) -> None:
    extractor = RecordingExtractor(pages=[pdf.ExtractedPage(1, "Must not be used")])
    with pytest.raises(pdf.OcrNormalizationError) as caught:
        pdf.PdfSourceAdapter(ocr_runner=FailedOcr(returncode), text_extractor=extractor).extract(
            artifact(tmp_path)
        )
    assert caught.value.returncode == returncode
    assert extractor.paths == []


def test_missing_ocr_program_remains_a_clear_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def run(*args: Any, **kwargs: Any) -> None:
        raise FileNotFoundError("ocrmypdf not installed")

    monkeypatch.setattr("newsrag.pdf_adapter.subprocess.run", run)
    extractor = RecordingExtractor()
    with pytest.raises(AdapterError, match="ocrmypdf not installed"):
        pdf.PdfSourceAdapter(text_extractor=extractor).extract(artifact(tmp_path))
    assert extractor.paths == []


@pytest.mark.parametrize(
    "primary_error",
    [AdapterError("primary broken"), RuntimeError("primary broken")],
    ids=["adapter-error", "extractor-exception"],
)
def test_auto_tries_secondary_after_primary_exception(
    tmp_path: Path, primary_error: Exception, caplog: pytest.LogCaptureFixture
) -> None:
    primary = RecordingExtractor(error=primary_error, extractor_name="pymupdf")
    secondary = RecordingExtractor(
        pages=[pdf.ExtractedPage(1, "Recovered", "pdfplumber")], extractor_name="pdfplumber"
    )
    result = pdf.FallbackTextExtractor(primary=primary, fallback=secondary).extract_pages(
        tmp_path / "input.pdf"
    )
    assert result == secondary.pages
    assert primary.paths == secondary.paths == [tmp_path / "input.pdf"]
    assert "pdf_text_fallback" in caplog.text and "reason=primary_error" in caplog.text
    assert "primary broken" not in caplog.text


def test_auto_preserves_both_extractor_failures(tmp_path: Path) -> None:
    extractor = pdf.FallbackTextExtractor(
        primary=RecordingExtractor(error=RuntimeError("primary broken"), extractor_name="pymupdf"),
        fallback=RecordingExtractor(
            error=AdapterError("secondary broken"), extractor_name="pdfplumber"
        ),
    )
    with pytest.raises(AdapterError) as caught:
        extractor.extract_pages(tmp_path / "input.pdf")
    assert "primary broken" in str(caught.value)
    assert "secondary broken" in str(caught.value)
    assert "pymupdf" in str(caught.value) and "pdfplumber" in str(caught.value)


@pytest.mark.parametrize(
    "empty_pages", [[], [pdf.ExtractedPage(1, " \n")]], ids=["no-pages", "blank-pages"]
)
def test_auto_rejects_two_unusable_extractions(
    tmp_path: Path, empty_pages: list[pdf.ExtractedPage]
) -> None:
    extractor = pdf.FallbackTextExtractor(
        primary=RecordingExtractor(pages=empty_pages),
        fallback=RecordingExtractor(pages=empty_pages),
    )
    with pytest.raises(AdapterError, match="no usable"):
        extractor.extract_pages(tmp_path / "input.pdf")


@pytest.mark.parametrize("mode", ["auto", "pdfplumber"])
def test_ocr_failure_and_empty_original_fail_with_actionable_combined_error(
    tmp_path: Path, mode: str
) -> None:
    with pytest.raises(AdapterError) as caught:
        pdf.PdfSourceAdapter(ocr_runner=FailedOcr()).extract(artifact(tmp_path, mode=mode, text=""))
    message = str(caught.value)
    assert "exit status 4" in message
    assert "original" in message and "no usable" in message
    assert "OCR" in message and "retry" in message


def test_combined_error_preserves_ocr_and_extractor_diagnostics(tmp_path: Path) -> None:
    with pytest.raises(AdapterError) as caught:
        pdf.PdfSourceAdapter(
            ocr_runner=FailedOcr(),
            text_extractor=RecordingExtractor(error=AdapterError("direct extraction broken")),
        ).extract(artifact(tmp_path))
    assert "output validation failed" in str(caught.value)
    assert "direct extraction broken" in str(caught.value)
    assert "retry" in str(caught.value)


def test_original_fallback_still_validates_page_order(tmp_path: Path) -> None:
    with pytest.raises(AdapterError, match="contiguous source order"):
        pdf.PdfSourceAdapter(
            ocr_runner=FailedOcr(),
            text_extractor=RecordingExtractor(pages=[pdf.ExtractedPage(2, "Bad ordinal")]),
        ).extract(artifact(tmp_path))
