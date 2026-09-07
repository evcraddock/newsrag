# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic Versioning.

## [Unreleased]

## [0.5.0] - 2026-09-06

### Added
- Added static HTML, plain-text, Markdown, DOCX, CSV, and XLSX source adapters alongside PDF, with source-native citations and shared retrieval, discovery, and packet generation.
- Added exact tabular cell evidence, separately attributed headers/context, worksheet coordinates, merged-cell anchors, and stored formula-cache qualifications. Hidden spreadsheet content is excluded from new evidence; formulas are never executed.
- Added source refresh with retained revision history and current/historical evidence selection.
- Added saved-artifact reprocessing with versioned processing generations, recipe-aware retries, and preserved historical evidence.
- Added inclusive UTC ingestion-date filters to `newsrag documents list` with `--ingested-since` and `--ingested-until`.
- Added daemon progress logging and disposable CLI/daemon verification workflows.

### Changed
- Unified local-path and public HTTP(S) ingestion, including mixed-source directories and manifests, with validated source selection and bounded acquisition.
- Added exact-byte duplicate identity and source/artifact provenance throughout inventory, search, discovery, and source packets.
- Updated repository development commands to use a repo-local corpus.
- Advanced corpus storage to schema 8 for source identity, revision history, processing generations, and shared tabular evidence.

### Fixed
- Recover existing text from the original PDF when OCRmyPDF exits with status 4; `auto` extraction also tries pdfplumber after primary-extractor errors. Failed recovery reports both stages with actionable guidance and never publishes partial OCR output.
- Treat search terms as literal text rather than FTS syntax to prevent query crashes.
- Preserve plain-text extraction limits during refresh and scope briefs and failed vector cleanup to their processing generation.

### Upgrade notes
- Stop old daemons before upgrading an existing corpus and back up the entire data directory, including SQLite, LanceDB, and saved artifacts. Do not mix old and new workers.
- Storage initialization upgrades existing corpora automatically; many CLI commands and daemon startup initialize storage. There is no automatic downgrade, and SQLite/LanceDB changes are not one cross-store transaction.
- Upgrades from schemas 1–4 reset regenerable discovery records. Upgrades from schemas 1–2 or unversioned corpora consolidate exact-byte duplicate documents. Migration does not automatically rerun OCR or generate new embeddings.
- New or intentionally reset corpora initialize directly at schema 8. See [reprocessing and upgrade guidance](docs/reprocessing.md) and [PDF recovery](docs/pdf.md).

## [0.4.0] - 2026-09-02

### Added
- Added supported Apple Silicon installation through `brew install evcraddock/tap/newsrag`.
- Added configuration guidance for llama.cpp, LM Studio, Ollama's OpenAI-compatible API, and hosted OpenAI embeddings.

### Changed
- Replaced the native Ollama embedding integration with one OpenAI-compatible `/v1/embeddings` provider.
- Require explicit embedding provider, base URL, and model configuration before ingestion or vector search.

## [0.3.0] - 2026-05-14

### Added
- Added bounded document inventory browsing with `newsrag documents list/show`.
- Added durable discovery storage for document profiles, briefs, discovery items, and evidence references.
- Added deterministic civic fact extraction with `newsrag discover document`.
- Added evidence-backed document briefs with `newsrag documents brief`.
- Added optional structured LLM discovery enrichment with validated JSON and quote-backed evidence.
- Added corpus discovery browsing commands for topics, entities, timelines, and story leads.

### Documentation
- Added research notes for discovery-oriented ingestion enrichment.

## [0.2.3] - 2026-05-12

### Fixed
- Moved the curl installer checkout cache to `${XDG_CACHE_HOME:-~/.cache}/newsrag/source` so it no longer collides with the default runtime data directory.
- Made the installer refuse to overwrite an existing non-git checkout target.

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
