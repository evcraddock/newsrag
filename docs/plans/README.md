# NewsRAG Implementation Plans

These files are the reviewed implementation backlog derived from `docs/architecture.md` and converted into backend task records.

Each plan uses the task structure expected for backend tasks: `## Goal`, `## Requirements`, `## Acceptance criteria`, and `## Dependencies`.

Dependencies reference the real backend task IDs created for the `newsrag` project.

## Proposed vertical-slice order

1. task-11831224 — [CLI, config, and doctor foundation](01-cli-config-doctor.md)
2. task-9c242503 — [Corpus data directory and storage lifecycle](02-corpus-storage-lifecycle.md)
3. task-6142564f — [Daemon run loop and durable job queue](03-daemon-job-queue.md)
4. task-40c08a4f — [Watched folder ingestion](04-watch-folder-ingestion.md)
5. task-7b45b114 — [Local PDF ingest end-to-end](05-local-pdf-ingest-e2e.md)
6. task-44db91c8 — [Direct PDF URL ingest end-to-end](06-url-ingest-e2e.md)
7. task-9b0e618d — [YAML manifest ingest end-to-end](07-yaml-manifest-ingest.md)
8. task-241cdb95 — [Embedding provider integration](08-embedding-providers.md)
9. task-e5c2f467 — [Hybrid search with citations](09-hybrid-search-citations.md)
10. task-8b24551a — [Search metadata filters](10-search-metadata-filters.md)
11. task-f0a187f0 — [Markdown source packet generation](11-markdown-source-packets.md)
12. task-393a0a9c — [Job failure visibility and retry UX](12-job-failure-retry.md)
13. task-13e07a5a — [pdfplumber fallback extraction path](13-pdfplumber-fallback.md)
14. task-c98325bb — [Watcher debouncing and daemon health checks](14-watcher-debounce-health.md)
15. task-4bb15b57 — [Architecture validation and backlog conversion](15-backlog-conversion.md)
