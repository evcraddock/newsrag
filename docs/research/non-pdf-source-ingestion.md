# Source-neutral ingestion design

Status: Approved on 2026-09-02.

## Goal

NewsRAG will ingest concrete source material supplied as a direct public URL, local file, local directory, or explicit manifest. It will use one source-neutral pipeline for PDFs and supported non-PDF formats.

Static HTML is the first new non-PDF format. Plain text, Markdown, DOCX, CSV, XLSX, timed transcripts, and audio/video are explicitly scheduled rather than left as undefined future work.

This task designs the system and records follow-up work. It does not implement ingestion.

## Responsibility boundary

NewsRAG receives material that has already been identified.

Accepted examples:

```bash
newsrag ingest https://city.gov/meetings/september-minutes.html
newsrag ingest ~/Downloads/september-minutes.docx
newsrag ingest ~/Downloads/meeting-records
newsrag ingest-manifest sources.yaml
```

NewsRAG reads the supplied material and adds supported content to its searchable corpus. It does not accept research requests such as “find the September minutes,” search municipal websites, browse source catalogs, query SlugKit, crawl links, or choose which external material should be ingested.

A separate discovery tool may supply a concrete URL, but NewsRAG has no dependency on that tool.

## One ingestion interface

`newsrag ingest <source>` is the primary command for one public HTTP(S) URL, local file, or local directory. A separate URL-specific command is not retained.

`newsrag ingest <directory>` performs a one-time recursive scan. It queues supported regular files, does not follow symlinks, and reports queued and skipped files by type. Continuous folder watching remains a separate workflow.

`newsrag ingest-manifest <file>` remains because it is an explicit batch workflow. Each entry uses one `source` field containing a URL or local path:

```yaml
documents:
  - source: https://city.gov/council/update.html
    title: Council Update
    body: City Council

  - source: ./reports/budget.docx
    title: Proposed Budget
    type: docx
```

The entire manifest is validated before any job is created.

## Source-type detection

A URL or path identifies how bytes are acquired; it does not identify the content format. After safe acquisition, NewsRAG selects a registered format adapter using:

1. An optional explicit `--type` or manifest `type` hint.
2. Validated media type.
3. Conservative content-signature detection.
4. Filename extension as supporting evidence.

A type hint selects an adapter but never bypasses its validation. Contradictory or unsupported input fails clearly. NewsRAG never treats arbitrary binary data as plain text merely because some bytes are printable.

## Source-neutral pipeline

Every supported source follows the same stages:

1. Register a durable background job.
2. Acquire exactly the supplied URL or local file.
3. Preserve the exact bytes as an immutable source artifact.
4. Validate the format and select a registered adapter.
5. Extract ordered canonical source units while retaining native structure.
6. Build chunks and retrieval passages carrying source-unit ranges.
7. Create embeddings and FTS/vector index records.
8. Publish one complete searchable document or fail without publishing it.

A format adapter owns format validation, extraction, source-specific metadata candidates, canonical source units, and location formatting. Shared acquisition, jobs, document state, chunking, embeddings, ranking, discovery, and packets remain outside individual adapters.

## Native structure and source units

Each adapter emits ordered text units without converting every format into HTML or flattening the source into an anonymous block of text.

| Source type | Preserved structure | Citation location |
| --- | --- | --- |
| PDF | Pages | Page number or range |
| Static HTML | Headings, paragraphs, lists, quotes, preformatted text, captions, table rows | Heading and block number/range |
| Plain text | Lines | Line range |
| Markdown | Headings, paragraphs, lists, quotes, tables, code blocks | Heading and line range |
| DOCX | Headings, paragraphs, lists, tables, footnotes | Heading and paragraph/table range |
| CSV | Tables, headers, rows, columns, cells | Table, row, column, or cell range |
| XLSX | Workbooks, sheets, tables, rows, columns, cells | Workbook, sheet, table, row, column, or cell range |
| SRT/WebVTT | Cues, timestamps, speakers, text | Timestamp range |
| Audio/video | Immutable media plus a derived transcript | Timestamp range |

An HTML block number is NewsRAG’s internal ordinal for an extracted heading, paragraph, list item, table row, or other retained content unit in the preserved snapshot. It is not a number displayed by the live webpage.

Human-facing citations remain readable, while a typed machine location preserves exact identity. Examples:

```text
Council Packet — 2026-04-12 — p. 27
Council Update — Budget — blocks 12–14
meeting-notes.md — Public Comment — lines 84–96
budget.xlsx — FY2027 — cells B14:F18
Council Meeting — 01:12:08–01:13:41
```

NewsRAG will not add a dedicated citation-inspection command initially. Search results show the matching passage, packets retain provenance, and the original artifact remains preserved for deeper inspection.

## PDF migration

PDF becomes a source adapter rather than remaining a permanent separate pipeline.

