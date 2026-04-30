# NewsRAG Architecture

NewsRAG is a local-first CLI evidence retrieval tool for city hall PDFs. The first product shape is a single-user, scriptable research tool that ingests civic PDF documents, normalizes them through OCR, indexes page-grounded passages with hybrid keyword/vector search, and returns cited evidence in terminal output or Markdown source packets.

## Product goals

The MVP should let a user collect city hall PDFs from local folders, direct PDF URLs, or a hand-written YAML manifest; process them in the background; search them with natural-language or keyword queries; and export reusable Markdown source packets with page-level citations. The system should prioritize evidence retrieval over answer generation: results should show passages, document metadata, page numbers, and source references so a human can inspect and reuse the evidence.

## Primary workflow

A user configures NewsRAG once, starts a local daemon through an external process manager, and works through CLI commands. The CLI can register documents from a folder, a direct PDF URL, or a YAML manifest. Registered documents become durable jobs. The daemon watches configured folders, reacts to filesystem events, processes queued jobs asynchronously, and updates the local search indexes. The user runs `newsrag search` for quick cited evidence and `newsrag packet` to write a Markdown source packet.

Example commands:

```bash
newsrag doctor
newsrag daemon run
newsrag watch add ./pdfs --body "City Council" --document-type agenda_packet
newsrag ingest ./pdfs --body "City Council" --document-type agenda_packet
newsrag ingest-url https://example.gov/packet.pdf --meeting-date 2026-04-12
newsrag ingest-manifest sources.yaml
newsrag search "stormwater downtown" --body "Planning Commission" --since 2025-01-01
newsrag packet "affordable housing funding" --out packets/housing.md
newsrag status
newsrag jobs list
newsrag jobs retry <job-id>
```

## Storage and configuration

NewsRAG uses configurable local storage with a per-corpus data directory defaulting to `./.newsrag/`. A user can override the active data directory with a CLI flag or configured default. The data directory contains the corpus-local SQLite database, LanceDB vector index directory, downloaded source PDFs, OCR-normalized PDFs, processing artifacts, and local logs relevant to that corpus.

Configuration is user-global, for example `~/.config/newsrag/config.yaml`. The global config stores daemon settings, embedding provider/model defaults, watched folder registrations, and user-level defaults. CLI flags can override config values for a specific command.

The daemon is global and may manage many data directories over time. Search behavior for multiple corpora is deferred until there is more than one corpus in active use; MVP search targets the selected/current data directory.

## Core entities

NewsRAG is organized around a small set of durable entities rather than a server-side application model.

- **Corpus/data directory**: a local collection of documents, metadata, artifacts, and indexes stored under a `.newsrag/` directory or configured equivalent.
- **Document**: a source PDF and its user-supplied civic metadata, such as title, source URL, meeting date, body or committee, document type, and jurisdiction.
- **Normalized PDF artifact**: the OCR-normalized/searchable PDF produced from the source document and used for text extraction.
- **Page**: canonical extracted page text with page number and extraction quality information. Pages are the source of citation truth.
- **Chunk**: a searchable passage derived from page text. MVP chunking is page-first, with long pages split into overlapping chunks. Chunks retain page start/end and allow future structure fields such as section title, heading path, agenda item, and bounding box.
- **Embedding**: a vector representation of a chunk stored in LanceDB, linked back to the chunk identifier and tagged with embedding provider, model, and version.
- **Job**: durable processing work tracked through pending, running, done, and failed states, with error details and retry support.
- **Watch**: a configured folder watcher with default metadata used when new PDFs appear.
- **Packet**: a generated Markdown evidence file assembled from retrieved chunks and source metadata.

## Ingestion pipeline

Ingestion registers documents and jobs quickly; processing happens in the daemon. Sources supported by the MVP are local PDFs/folders, direct PDF URLs, and YAML manifests. URL support is direct PDF download only. A manifest is the preferred way to provide civic metadata for multiple documents.

Example manifest:

```yaml
documents:
  - url: https://example.gov/council/packet-2026-04-12.pdf
    title: City Council Packet
    meeting_date: 2026-04-12
    body: City Council
    document_type: agenda_packet
    jurisdiction: Example City
```

