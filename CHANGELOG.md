# Changelog

All notable changes to this project will be documented in this file.

The format is based on Keep a Changelog, and this project adheres to Semantic Versioning.

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
