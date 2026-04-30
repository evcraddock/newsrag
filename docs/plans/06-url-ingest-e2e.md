# Direct PDF URL ingest end-to-end

## Goal

Allow users to ingest a direct PDF URL into the selected corpus with supplied civic metadata.

## Requirements

- Add `newsrag ingest-url <url>` with metadata options such as title, meeting date, body, document type, and jurisdiction.
- Download direct PDF URLs into the data directory while preserving source URL and retrieval timestamp.
- Hash downloaded content and avoid duplicate indexing of unchanged files.
- Reuse the same background processing path as local PDF ingestion.
- Fail clearly when the URL does not return a PDF-like response or cannot be downloaded.

## Acceptance criteria

- [ ] `newsrag ingest-url` enqueues a processing job for a direct PDF URL.
- [ ] Download behavior is unit-tested with mocked HTTP responses.
- [ ] Source URL and retrieval timestamp are stored with the document.
- [ ] Download failures create failed jobs or clear CLI errors with actionable messages.

## Dependencies

- task-7b45b114 — Local PDF ingest end-to-end
