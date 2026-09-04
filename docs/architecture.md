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
newsrag ingest https://example.gov/packet.pdf --meeting-date 2026-04-12
newsrag ingest-manifest sources.yaml
newsrag search "stormwater downtown" --body "Planning Commission" --since 2025-01-01
newsrag packet "affordable housing funding" --out packets/housing.md
newsrag status
newsrag jobs list
newsrag jobs retry <job-id>
```

## Storage and configuration

NewsRAG uses configurable local storage with a data directory defaulting to the user data directory (`$XDG_DATA_HOME/newsrag`, or `~/.local/share/newsrag` when `XDG_DATA_HOME` is unset). A user can override the active data directory with a CLI flag or configured default for a separate corpus. The data directory contains the corpus-local SQLite database, LanceDB vector index directory, immutable content-addressed source artifacts, OCR-normalized PDFs, processing artifacts, and local logs relevant to that corpus.

Configuration is user-global, for example `~/.config/newsrag/config.yaml`. The global config stores daemon settings, embedding provider/model defaults, watched folder registrations, and user-level defaults. CLI flags can override config values for a specific command.

The daemon is global and may manage many data directories over time. Search behavior for multiple corpora is deferred until there is more than one corpus in active use; MVP search targets the selected/current data directory.

## Core entities

NewsRAG is organized around a small set of durable entities rather than a server-side application model.

- **Corpus/data directory**: a local collection of documents, metadata, artifacts, and indexes stored under the default user data directory or configured equivalent.
- **Source**: the submitted URL or local path, stored separately from the bytes retrieved from it.
- **Source artifact**: the immutable raw bytes acquired from a source, identified by content hash and retaining media type, byte size, acquisition time, and stored path.
- **Document**: the searchable representation of one source artifact plus user-supplied civic metadata, such as title, meeting date, body or committee, document type, and jurisdiction.
- **Normalized PDF artifact**: the OCR-normalized/searchable PDF derived from a raw PDF source artifact and used for text extraction.
- **Source unit**: one ordered canonical text unit with a typed machine location, human label, normalized text, structure metadata, and extractor identity. PDF pages are represented as page source units; future formats may use blocks, lines, cells, or timestamps.
- **Page**: the PDF-specific compatibility record linked one-to-one to a page source unit. Existing page numbers and page citations remain the source of citation truth for PDFs.
- **Chunk**: searchable text derived from source units. Chunks retain both the existing PDF page span and a source-unit range for source-neutral retrieval.
- **Embedding**: a vector representation of a chunk or passage stored in LanceDB, linked back to its source record, source-unit range, and embedding provider/model/version.
- **Job**: durable processing work tracked through pending, running, done, and failed states, with structured completion outcomes, error details, and retry support.
- **Watch**: a configured folder watcher with default metadata used when new PDFs appear.
- **Packet**: a generated Markdown evidence file assembled from retrieved chunks and source metadata.

## Ingestion pipeline

Ingestion registers documents and jobs quickly; processing happens in the daemon. `newsrag ingest <source>` accepts one public HTTP(S) URL, local file, or local directory. A one-time directory scan recursively queues supported regular files, does not follow symlinks, and reports queued and skipped inputs by type. Watched folders remain a separate continuous-ingestion workflow. A manifest is the preferred way to provide civic metadata for multiple documents; every entry is validated before its jobs are created atomically, and relative local paths resolve from the manifest file's directory.

Example manifest:

```yaml
documents:
  - source: https://example.gov/council/packet-2026-04-12.pdf
    type: pdf
    title: City Council Packet
    meeting_date: 2026-04-12
    body: City Council
    document_type: agenda_packet
    jurisdiction: Example City
