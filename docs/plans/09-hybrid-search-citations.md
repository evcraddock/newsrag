# Hybrid search with citations

## Goal

Implement `newsrag search` so users can query indexed PDFs and receive ranked evidence passages with page-level citations.

## Requirements

- Add `newsrag search <query>`.
- Search SQLite FTS5 for keyword candidates.
- Search LanceDB for vector candidates using the configured query embedding provider.
- Merge keyword and vector candidates with simple score normalization and weighting.
- Include a no-op reranker hook in the retrieval pipeline.
- Return cited evidence passages with document title, meeting date, page number, and snippet text.
- Report clearly when no matching evidence is found.

## Acceptance criteria

- [ ] Search over mocked indexed chunks returns ranked cited passages.
- [ ] Keyword-only, vector-only, and overlapping candidate sets merge deterministically under test.
- [ ] Citations use the concise format `Title — date — p. N` where metadata is available.
- [ ] Empty results produce a clear no-evidence message.

## Dependencies

- task-7b45b114 — Local PDF ingest end-to-end