Processing is idempotent. The system hashes source content to avoid duplicate indexing and to detect changed documents. Each document is normalized through OCR first, then text is extracted from the normalized PDF. This makes scanned and born-digital PDFs follow the same downstream path.

The OCR stage uses `ocrmypdf` with Tesseract and its required supporting tools. Text extraction uses PyMuPDF as the primary extractor and pdfplumber as a fallback or table-oriented extraction path. Page text is stored before chunking so citations remain stable and inspectable.

## Chunking and citations

The MVP uses page-first chunking. Each page is stored as canonical extracted text. Short pages may produce one chunk; long pages are split into overlapping passages while preserving page start/end. This keeps citations simple and reliable for city hall PDFs, where page numbers are often the most important reference.

The data model should allow structure-aware chunking later without changing the retrieval contract. Optional chunk metadata can include section title, heading path, agenda item, table marker, and bounding box.

Terminal citations use a concise format:

```text
City Council Packet — 2026-04-12 — p. 27
```

Markdown packet/source-list citations can include richer context:

```text
City Council Packet (City Council, 2026-04-12), p. 27 — source.pdf
```

## Search and retrieval

Search is evidence-first. The default `newsrag search` command returns ranked passages with citations and metadata. It does not generate an answer by default. When no evidence is found, the system should report that clearly.

The MVP retrieval pipeline uses hybrid search:

1. SQLite stores canonical metadata, pages, chunks, job state, watches, and FTS5 keyword indexes.
2. LanceDB stores chunk embeddings for vector search.
3. Search collects keyword candidates from SQLite FTS5 and semantic candidates from LanceDB.
4. Candidate scores are normalized and merged into a hybrid ranking.
5. A reranker interface exists in the pipeline, but the MVP implementation is a no-op.
6. Final results are returned as cited passages.

Search filters should use user-supplied civic metadata, including body, document type, meeting date ranges, jurisdiction/source, and source URL where useful.

## Embeddings

Embeddings are pluggable. The MVP is local-first and defaults to Ollama running `nomic-embed-text`. The architecture also supports `bge-base-en` as a local model option and OpenAI `text-embedding-3-small` as an optional hosted provider.

Every embedding record should retain provider, model, and version information. This allows safe index rebuilds when the embedding model changes. `newsrag doctor` checks embedding provider availability, including whether Ollama is reachable and whether the configured model is installed or pullable.

## Daemon and jobs

The daemon is a long-running process exposed through `newsrag daemon run` and intended to be managed by an external process manager such as launchd in regular use or a development process manager during local development. NewsRAG should not rely on custom PID-file supervision as its core architecture.

The daemon uses filesystem notifications through `watchfiles` and an async worker model. Watched folder events should be debounced so partially copied PDFs are not processed too early. SQLite is the durable queue and job-state store. Failed jobs retain contextual error messages and timestamps, and CLI retry commands make failures visible and recoverable.

## Doctor and observability

`newsrag doctor` validates local prerequisites and configuration before long processing runs. It should check external binaries, data directory writability, embedding provider availability, daemon connectivity where applicable, and basic config validity. Error messages should be actionable.

`newsrag status` and `newsrag jobs list` should show queue health, failed jobs, and processing state. This matters because OCR and embedding jobs can be slow and failures should not be silent.

## Packet generation

`newsrag packet` uses the same retrieval pipeline as search and writes an extractive Markdown source packet. The initial packet template is fixed and research-oriented, with configurable templates left for later.

Default packet structure:

```markdown
# Source Packet: <query>

## Key Evidence

## Timeline

## Open Questions

## Source List
```

The MVP packet is templated and evidence-based. It should quote or summarize retrieved passages only enough to organize the evidence, and it should preserve citations to document titles, dates, bodies, pages, and source files.

## Implementation guidance

The CLI should use Typer and the project should use `uv` as its package/runtime tool. Development commands should run through `uv run ...`. Tests should be mostly unit tests with mocked OCR, PDF extraction, embedding providers, storage boundaries, and retrieval components so the suite is fast and stable. Integration tests with real PDFs can be added later after the pipeline shape is implemented.