```

Processing uses exact raw-byte SHA-256 identity within one corpus. It does not compare normalized or extracted text and does not use fuzzy or semantic duplicate detection. A published exact duplicate completes with `duplicate_ignored`; it creates no new source, artifact, document, alias, or metadata and discards the repeated submission payload. The job result retains the existing artifact and document IDs. Artifacts retained after a failed attempt can be reused by a retry.

When a known source returns different bytes, NewsRAG preserves the new artifact with `change_detected_artifact_saved` but does not replace the source's current document. Repeating those changed bytes reports `change_already_detected`. New publications report `created`. A unique document-to-artifact constraint and transactional cleanup make concurrent duplicate processing converge without exposing partial document records or vectors. The complete policy is recorded in [Source identity and repeated ingestion](research/source-identity-and-repeated-ingestion.md).

Raw bytes are retained as immutable, content-addressed source artifacts. Acquisition runs in the daemon before format extraction. One acquisition accepts either an explicitly submitted local regular file or a public HTTP(S) URL, stages bytes while hashing, applies the duplicate decision, and durably promotes bytes that must be retained before invoking an adapter.

Remote acquisition resolves and pins a validated public address for each request, revalidates every redirect destination, follows at most five redirects, and applies a 30-second request timeout. URL credentials, localhost, non-public addresses, unsafe redirects, ambient proxy or cookie state, and unsupported content encodings are rejected. HTTP bodies are streamed with separate 250 MiB compressed and decompressed limits, reduced to 10 MiB when an HTML hint, extension, or response media type identifies static HTML. Only the submitted resource and required HTTP redirects are fetched; linked and embedded resources are not retrieved.

Local acquisition has a 250 MiB limit, reduced to 10 MiB for recognized HTML input, and accepts only regular files. An explicitly submitted symlink is allowed only when its resolved regular-file target remains stable. Directory scans skip symlinks. Device/inode, size, modification time, resolved target, and bytes read are checked around the read so missing, special, changed, or oversized inputs fail without preserving a partial artifact.

Acquisition provenance stores submitted and resolved references, redirects, retrieval or file-read timestamps, reported media type, exact byte size and SHA-256, and the durable artifact path. Diagnostics use stage-specific errors and omit URL credentials, query data, response bodies, and ambient secrets.

Each PDF is normalized through OCR, then text is extracted from the normalized PDF. This makes scanned and born-digital PDFs follow the same downstream path. The OCR stage uses `ocrmypdf` with Tesseract and its required supporting tools. Text extraction uses PyMuPDF as the primary extractor and pdfplumber as a fallback or table-oriented extraction path. Each extracted page is stored as an ordered page source unit before chunking, and source-unit ranges propagate through chunks, passages, embeddings, and search results while existing page citations remain stable.

## Source adapter contract

A format adapter receives an `AdapterInput` identifying one immutable raw artifact, its validated media type and content hash, an isolated work directory, and format-specific options. Its `extract` method must validate the artifact and return an `AdapterResult` containing ordered `CanonicalSourceUnit` values, extractor identity, validated media type, metadata candidates, and any derived artifact used for extraction. The adapter registry selects a supported adapter from an optional `--type` hint, reported media type, conservative content signature, and filename extension, in that order. A hint selects an adapter but never bypasses that adapter's validation. PDF and static HTML are currently registered.

Each canonical source unit has a contiguous one-based ordinal, typed machine location, human-readable location label, normalized text, structure metadata, and extractor name/version. Adapters do not chunk, embed, index, publish documents, or modify source identity. Those stages belong to the shared ingestion pipeline.

PDF is the first implementation of this contract. The PDF adapter validates the raw PDF, runs the existing OCR and extraction fallback behavior, and emits one page source unit per PDF page. The shared pipeline then performs page-compatible chunking, passage generation, embeddings, FTS/vector indexing, and publication. SQLite publication is transactional; failed vector publication rolls back the document bundle and removes staged vectors so incomplete documents cannot appear in search.

The static HTML adapter accepts `text/html` and `application/xhtml+xml` artifacts with conservative document signatures and a restricted encoding set. It parses with network access, entity resolution, recovery, and oversized parser trees disabled; it never executes JavaScript or retrieves linked resources. The adapter selects one unambiguous `article`, then one `main` or `role=main`, then the document body, and emits deterministic `html_block` units for retained headings, paragraphs, list items, quotations, preformatted text, captions, and table rows. Units carry block numbers, heading paths, element kinds, normalized text, and `static-html` extractor identity. Safe title, language, author, and publication-time metadata candidates fill missing user metadata. HTML files and public URLs use the same background acquisition, exact-byte identity, chunking, embedding, indexing, and atomic publication path as PDFs.

Static HTML extraction fails rather than truncating when input exceeds 10 MiB, the parsed tree exceeds 100,000 elements or 256 levels, or retained text exceeds 10 MiB. It also rejects unsupported or conflicting encodings, unsafe external declarations, malformed input, ambiguous content roots, and empty evidentiary output.

## Chunking and citations

PDF uses page-first chunking. Each page is stored as canonical extracted text. Short pages may produce one chunk; long pages are split into overlapping passages while preserving page start/end. HTML uses block-first chunking, keeping each source unit tied to its stable block ordinal and heading path. Shared chunks and passages retain authoritative source-unit start/end IDs for either format.

The data model allows further structure-aware chunking without changing the retrieval contract. Optional chunk metadata can include section title, heading path, agenda item, table marker, and bounding box.

Terminal citations use a concise format:

```text
City Council Packet — 2026-04-12 — p. 27
Council Update — Budget — block 12
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

