# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic Versioning.

## [0.2.2] - 2026-05-12

### Fixed
- Fixed the documented `curl ... | bash` installer path by supporting scripts run from stdin.
- Changed the default data directory from project-local `./.newsrag` to user storage (`$XDG_DATA_HOME/newsrag` or `~/.local/share/newsrag`).
- Reduced `newsrag --version` startup latency by lazy-loading heavy command dependencies only when their commands run.

## [0.2.1] - 2026-05-09

### Fixed
- Added a tag-triggered GitHub Release workflow that validates release tags, runs package checks, builds distribution artifacts, and creates or updates GitHub Releases automatically.
- Updated the release skill and script messaging to make GitHub Actions the owner of release creation after a tag is pushed.
- Fixed the release script so changelog-only release commits can proceed when `project.version` already matches the requested version.

## [0.2.0] - 2026-05-09

Initial release of NewsRAG, a local-first CLI evidence retrieval tool for city hall PDFs.

### Added
- CLI foundation with config loading, doctor checks, and storage status.
- Local corpus storage using SQLite, FTS5, and LanceDB.
- Durable daemon job queue with job visibility, retry support, and failure reporting.
- Watched-folder ingestion with debounce/stabilization and watcher health checks.
- Local PDF ingestion with OCR normalization, page extraction, chunking, embeddings, and indexing.
- Direct PDF URL ingestion and YAML manifest ingestion.
- Ollama embedding provider integration.
- Hybrid keyword/vector search with page citations and metadata filters.
- Markdown source packet generation from cited evidence.
- pdfplumber fallback extraction path for low-quality PyMuPDF output.
- GitHub Actions CI for branch and PR validation.
- Install script, `newsrag --version`, and release workflow.

### Fixed
- Improved doctor and embedding provider error reporting.
- Reduced irrelevant vector-only search matches.
- Improved search snippet quality and semantic tail filtering.
- Added contextual ingestion and extraction failure messages.
