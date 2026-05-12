# Corpus data directory and storage lifecycle

## Goal

Make a selected corpus data directory usable by the CLI and daemon by creating, validating, and reporting the local storage layout for SQLite, LanceDB, source PDFs, OCR artifacts, logs, and processing state.

## Requirements

- Initialize the configured NewsRAG data directory when needed.
- Create stable subdirectories for source PDFs, downloaded PDFs, OCR-normalized PDFs, LanceDB data, logs, and transient processing artifacts.
- Create or migrate the SQLite database enough to track high-level entities: documents, pages, chunks, jobs, watches, and metadata.
- Ensure storage initialization is idempotent.
- Add a `newsrag status` view that can report the active data directory and basic storage health.

## Acceptance criteria

- [ ] Running a storage-initializing command twice leaves the data directory valid and does not duplicate state.
- [ ] `newsrag status` reports the selected data directory and whether required local storage exists.
- [ ] Unit tests cover fresh initialization, existing initialization, invalid/unwritable data directory, and status output shaping.

## Dependencies

- task-11831224 — CLI, config, and doctor foundation
