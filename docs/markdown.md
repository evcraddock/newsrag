# Markdown sources

NewsRAG ingests local `.md` files and direct public HTTP(S) Markdown URLs. Markdown is a separate source type, not a rendering mode for plain text.

```bash
newsrag ingest ./meeting-notes.md --title "Meeting notes"
newsrag ingest https://example.gov/meeting-notes.md
newsrag ingest https://example.gov/export --type markdown
newsrag ingest ./source-directory
newsrag documents list --source-type markdown
newsrag search "road budget" --source-type markdown
newsrag packet "road budget" --source-type markdown --out packet.md
```

Recursive directory scans recognize `.md` case-insensitively alongside PDF, HTML, and `.txt`, retaining regular-file and symlink protections. Continuous folder watching remains PDF-only. Manifests support `type: markdown`; all entries are validated before any jobs are created.

```yaml
documents:
  - source: ./meeting-notes.md
    title: Meeting notes
    body: City Council
  - source: https://example.gov/export
    type: markdown
```

## Type, decoding, and limits

The canonical media type is `text/markdown`. An explicit `markdown` hint, a recognized media type, or supporting `.md` filename evidence selects the adapter; the usual media/signature precedence still applies. Generic binary transport types need filename evidence or an explicit hint. Servers reporting `text/plain` select plain text unless an explicit Markdown hint is supplied; that hint retains the declared charset while selecting Markdown. Contradictory non-text media types still fail validation.

Markdown shares the [plain-text decoding policy](plain-text.md#type-and-encoding): strict UTF-8 by default, supported declared ASCII/Windows-1252/Latin-1, matching UTF-8/UTF-16 BOM handling, and rejection of invalid or conflicting encodings. It shares the 10 MiB raw-byte limit, 10,485,760 decoded-character limit, and 100,000 physical-line limit. Known hints, filenames, and response types impose the byte cap during acquisition, including compressed and decompressed remote bytes. Binary signatures, PDF signatures, unsupported controls, empty input, and whitespace-only input fail rather than being guessed or truncated. Unlike plain text, literal HTML/XML markup is valid Markdown content.

## Structure and citations

The adapter uses markdown-it-py's CommonMark block parser with tables enabled and inline parsing disabled. It identifies headings, paragraphs, ordered/bullet lists, block quotes, tables, fenced/indented code blocks, thematic breaks, and literal HTML blocks. List and quote ancestry is retained in each leaf block's `containers` metadata; tables are single canonical blocks. Heading units include levels and ordered heading paths. Fenced code retains delimiters and the literal info string without executing it. The parser has a 64-level nesting budget; source not classified as a block, including reference definitions or over-deep syntax, remains literal source evidence rather than being dropped.

Canonical `markdown_block` units retain exact decoded source slices, including Markdown syntax, spaces, tabs, and blank lines. CRLF/CR normalize to LF. Locations use original one-based inclusive `line_start` and `line_end`; blank gaps are retained, and a terminal newline does not invent a phantom line. Unit ordinals are separate from physical line numbers. A multiline paragraph, table, or code block carries its full source line range, even when chunking splits it into shorter search passages.

For example, a citation can say `Meeting notes — Budget — Roads — lines 13–15`. Inventory reports original line counts, not parser block counts or invented pages. Search, discovery, facts, briefs, enrichment, and packets resolve the same source-unit identities and retain artifact, revision, and processing-generation provenance.

No Markdown or embedded HTML is rendered or executed, and no links, images, scripts, includes, or other resources are fetched. Packets and generated briefs escape Markdown source evidence for inert display; stored canonical evidence remains unchanged. No civic metadata or document title is inferred from headings; explicit user metadata takes precedence over the adapter's `text_encoding` candidate.

## Refresh and reprocessing

Markdown uses the existing duplicate and revision policies: first successful exact-byte import wins, ordinary changed-byte ingestion stages an artifact, explicit refresh publishes only after complete processing, and historical bytes reactivate their original revision without reindexing. Failed replacements leave current searchable evidence intact. Existing packet files are never rewritten.

Refresh retains an accepted Markdown type and the text byte cap for extensionless or generically served sources. A `text/plain` response remains Markdown when that source was already accepted as Markdown. Literal HTML signatures also remain Markdown in this fallback context because embedded HTML is valid Markdown; a fresh recognized HTML media type or an unambiguous PDF signature can still select another supported format. Retained type evidence never bypasses validation.

Reprocessing uses the saved raw artifact and pinned input charset, not a live URL or original local file. Parser version and settings participate in the processing fingerprint; unchanged configurations yield integrity-verified no-ops, while explicit rebuilds retain old units and citation anchors as historical processing generations. See [Reprocessing](reprocessing.md) for batch limits, retries, configuration pinning, and publication guarantees.
