# Reprocessing preserved source artifacts

Reprocessing rebuilds derived evidence from saved bytes. It is different from `refresh`, which rereads a registered source and may publish a new source revision, and from `jobs retry`, which resumes a failed attempt with its saved target. This workflow implements the requirements consolidated into `task-aec94144`; the separate design task `task-aa5c6e7a` was cancelled as redundant.

## Commands

```bash
newsrag reprocess <document-id>
newsrag reprocess <document-id-1> <document-id-2>
newsrag reprocess <pdf-document-id> --pdf-extractor pdfplumber
newsrag jobs list
newsrag jobs retry <failed-job-id>
newsrag documents generations <document-id>
```

Each request accepts 1–20 explicit published document IDs. Historical source revisions may be selected explicitly. There are no directory, manifest, wildcard, automatic, scheduled, or unbounded corpus modes. Duplicate IDs within a request are deduplicated. The whole batch is validated before any new jobs are inserted, including unknown documents and incompatible PDF-only options. Each accepted document has its own independently completing job; a failure does not roll back other successful documents in the batch.

A repeated request returns an existing pending/running job for the same document and options. Different options for an already active target fail clearly rather than silently replacing the target. Enqueueing does not load an embedding model or read source contents. The worker needs the configured processing tools and embedding service.

## Identity and retained evidence

A document keeps its source ID, artifact ID/hash, document ID, revision membership, original metadata, and acquisition provenance. Reprocessing never fetches the original URL/path, changes the source's current revision, or creates another copy of a source revision. Missing or changed live sources do not prevent reprocessing when the saved artifact remains intact.

Each successful processing run has a separate processing-generation ID. Source units, pages, chunks, passages, and embedding records are retained rather than overwritten; source-unit ordinal uniqueness is scoped to document and generation. OCR output paths are also isolated by generation. Existing source-unit IDs and their text/locations remain authoritative for citations already issued. Existing packet files are not rewritten.

One processing-generation pointer per document selects the active derived evidence. New search and evidence operations use the active generation of each eligible source revision. `--include-history` continues to mean source-revision history, not all processing generations in search. Generation inventory exposes retained configurations and publication identities; this version has no manual generation rollback or pruning command. On failure, the previous generation remains active automatically.

A query captures its eligible passages and processing generation once. A concurrent publication cannot substitute a new generation's text, source-unit range, or normalized output for already selected evidence. New packets identify the processing generation in addition to source/revision/document/artifact identity. Resolving an old HTML ordinal range remains restricted to its original generation even when a new extraction emits the same ordinals.

## Triggers and no-op behavior

Explicit reprocessing compares a canonical configuration fingerprint with the active generation. The fingerprint includes adapter and chunker implementations/settings, requested extractor options, embedding provider/model/version, a non-reversible endpoint identifier, processing/index format versions, relevant package versions, and local OCR tool versions. Credentials and full embedding endpoints are not persisted in generation configuration.

Changing extraction options or tool versions, chunking parameters/implementation, embedding model identity, or processing/index format versions causes a complete staged rebuild. The first version deliberately rebuilds the entire derived bundle rather than implementing separate partial-rebuild modes. Internal processing and index version constants must be bumped when output semantics change. Restart workers after upgrading processing tools; local OCR tool versions are recorded for the lifetime of a worker process.

If the fingerprint is unchanged, the worker verifies the saved artifact and completes with `unchanged` without extraction, embedding requests, index replacement, or pointer mutation. Fingerprints cannot detect a model server silently replacing weights behind the same model/version and endpoint; use explicit versioned model identifiers. The first explicit reprocessing of a migrated legacy generation rebuilds because its original processing configuration is not reliably known.

Embedding vectors are partitioned when dimensions differ, and vector queries match provider/model/version and dimensions. Incompatible model spaces are never compared. Keyword retrieval remains available across eligible documents; vectors contribute only from compatible model spaces. Search does not silently re-embed an explicitly versioned processing generation just because the query model changed. Reprocess the desired documents explicitly to move their vectors to a different model.

## Durable jobs, retries, and failures

Jobs use `pending`, `running`, `done`, and `failed`. The payload records document/artifact identity, the base generation, requested options, the target configuration/fingerprint once prepared, and processing progress. Outcomes are `unchanged` or `reprocessed`, with previous/new processing generation IDs and the unchanged source/revision/artifact/document identities.

Before processing, the worker copies saved bytes into a private temporary snapshot while checking the exact SHA-256 and recorded size. The adapter reads that verified snapshot, not a possibly changing live source or cache file. Missing, corrupt, nonregular, or unsupported saved inputs fail with a stage-specific diagnostic; no replacement bytes are fetched.

Retries always use the same preserved artifact and base generation. Once a target configuration has been recorded, retries require that configuration: changes to worker models, tools, chunking, or extractor settings produce a configuration conflict. Restore the saved configuration or submit a new reprocessing request. A new request captures the currently active generation; a retry must not adopt it silently. Interrupted unpublished work is recomputed from verified bytes, not resumed from an untrusted partially written index.

Publication uses SQLite as the authoritative visibility boundary. A transaction inserts the new generation and complete derived bundle, checks the expected active generation, writes FTS and vector indexes, changes the active pointer, and records the job's completion receipt. Readers see only committed generations. On failure, SQLite changes roll back and vector compensation deletes only IDs created by that attempt, never all vectors belonging to the existing document. Uncommitted vector rows remain ineligible even if cleanup fails; cleanup failures are reported.

Refresh and reprocessing share the process-held corpus worker lock. Recovery occurs only after acquiring that lock, and cancellation retains ownership until outstanding processing finishes. A crashed worker releases the lock; a later worker marks unfinished jobs failed/retryable. Committed receipts are authoritative: replay and late failure acknowledgements cannot republish a generation or erase success. Ordinary ingestion retains its existing concurrency.

## Migration and upgrade

Stop all old NewsRAG daemons before upgrading a corpus to schema 7, then restart workers on the same version. Do not mix old and new worker binaries on a corpus.

Migration creates one legacy processing generation per existing published document, preserves original document/source-unit/derived row IDs, text, FTS entries, embedding records, normalized paths, revision history, and staged artifacts, and assigns generation membership. It changes source-unit ordinal uniqueness without renumbering old citations. Unknown legacy processing fingerprints are marked `legacy:unknown`, not fabricated. Migration is idempotent and validates generation ownership.

No automatic enrichment, source reassignment, metadata editing, retention/deletion, manual rollback, or automatic reprocessing is added. Use disposable corpora for migration and failure testing before upgrading an installed corpus.
