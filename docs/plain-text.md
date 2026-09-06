# Plain-text sources

NewsRAG ingests literal plain text from explicit local files and direct public HTTP(S) URLs, using the same durable jobs, immutable artifacts, hybrid search, discovery, packets, refresh, and reprocessing workflows as PDF and HTML.

```bash
newsrag ingest ./meeting-notes.txt --title "Meeting notes"
newsrag ingest https://example.gov/meeting-notes.txt
newsrag ingest ./extensionless-notes --type text
newsrag ingest ./source-directory
newsrag documents list --source-type text
newsrag search "road budget" --source-type text
newsrag packet "road budget" --source-type text --out packet.md
```

Directory scans recognize `.txt` case-insensitively and may mix text, PDF, and HTML files. Scans retain existing regular-file and symlink restrictions. Continuous folder watching remains PDF-only; this task does not add text watches.

Manifests use the existing `source` field and optional `type: text`:

```yaml
documents:
  - source: ./meeting-notes.txt
    title: Meeting notes
    body: City Council
  - source: https://example.gov/export
    type: text
```

The manifest is validated before any jobs are created. Adapter validation occurs in the worker after acquisition; a valid manifest is not a guarantee that the supplied bytes are valid text.

## Type and encoding

Text selection requires an explicit `text` hint, a `text/plain` response media type, or supporting `.txt` filename evidence. NewsRAG does not classify arbitrary printable bytes as plain text without such evidence. An explicit hint does not bypass validation. Contradictory non-text media types, known binary signatures, and conservative PDF/HTML/XML document signatures are rejected by the text path. Generic binary transport types may use `.txt` or an explicit text hint, but bytes still undergo strict decoding and validation.

A charset declares how bytes map to characters. A byte-order mark (BOM) is an optional leading encoding marker, not part of the evidence text. Supported decoding is deterministic:

- UTF-8 is the default when no charset or BOM is present, including ordinary ASCII text.
- UTF-8 BOMs are accepted and removed from decoded text.
- UTF-16 little-endian and big-endian input requires a matching BOM. A generic `charset=utf-16` declaration is accepted when the BOM specifies the byte order.
- Explicit `charset=us-ascii`, `charset=windows-1252`, and `charset=iso-8859-1` declarations are supported, including recognized aliases.
- Unsupported, invalid, conflicting, or BOM-mismatched charset declarations fail. Invalid byte sequences never fall back to another encoding or replacement characters. UTF-32 is not supported.

Remote text/HTML charset parameters survive acquisition. The actual input media type, including its charset, is retained in processing options so retries and reprocessing decode the same preserved bytes consistently. Reprocessing never consults a live source header to reinterpret an old artifact. An unmarked non-UTF-8 local file is rejected rather than guessed; convert it explicitly to UTF-8 before ingesting.

## Limits and safety

Plain text has a 10 MiB input-byte limit, a 10,485,760 decoded-character limit, and a 100,000 physical-line limit. Known text hints, filenames, and response media types apply the byte limit during acquisition, including compressed and decompressed remote bytes. The adapter independently enforces its input, decoded-text, and line-count limits and fails rather than truncating. Empty and whitespace-only documents are rejected.

NUL, terminal escape sequences, unsupported control characters, and formatting controls such as bidirectional overrides are rejected. Tabs, CR/LF line endings, and Unicode joining characters are supported. No scripts, embedded resources, links, markup, or source contents are executed or fetched. Existing public-network, redirect, credential, timeout, and local-file stability protections remain unchanged.

## Line citations and metadata

Every physical line becomes a canonical `text_line` source unit with one-based `line_start`/`line_end` locations. Blank lines count and retain their positions. CRLF and CR are normalized to LF; leading/trailing spaces and tabs remain in canonical line text. A final line terminator does not invent an extra empty line, while genuine trailing blank lines are retained. Inventory reports `lines`, never invented pages or HTML blocks.

For example, `first\r\n\r\nthird\r\n` contains three lines: text at lines 1 and 3 and a blank line 2. A citation can say `Meeting notes — line 3` or `Meeting notes — lines 1–3`. Chunking uses the existing non-page source-unit chunker; blank lines produce no searchable chunks but their source-unit identities remain available. A long line may create multiple chunks sharing that same line location.

The adapter records the actual decoding choice as `text_encoding`. It does not infer a title, meeting date, civic body, or jurisdiction from text contents. Existing explicit user metadata takes precedence. Search, facts, briefs, enrichment, discovery browsing, and packets use typed line locations and retain artifact hashes and source/revision/processing-generation provenance.

Markdown parsing is outside this adapter. `.md` files are not automatically scanned as text. Markdown-like characters inside an accepted plain-text artifact remain literal; headings, lists, links, and code blocks are not interpreted.

## Lifecycle behavior

Exact-byte duplicates retain first-successful-import-wins behavior. Ordinary ingestion of changed bytes stages them; explicit refresh may publish a new source revision. Refresh retains the accepted text type and 10 MiB acquisition limit even for extensionless files and generic-content-type URLs originally accepted through an explicit text hint. Fresh recognized media types and content signatures take precedence over that fallback, so supported format changes still work; the fallback never bypasses text validation. Historical reactivation reuses the original revision without reindexing. Current-only and `--include-history` source-revision scopes apply to text as to other formats.

Reprocessing preserves the original document/artifact/revision identity, charset choice, and older processing generations. New line units receive generation-specific IDs without rewriting old line text or citation anchors. Failed processing cannot replace currently searchable evidence. Existing packets remain unchanged. See [Reprocessing](reprocessing.md) for bounded batches, saved-configuration retries, and failure recovery.