The PDF adapter keeps existing OCR normalization and extraction behavior, emits each page as a typed source unit, and then uses the same chunking, embedding, indexing, search, discovery, and packet stages as every other format. Existing PDF records are migrated into the shared model, and existing page citations remain unchanged.

## Initial non-PDF format: static HTML

Static HTML is the first new adapter because municipal notices, meeting summaries, ordinances, staff updates, and press releases are commonly published directly as webpages. One extractor can process a direct HTML response or saved local HTML file while establishing the non-page citation model needed by later formats.

Static HTML means the evidentiary content is present in the supplied response or file. The initial adapter does not execute JavaScript, operate a browser, authenticate, submit forms, bypass paywalls, process a webpage containing a media player, or fetch linked resources.

### HTML extraction

For a preserved HTML artifact, the adapter:

1. Uses one unambiguous `article` element when present.
2. Otherwise uses one unambiguous `main` or `role=main` element.
3. Otherwise uses the cleaned document body.
4. Removes scripts, styles, forms, navigation, headers, footers, sidebars, frames, and other active or non-evidentiary elements.
5. Retains headings, paragraphs, lists, quotations, preformatted text, captions, and table content in source order.
6. Preserves visible link text but does not visit links.
7. Uses deterministic rules rather than site-specific or AI-based cleanup.

Each retained unit stores its content kind, document-wide block ordinal, heading path, normalized text, typed location, and extractor name/version.

### HTML limits

Initial HTML defaults are:

- 10 MiB maximum downloaded or local HTML input;
- five redirects maximum;
- 30-second request timeout;
- bounded extracted text, element count, and parser nesting; and
- clear failure rather than silent truncation.

Other adapters define format-appropriate limits in their own tasks.

## Background jobs and document state

All source types use the existing durable background job system.

- `pending`: the job is waiting; no searchable document exists.
- `running`: acquisition, extraction, or indexing is in progress; no searchable document exists.
- `done`: artifact, metadata, source units, chunks, passages, embeddings, and indexes are complete; the document is searchable.
- `failed`: the stage-specific error is visible; no searchable document is published.

A preserved artifact may remain attached to a failed job for diagnosis, but it is not an ingested document. Failed or partial extraction, transcription, embedding, FTS, or vector output never replaces searchable data.

## Metadata and provenance

Every source retains:

- original URL or file path;
- resolved final URL or path;
- retrieval or file-ingestion time;
- exact stored source artifact;
- source type and reported media type;
- byte size and SHA-256 hash;
- extractor name and version;
- user-provided title, meeting date, body, document type, and jurisdiction; and
- source-specific details such as HTML language, workbook sheet names, or media duration.

User-provided metadata takes precedence. Adapters may provide safe metadata candidates already present in the source, such as HTML title, declared language, author, publication time, DOCX metadata, worksheet names, or transcript duration. They do not infer civic metadata such as meeting date or jurisdiction.

Credentials, authorization headers, cookies, proxy secrets, and source bodies never appear in diagnostics or logs.

A citation identifies the immutable ingested artifact, not the current contents of a mutable URL. Packet source lists include source type, original URL/path, final URL when different, retrieval time for remote sources, and artifact hash.

## Search, inventory, discovery, and packets

All source types share one corpus and one search index. Mixed-source search is the default. `--source-type` is an optional filter, never a required argument.

Document inventory shows source type and an appropriate extent instead of assuming pages:

```text
pages=27
blocks=84
lines=320
cells=B2:F120
duration=01:42:18
```

Non-page documents never receive an invented page count.

Successfully ingested sources are eligible for the same briefs, deterministic fact extraction, topics, timelines, and story leads as PDFs. Discovery evidence uses the same typed locations and stored quotes as search.

One source packet may contain evidence from different formats. Each item uses its own typed citation, while the packet retains the existing evidence, timeline, open-question, and source-list structure.

## Safety rules

### Remote sources

- Accept only public `http` and `https` URLs.
- Reject URL credentials, localhost, private networks, link-local destinations, multicast, unspecified addresses, and unsupported schemes.
- Do not provide a private-network override.
- Revalidate every redirect destination.
- Enforce format-specific redirects, timeouts, and download limits.
- Do not inherit browser cookies or ambient credentials.
- Fetch only the supplied resource, never links, frames, refresh targets, or subresources.

### Local files and parsers

- Read only explicitly supplied regular files or regular files found during an explicit directory scan.
- Do not follow directory symlinks or process devices, sockets, and other special files.
- Validate file identity and size around reads.
- Bound archive expansion, parser work, source-unit counts, and extracted text.
- Never execute HTML JavaScript, DOCX macros, spreadsheet formulas, embedded programs, or parser network requests.
- Never retrieve external document, image, stylesheet, workbook, or media relationships.
- Treat extracted text and metadata as untrusted output and remove terminal control sequences or escape output contexts.

