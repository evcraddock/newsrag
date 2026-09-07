# Derived artifact storage and upgrades

Generated processing files use `artifacts/derived/<processing-generation-id>/`. PDF normalization writes `<source-content-hash>.pdf` there during ingestion, refresh, and reprocessing. The shared directory is format-neutral; this layout change adds no adapters and changes neither OCR nor extraction configuration. Retained original bytes remain immutable in `artifacts/sources/`; acquisition staging stays in `artifacts/staging/`.

New corpora never create or require `ocr-pdfs/`. Old processing generations, source revisions, document IDs, page/source-unit IDs, citations, and search indexes remain intact. Reprocessing continues to retain previous outputs and uses a different generation directory.

## Upgrade an existing corpus

1. Stop every daemon/worker using the corpus, including old-version workers. Do not mix versions during an upgrade.
2. Back up the **entire** corpus, including SQLite, LanceDB, and artifacts. Allow enough free space to copy all referenced legacy normalized files; a file shared by several generations gets a separate copy for each generation.
3. Inspect without changing files:

   ```bash
   newsrag --data-dir /absolute/path/to/corpus status
   ```

4. Initialize with the new version, then inspect again:

   ```bash
   newsrag --data-dir /absolute/path/to/corpus status --initialize
   newsrag --data-dir /absolute/path/to/corpus status
   ```

5. Restart workers using the new version only after initialization succeeds and the `derived_transition` check is `ok`.

Initialization performs the transition automatically, including when invoked by other CLI commands or daemon startup. The storage schema remains version 8; this is a filesystem/reference transition after existing schema migrations, not a rebuild of evidence. A failed transition rolls back its normalized-path updates together; schema migrations have their own existing transaction boundaries.

## What gets reconciled

The transition recognizes normalized paths containing the legacy `ocr-pdfs/` directory, including corpus-relative references, older flat layouts, generation subdirectories, and absolute paths. It copies referenced outputs into generation-specific derived storage, verifies SHA-256 equality, durably publishes complete files without overwriting existing destinations, then transactionally updates `processing_generations.normalized_path` and matching `documents.normalized_path` references. A document's legacy normalized path can describe its initial generation rather than its active generation; that relationship is preserved rather than blindly repointed.

For a legacy absolute reference, the transition also looks for the same suffix under the current corpus's `ocr-pdfs/` directory. This supports a moved corpus when the recorded old path is unavailable. If both copies exist with different bytes, the transition stops rather than guessing. This is not a general corpus-relocation facility: it does not rewrite original-source paths, custom artifact paths, or already exported packets. Moving a corpus can independently invalidate absolute links in previously exported files.

**Legacy files are never renamed or deleted.** Existing saved packets with legacy artifact links continue to work when those original locations are unchanged. Unreferenced files, interrupted processing outputs, and custom paths outside the recognized legacy directory remain untouched. A leftover `ocr-pdfs/` directory is supported, not required, and does not itself make storage unhealthy. No cleanup command or automatic pruning is added; keep legacy copies needed by exported packets or other external references.

## Interrupted upgrades and recovery

- **Interruption or disk full:** free space or fix the reported I/O problem, then rerun `status --initialize`. Copies use private `.transition-*` temporary files and atomic publication. Only complete, verified, synced destinations are referenced by a committed transaction. Retries reuse matching completed copies; abandoned temporary files are never adopted as artifacts. Old files remain available even if the reference transaction rolls back.
- **Missing legacy artifact:** restore the original normalized file from backup at the recorded absolute path or the corresponding current-corpus `ocr-pdfs/` path. Retry initialization. An unexplained destination copy alone is not accepted as proof of the missing source's bytes.
- **Conflicting copies/destination:** preserve both copies, compare them with your backup, and restore the correct corpus files before retrying. NewsRAG does not overwrite a conflicting destination or choose between different legacy bytes. Do not substitute a newly OCR-processed file for a historical artifact.
- **Unsafe paths or inconsistent document/generation references:** initialization refuses to guess. Restore a consistent corpus backup or investigate the reported records before retrying; do not manually rename directories and leave references unreconciled.

Plain `status` is read-only. `derived_artifacts` checks the new directory; `derived_transition` reports pending references as a warning and missing/conflicting/unsafe legacy references as errors. Once references are reconciled, retained legacy copies are accepted. These checks are not a full corpus-wide checksum audit.
