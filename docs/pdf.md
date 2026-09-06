# PDF sources and OCR recovery

NewsRAG normally runs `ocrmypdf --skip-text --quiet` on a saved original PDF and extracts page text from the normalized result. Page numbers and citations keep their original, one-based order.

## Extractor selection

```bash
newsrag ingest ./agenda.pdf
newsrag ingest ./agenda.pdf --pdf-extractor pdfplumber
newsrag ingest https://example.gov/agenda.pdf --pdf-extractor auto
```

- `auto` tries PyMuPDF first, then pdfplumber if the primary extractor raises an error or returns no usable text. If both paths fail or produce only empty/whitespace text, ingestion fails with both stages identified.
- `pymupdf` and `pdfplumber` select one extractor explicitly; they do not silently switch to another extractor.
- `table` currently selects the existing pdfplumber text-extraction path; it does not introduce spreadsheet-style cell extraction or arithmetic.

Selecting an extractor does **not** skip OCR normalization.

## OCR exit status 4

If OCRmyPDF exits with status 4, NewsRAG automatically attempts the selected text-extraction path on the saved **original PDF**, ignoring any partially written OCR output. Other exit statuses, missing executables, and unrelated normalization errors still fail rather than triggering this recovery.

A successful recovery publishes the original PDF's existing text, actual per-page extractor identities, and unchanged page citations. The current processing generation has no normalized artifact; source units record `text_source: original` and `ocr_exit_code: 4`. A warning identifies the fallback without logging document text. An `auto` extractor fallback is also logged with the extractor names and failure category.

**This recovery does not perform OCR on image-only pages.** Such pages can remain empty in an otherwise text-readable PDF. If direct extraction yields no usable text, ingestion fails instead of claiming success. If direct extraction raises an error, the final diagnostic includes both the OCR failure and the extraction failure.

## Recovery and retries

When both paths fail, repair/re-export the PDF or resolve the reported OCR problem, then retry the failed job:

```bash
newsrag jobs show <job-id>
newsrag jobs retry <job-id>
```

Normal ingestion retries still require the source to be available. Existing published documents can be reprocessed from saved bytes, without refetching or requiring the original local file:

```bash
newsrag reprocess <document-id> --pdf-extractor pdfplumber
```

Failed attempts do not publish partial document evidence. Reprocessing preserves prior generations, normalized artifacts, and historical packet provenance. PDF processing configuration includes an adapter format version so the changed recovery behavior is distinguished from older processing recipes; duplicate ingestion still preserves existing document identity.

## Disposable smoke verification

`uv run python scripts/smoke_pdf_fallback.py` starts the real CLI/daemon using `make dev`, a temporary corpus/configuration, an isolated Overmind socket, and localhost mock embeddings. A private OCR command shim deterministically emits statuses 4 and 5; PyMuPDF and pdfplumber remain real. It exercises extraction, page citations, packets, duplicate handling, saved-byte reprocessing, empty-text atomic failure, non-4 rejection, and retry recovery. It does not touch installed corpora/services.

Optionally pass `--source-pdf /path/to/agenda.pdf` to exercise the same exit-4 recovery with an additional real PDF. This injects the normalization failure for repeatability; it does not assert that the current installed OCRmyPDF naturally fails on that file.
