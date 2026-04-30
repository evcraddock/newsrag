# Local PDF ingest end-to-end

## Goal

Implement the first user-visible ingestion path: add local PDF files or folders, process them through OCR, extraction, chunking, embedding, and indexing, then make them available for later search.

## Requirements

- Add `newsrag ingest <path>` for a PDF file or directory of PDFs.
- Record document metadata from CLI flags and source file properties.
- Hash source content for idempotency and duplicate detection.
- Run OCR normalization with `ocrmypdf` before text extraction.
- Extract page text with PyMuPDF.
- Store page text as canonical citation source.
- Create page-first chunks with overlap for long pages.
- Store chunk metadata in SQLite and vector embeddings in LanceDB.
- Use interfaces/mocks around OCR, extraction, and embedding so tests remain fast.

## Acceptance criteria

- [ ] Running `newsrag ingest ./pdfs` enqueues local PDF processing jobs.
- [ ] A mocked end-to-end local PDF job creates a document, pages, chunks, and vector records under test.
- [ ] Re-ingesting an unchanged PDF does not duplicate document/chunk records.
- [ ] Ingestion failures are recorded as failed jobs with contextual errors.

## Dependencies

- task-6142564f — Daemon run loop and durable job queue
- task-241cdb95 — Embedding provider integration