Search filters should use user-supplied civic metadata, including body, document type, meeting date ranges, jurisdiction/source, and source URL where useful. Search spans every indexed source type by default; the optional `--source-type` filter on `newsrag search` and `newsrag documents list` narrows results to `pdf` or `html` while composing with the existing filters.

Document inventory derives source type from the published artifact and reports typed extents rather than treating every document as paginated: PDFs show page counts and HTML documents show canonical block counts. Search citations likewise resolve persisted typed source-unit locations, preserving PDF page citations and HTML heading/block citations.

## Embeddings

Embeddings use one OpenAI-compatible `/v1/embeddings` integration. The configured service can be local, such as llama.cpp, LM Studio, or Ollama's compatible API, or hosted, such as OpenAI. NewsRAG has no implicit embedding provider because the base URL and model must be selected explicitly.

Every embedding record retains provider, model, and version information. This allows safe index rebuilds when the embedding model changes. `newsrag doctor` checks the configured `/v1/models` endpoint, optional API-key environment variable, and model availability without exposing secret values.

## Daemon and jobs

The daemon is a long-running process exposed through `newsrag daemon run` and intended to be managed by an external process manager such as launchd in regular use or a development process manager during local development. NewsRAG should not rely on custom PID-file supervision as its core architecture.

The daemon uses filesystem notifications through `watchfiles` and an async worker model. Watched folder events should be debounced so partially copied PDFs are not processed too early. SQLite is the durable queue and job-state store. Failed jobs retain contextual error messages and timestamps, and CLI retry commands make failures visible and recoverable.

## Doctor and observability

`newsrag doctor` validates local prerequisites and configuration before long processing runs. It should check external binaries, data directory writability, embedding provider availability, daemon connectivity where applicable, and basic config validity. Error messages should be actionable.

`newsrag status` and `newsrag jobs list` should show queue health, failed jobs, and processing state. This matters because OCR and embedding jobs can be slow and failures should not be silent.

## Discovery and enrichment

Discovery evidence uses source-unit start/end IDs as its canonical location for every format. PDF evidence also records derived page IDs and page ranges so existing page-oriented output remains unchanged; HTML evidence records heading/block labels without inventing page values. Optional passage IDs provide narrower quote-validation context. Evidence is persisted only after the cited source-unit range belongs to the document and the quote is found in the canonical source text or cited passage.

Deterministic fact extraction, document briefs, structured enrichment, topics, timelines, and story leads operate over canonical source units. Document profiles store `source_type`, `extent_type`, and `extent_count`; PDF profiles use pages and HTML profiles use blocks. Terminal discovery output formats each typed location as a PDF page citation or HTML heading/block citation.

Schema version 5 replaces the previously unused page-only discovery schema. Upgrading from schema versions 1–4 resets only regenerable document profiles, briefs, discovery items/evidence, and their FTS tables rather than converting old derived records. Sources, artifacts, documents, source units, chunks, passages, embeddings, and search indexes are not reset.

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

The MVP packet is templated and evidence-based. It quotes retrieved passages without generating new claims and preserves each search result's typed citation, so one packet can contain PDF page evidence and HTML heading/block evidence without inventing HTML pages. Source-list entries retain the existing descriptive metadata and add authoritative provenance from the published source artifact: source type, submitted URL or path, a differing final URL, remote retrieval time, and exact-byte SHA-256. This ties packet evidence to the immutable ingested artifact.

## Implementation guidance

The CLI should use Typer and the project should use `uv` as its package/runtime tool. Development commands should run through `uv run ...`. Tests should be mostly unit tests with mocked OCR, PDF extraction, embedding providers, storage boundaries, and retrieval components so the suite is fast and stable. Integration tests with real PDFs can be added later after the pipeline shape is implemented.