Each new adapter requires format-specific safety limits and tests.

## Failure handling

Failures identify the source and stage without exposing source contents or credentials. Expected categories include:

- invalid URL/path or unsupported acquisition method;
- blocked network destination or redirect;
- DNS, TLS, timeout, redirect, or HTTP status failure;
- missing, changed, unreadable, special, or oversized local file;
- unsupported, ambiguous, or contradictory source type;
- decoding, parsing, normalization, or empty-content failure;
- unsafe archive, relationship, embedded content, or media structure;
- artifact or provenance persistence failure;
- chunking, transcription, embedding, FTS, or vector failure; and
- final publication failure.

Once registration succeeds, errors produce a durable failed job and no searchable partial document.

## Explicit exclusions from this design

The task intentionally does not define:

- source identity or duplicate-ingestion policy, now defined in [[source-identity-and-repeated-ingestion]];
- source revisions, updates, refresh, or change detection;
- reprocessing after extractor, embedding, or index changes;
- catalog browsing, source discovery, crawling, or SlugKit integration; or
- implementation of any adapter.

The first three exclusions are not left undefined. They are assigned to the explicit design and implementation tasks below.

## Follow-up tasks and dependencies

Every task is high priority. Dependencies are also recorded in each backend task description.

### Source lifecycle policy

| Task | Purpose | Depends on |
| --- | --- | --- |
| `task-ad4349b2` | Design source identity and repeated ingestion | `task-8364d78a` |
| `task-96a0b951` | Implement duplicate-ingestion handling | `task-ad4349b2`, `task-dbf01cd8`, `task-9de42124` |
| `task-9603f915` | Design source revisions and change detection | `task-ad4349b2` |
| `task-08cdcec9` | Implement source refresh and version history | `task-96a0b951`, `task-9603f915` |
| `task-aa5c6e7a` | Design source reprocessing behavior | `task-ad4349b2`, `task-9603f915` |
| `task-aec94144` | Implement source reprocessing workflow | `task-08cdcec9`, `task-aa5c6e7a` |

### Shared pipeline and initial HTML support

| Task | Purpose | Depends on |
| --- | --- | --- |
| `task-dbf01cd8` | Add source-neutral artifacts and source units | `task-8364d78a`, `task-ad4349b2` |
| `task-9de42124` | Add source adapter contract and migrate PDF | `task-dbf01cd8` |
| `task-8dd80bf7` | Add safe source artifact acquisition | `task-dbf01cd8`, `task-9de42124`, `task-96a0b951` |
| `task-fbf94b96` | Unify source ingestion commands | `task-8dd80bf7` |
| `task-d7caa781` | Implement deterministic static HTML extraction | `task-9de42124` |
| `task-f32c1b04` | Connect and index static HTML end to end | `task-fbf94b96`, `task-d7caa781` |
| `task-0d3cae4d` | Extend inventory and search for mixed sources | `task-f32c1b04` |
| `task-c3112638` | Extend discovery for source-neutral evidence | `task-0d3cae4d` |
| `task-2c335ff4` | Extend source packets for mixed sources | `task-0d3cae4d`, `task-c3112638` |

### Additional text and document adapters

| Task | Purpose | Depends on |
| --- | --- | --- |
| `task-cef88236` | Add plain-text source adapter | `task-aec94144`, `task-c3112638`, `task-2c335ff4` |
| `task-95849a05` | Add Markdown source adapter | `task-cef88236` |
| `task-c5005a94` | Add DOCX source adapter | `task-95849a05` |

### Structured data

| Task | Purpose | Depends on |
| --- | --- | --- |
| `task-c089c26c` | Design tabular source ingestion and citations | `task-c5005a94` |
| `task-58d3cf54` | Add CSV source adapter | `task-c089c26c` |
| `task-2f7058f3` | Add XLSX source adapter | `task-c089c26c`, `task-58d3cf54` |

### Transcripts and media

| Task | Purpose | Depends on |
| --- | --- | --- |
| `task-5a11ea37` | Add timed-transcript source adapters | `task-2f7058f3` |
| `task-22b74bdf` | Design audio and video source ingestion | `task-5a11ea37` |
| `task-f4675725` | Add media artifact acquisition and validation | `task-22b74bdf` |
| `task-13483608` | Transcribe and index validated media | `task-f4675725` |

## Verification expectations

Follow-up work must test adapter contracts independently, use mocked network and model boundaries, verify deterministic locations, preserve existing PDF citations, exercise mixed-source search/discovery/packets, and inject failures at each stage to prove that partial documents are never published.

## Approval record

- Individual design decisions: discussed and agreed
- Consolidated document review: approved on 2026-09-02
- Follow-up tasks: created with explicit priorities and dependencies
