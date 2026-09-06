# CSV sources and exact cell evidence

CSV ingestion follows the [approved tabular contract](research/tabular-source-ingestion-and-citations.md). CSV is supported; XLSX parsing, calculation, joins, aggregation, hidden-content overrides, and executable spreadsheet export are not implemented.

## Ingest and search

```bash
newsrag ingest ./expenses.csv
newsrag ingest https://example.gov/expenses.csv
newsrag ingest https://example.gov/export --type csv
newsrag ingest ./expenses.csv --csv-delimiter semicolon --csv-header absent --csv-encoding utf-8
newsrag search "Roads" --source-type csv
newsrag documents list --source-type csv
newsrag documents show <document-id>
newsrag packet "Roads" --source-type csv --out roads.md
```

Case-insensitive `.csv`, `text/csv`, and `application/csv` select the adapter. Generic transport requires explicit or filename evidence; printable text and ZIP signatures do not identify CSV. `text/plain` stays plain text unless explicitly selected as CSV (or refreshing an already accepted CSV source). An explicit CSV hint preserves text charset parameters but cannot override contradictory non-text media or invalid source bytes. Public HTTP(S) acquisition retains the existing redirect, DNS, private-address, timeout, and size protections.

Recipe flags select CSV when `--type` is omitted. Conflicting type/PDF options and invalid recipe values fail before enqueueing. Recipe flags cannot apply to a directory scan: use a manifest for per-file settings. Recursive directories include CSV alongside existing formats; mixed-source retrieval remains the default.

```yaml
documents:
  - source: ./expenses.csv
    type: csv
    csv:
      delimiter: semicolon
      header: absent
      encoding: utf-8
  - source: ./meeting-notes.md
```

Only `delimiter`, `header`, and `encoding` are accepted under `csv`. Unknown keys, wrong types, and cross-format options reject the entire manifest without creating partial jobs.

## Deterministic interpretation

- Delimiters: `comma` (default), `semicolon`, `tab`, or `pipe`. No sniffing, fallback dialects, comments, backslash escapes, or whitespace trimming.
- Headers: `present` (default) or `absent`. With headers, logical record 1 must exist and cannot be a zero-field blank record. A header-only artifact fails. Duplicate and empty labels retain their column coordinates; repeated header-looking rows remain ordinary data.
- Encoding: `auto` (default) means strict BOM/declaration/default rules, not detection. UTF-8 is the default; UTF-8 BOMs are removed. UTF-16 LE/BE requires a matching BOM. Declared ASCII, Windows-1252, Latin-1, and recognized aliases are supported. Explicit encodings, HTTP charsets, and BOMs must agree. An HTTP CSV `header` parameter must agree with the recipe.
- Quoting: double quotes enclose fields and doubled quotes represent literal quotes. Quoted delimiters and newlines are preserved. CRLF/CR normalize to LF, including inside values. Quotes inside unquoted fields, unfinished quotes, text after closing quotes, and ragged nonblank records fail.
- All fields remain strings. `001200`, spaces, empty strings, and `=HYPERLINK(...)` are literal data. No formulas, arithmetic, currency, units, booleans, or civic dates are inferred. Zero-field blank records become blank positions, distinct from empty-string fields. A final terminator does not invent a record.

Rows are one-based **logical records**, not physical lines. CSV has synthetic sheet 1 and table `sheet-1`. Column labels A/B/C are generated orientation, not source header text.

## Retrieval and citations

Each passage focuses on one data row and a contiguous stripe of at most 64 complete cells. Header and immediate preceding/following data-row context are separately attributed. Blank rows are not skipped to find more convenient neighbors. Optional context that exceeds a budget is omitted whole with an explicit reason; focus values are never shortened or split.

Keyword indexes contain literal focus values and applicable header values in separate fields, not generated coordinate/type labels. Results distinguish `focus` matches from `header-context` matches. Neighbor-only keyword matches cannot be attributed to the focus. Embeddings include bounded, labeled context while retaining the focus selector.

A result such as `expenses — cells B3:C3` carries exact cell coordinates, row-unit anchors, document identity, and processing generation. Quote validation checks the persisted selected cells, not arbitrary words in the combined row/passage. Multi-cell excerpts use coordinate-mapped representations such as `B3=string:"001200"`; a single selected cell can also support its exact literal value. Header and neighbor claims require their own supporting selectors.

Discovery facts report extractive table values rather than running prose money/date/action heuristics over rows. Structured CSV enrichment validates the same typed regions: summaries and claim summaries must retain exact extractive values, and labels must be coordinates or attributed literal values/headers. It rejects inferred arithmetic or relationships rather than presenting them as source facts.

Packets retain focus/context labels, exact machine selectors, original artifact/source/revision/generation provenance, and the retrieval snapshot. Source-controlled punctuation is entity-escaped, making formula-looking strings, HTML, images, and links inert without changing the decoded literal values. Inventory reports rows, sheet/table counts, and bounded extents rather than invented pages.

## Limits and failure behavior

Limits are rejection boundaries, never truncation targets:

| Resource | Limit |
| --- | --- |
| Acquired bytes / decoded characters | 10 MiB / 10,485,760 characters |
| Physical lines / logical records | 100,000 each, including headers and blank records |
| Columns / rectangular positions | 256 / 1,000,000 |
| Individual value / aggregate canonical value characters | 8,192 / 10,485,760 |
| Serialized descriptors, cells, row units, and labels | 64 MiB UTF-8 |
| Focus / context / full passage | 16,384 / 16,384 / 32,768 serialized characters |
| Generated passages / aggregate index text | 100,000 / 64 MiB UTF-8 |
| Evidence materialization | 256 cells and 32,768 rendered characters, including context |

Escaped rendering can be larger than source text. A cell that cannot fit the focus budget fails; an explicit whole-table/column region or escaped packet item that exceeds materialization limits requires a narrower selection. Malformed inputs, unsupported controls/signatures, and conflicting declarations fail without partial publication. SQLite publication and attempted-vector compensation retain the previous visible generation after failures.

## Refresh and reprocessing

```bash
newsrag refresh <source-id>
newsrag reprocess <document-id> --csv-header absent
newsrag reprocess <document-id> --csv-delimiter pipe --csv-encoding windows-1252
newsrag documents generations <document-id>
newsrag jobs retry <failed-job-id>
```

Identical bytes remain `duplicate_ignored`, even when supplied with another valid recipe. Ordinary changed bytes are staged; explicit refresh publishes a new revision or reactivates retained historical bytes. Refresh captures the active generation's requested recipe, not stale initial-ingestion options, and validates each new artifact's declarations independently.

Reprocessing reads verified preserved bytes and never refetches. Omitted settings inherit the active generation; explicit settings replace only those settings. CSV overrides reject a mixed-format batch before inserting any jobs. Unchanged fingerprints are verified no-ops. Changed recipes create a new processing generation with new selectors while old anchors, packets, revisions, and artifact identities remain intact. Retries retain the saved recipe/configuration and base-generation conflict checks.

## Upgrade and development safety

Stop older daemons before upgrading to schema **8**; do not mix worker versions. Migration adds generation-owned table/cell/passage relations and optional discovery selectors without assigning invented table locations to legacy evidence. Existing IDs, text, FTS entries, citations, and processing history remain unchanged. Inconsistent ownership fails explicitly. Migration is idempotent.

Use disposable corpora for migration, failure injection, and CLI/daemon testing. Development services must start via `make dev`; use an isolated data directory and configuration. Do not test against installed corpora or restart installed services. See [Development](development.md) and [Reprocessing](reprocessing.md).
