# pdfplumber fallback extraction path

## Goal

Improve PDF text extraction robustness by adding a pdfplumber fallback or table-oriented extraction path behind the extraction interface.

## Requirements

- Keep PyMuPDF as the primary page text extractor.
- Add pdfplumber as a fallback when extraction quality is low or when table-oriented extraction is requested.
- Preserve page numbers and citation provenance regardless of extractor path.
- Record which extractor path produced page text or supplemental table text where useful.
- Keep tests mock-heavy and avoid requiring large real PDF fixtures.

## Acceptance criteria

- [ ] The extraction interface can choose PyMuPDF or pdfplumber under test.
- [ ] Low-quality primary extraction can trigger fallback behavior in a deterministic test.
- [ ] Page records retain page numbers and source document identity after fallback extraction.
- [ ] Extraction errors include context about document path and extraction stage.

## Dependencies

- task-7b45b114 — Local PDF ingest end-to-end
