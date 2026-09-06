# DOCX sources

NewsRAG ingests local `.docx` files and direct public HTTP(S) DOCX URLs without opening Word, LibreOffice, or an Office rendering engine. Documents use the same immutable artifacts, jobs, hybrid search, discovery, packets, refresh, and reprocessing as the other source types.

```bash
newsrag ingest ./meeting-notes.docx --title "Meeting notes"
newsrag ingest https://example.gov/meeting-notes.docx
newsrag ingest https://example.gov/export --type docx
newsrag ingest ./source-directory
newsrag documents list --source-type docx
newsrag search "road budget" --source-type docx
newsrag packet "road budget" --source-type docx --out packet.md
```

Recursive scans recognize `.docx` case-insensitively alongside PDF, HTML, plain text, and Markdown. Existing regular-file/symlink restrictions remain unchanged; continuous watches remain PDF-only. Manifests accept `type: docx`, with batch validation before enqueueing.

```yaml
documents:
  - source: ./meeting-notes.docx
    title: Meeting notes
    body: City Council
  - source: https://example.gov/export
    type: docx
```

## Format and package policy

The canonical media type is `application/vnd.openxmlformats-officedocument.wordprocessingml.document`. An explicit DOCX hint, that media type, or supporting `.docx` filename evidence selects the adapter. Generic ZIP/octet-stream transport is acceptable with a hint or filename evidence, but ZIP magic alone never classifies an archive as DOCX. Type hints never bypass validation. `.docm`, legacy `.doc`, templates, and other Office formats are not DOCX inputs; contradictory filename/media evidence fails even with an explicit DOCX hint.

The reader supports the transitional WordprocessingML package profile. It validates the required content-types part, root relationships, exactly one related main document with the correct content type and body, related part roots/types, relationship IDs and targets, and reachability. Strict OOXML and unknown package features fail explicitly rather than being partially interpreted. No member is extracted to a filesystem path.

Package validation rejects unsafe/traversing/aliased member names, duplicate parts, symlinks, encryption, unsupported compression, CRC failures, malformed XML, DTDs/entities, missing targets, unrelated/orphan parts, and unsupported active content. Every XML part uses a hardened parser with network, entity resolution, recovery, and oversized trees disabled. Ordinary styles, numbering, notes, themes, properties, and inert custom XML are validated even when they are not extracted as body evidence.

**External relationship policy:** external hyperlink targets are ignored and their visible text is retained. No hyperlink is visited, including non-HTTP targets. Every other external relationship is rejected, including linked images, templates, and external data. Macros, embedded objects, ActiveX, alternative-format imports, and unsafe active fields are rejected; no programs, fields, or Office components are executed.

Embedded images remain bounded inert package bytes. They are not decoded, OCRed, rendered, or used for text extraction. Image-only documents fail with an explicit no-text/image-OCR-excluded error.

## Limits

- Raw artifact: 25 MiB, also enforced during acquisition for known DOCX hints, extensions, media types, and refresh fallbacks.
- ZIP entries: 2,000; only stored and deflated compression.
- Expanded archive: 100 MiB total, 20 MiB per part, and at most a 200:1 per-member compression ratio.
- XML: 100,000 cumulative elements and 128 levels of nesting; style inheritance is limited to 64 links.
- Extracted text: 10,485,760 characters; at most 100,000 canonical units.
- Canonical text, labels, and serialized location/structure metadata: 20,971,520 characters total, preventing repeated long heading paths from amplifying storage without bound.

Declared and actual sizes are checked. Exceeding a limit fails rather than truncating. Unsupported terminal/formatting controls in extracted text or metadata fail clearly. Public-network validation, redirect limits, timeouts, local-file stability checks, and credential protections remain in force.

## Structure and citations

Canonical units retain body order, headings and inherited paragraph styles, paragraphs (including blanks), list identity/level/numbering definitions, table rows, and referenced footnotes. Tabs and line breaks in text runs are preserved. Inserted text is retained and deleted text is omitted; fields are not evaluated. Heading levels come from outline properties or recognized heading styles, never from inferred civic meaning.

Tables retain ordered cell text, grid spans, and vertical-merge metadata. Nested tables remain in source order within their outer cell and are cited through that outer row. List numbering metadata is retained rather than recreating layout-dependent displayed counters. Paragraph numbers count body paragraphs independently of tables and footnotes. Table numbers and row numbers identify extracted tables/rows, not page layout.

A footnote is emitted immediately after its first referring paragraph or table row, with its package footnote ID and note-local paragraph number. Later references retain a marker to the same note. Separator notes are not evidence. Footnote IDs are package identifiers, not an assertion about Word's displayed numbering. Endnote references and unsupported body structures fail explicitly; headers, footers, comments, custom metadata XML, and image/textbox content are not included as main-body evidence.

All units use `docx_block` locations with a consecutive `block_number` plus typed paragraph, table/row, or footnote fields. Heading-aware examples include `Council notes — Budget — paragraph 2`, `Council notes — Budget — table 1, row 2`, and `Council notes — Budget — footnote ID 2, paragraph 1`. Ranges can span different native unit kinds while retaining exact source-unit identities. Inventory reports canonical blocks, never invented page numbers.

Core title, author, and language properties may fill missing metadata; explicit user metadata wins. No meeting date, civic body, or jurisdiction is inferred. Search, discovery, facts, briefs, enrichment, and packets preserve typed citations and artifact/revision/generation provenance. Generated Markdown output escapes DOCX text and source-controlled labels so literal markup cannot become active content; stored evidence stays unchanged.

## Lifecycle

Exact-byte duplicates follow first-successful-import-wins behavior. Ordinary changed-byte ingestion stages an artifact; explicit refresh publishes only after full validation/indexing. Invalid replacement archives cannot replace current searchable evidence. Historical-byte reactivation retains the original revision without reindexing, and `--include-history` continues to mean source-revision history.

Refresh retains accepted DOCX evidence and its acquisition cap for extensionless or generically served sources while allowing fresh recognized format evidence to select another supported format. Reprocessing reads verified saved bytes, never the original file or live URL, and pins extractor/package settings in its fingerprint. Old units, citations, processing generations, and packets remain retained; a failed rebuild leaves the current generation usable. See [Reprocessing](reprocessing.md) for batch limits, configuration pinning, and recovery behavior.
