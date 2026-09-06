# Tabular source ingestion and citations

Status: Approved by explicit human review in `task-c089c26c`. This is the design deliverable, not an implementation of tabular runtime behavior.

## Goal and boundary

Define one bounded evidence model for CSV and XLSX: preserve source coordinates and values, make rows understandable in search, and issue citations that distinguish cited cells from contextual cells. Build on [source-neutral ingestion](non-pdf-source-ingestion.md), [exact-byte identity](source-identity-and-repeated-ingestion.md), [source revisions](source-revisions-and-change-detection.md), and [processing generations](../reprocessing.md).

Reuse `task-58d3cf54` for CSV plus the shared tabular foundation, and `task-2f7058f3` for XLSX. Refine these existing tasks after design approval; create no duplicate tasks. XLSX continues to depend on CSV and this design. This task changes documentation and approved task descriptions only: no adapter, schema migration, parser dependency, commands, or corpus mutation.

The first version is evidence retrieval, not a spreadsheet engine. It does not calculate formulas, infer table regions or civic metadata, join tables, aggregate amounts, infer units/currencies/dates from formatting, reconstruct Excel's displayed formatting, or export executable CSV/XLSX. Existing PDF, HTML, text, Markdown, and DOCX behavior must remain compatible.

## Decisions at a glance

- One artifact remains one document, including a workbook containing multiple sheets.
- One canonical rectangular table plane per worksheet; CSV has one synthetic sheet and table. Native Excel tables annotate regions of that plane rather than duplicating their cells.
- Coordinates retain source row/column positions. CSV rows are logical records, not physical text lines; XLSX rows/columns are worksheet coordinates.
- One canonical source unit per row in the table's bounded extent, including blank and hidden rows; cells remain individually typed and addressable.
- Headers are declared, not guessed. CSV defaults to a first-record header, with an explicit no-header option. XLSX uses native table header declarations or explicit per-sheet header-row overrides; otherwise column letters provide orientation.
- Search cites a focus rectangle and separately identifies header/neighbor context. Precise cell selectors cannot be replaced with the existing row-unit endpoints alone.
- Formula expressions and cached results are separate. Never calculate, translate, or refresh formulas; caches are explicitly unverified snapshots.
- Hidden sheet/row/column values are retained canonically but excluded from indexing, discovery, and newly generated evidence. There is no include-hidden override in this version.
- Exact bytes, source-revision history, generation-pinned recipes, and old citation anchors remain authoritative.

## Canonical model

### Document, sheet, table, and native regions

A CSV artifact has synthetic `sheet_index=1`, no source sheet name, and `table_id=sheet-1`. An XLSX artifact has sheets in workbook-declared order, with a one-based `sheet_index`, original sheet name, package sheet ID, relationship target, and visibility state. A package sheet ID or name is provenance, not a globally stable identity. Unsupported sheet types fail; never silently reorder or renumber visible sheets after excluding hidden sheets.

Each sheet owns one canonical table plane with generation-local `table_id=sheet-<sheet_index>`. Empty worksheets have a descriptor but no row units. A workbook with no eligible visible, nonblank, non-error, literal or cached-value cells fails with an explicit no-searchable-evidence error rather than publishing an empty document.

XLSX native table definitions retain their IDs/names, rectangular ranges, column names, header-row count, and totals-row count as region descriptors within the worksheet plane. Multiple disjoint native tables and surrounding cells remain in source order. Native tables do not duplicate cells, source units, or index records. Reject overlapping native table regions, invalid column counts, contradictory declared header/totals extents, and out-of-bounds references. Named ranges are retained as bounded descriptive metadata only, not evaluated or used to discover additional tables.

Descriptors are stored once per processing generation, not copied into every cell or passage. A table descriptor includes source type, sheet provenance, extent, header policy, native regions, merges, hidden-coordinate intervals, and source-order information. Use separate typed descriptor/cell relations or equivalently constrained records; do not introduce a second document identity for a table.

### Extent and source order

CSV width is the header's field count when a header is present, or the first nonempty logical record's field count otherwise. Every nonempty record must have that width. Retain blank records as blank rows of that width, including leading blanks in no-header mode and real trailing blanks. A final record terminator does not invent another record. Header-present mode requires a nonblank first logical record; it never searches ahead for a likely header. Reject an artifact with no nonempty record.

For XLSX, derive the occupied rectangle from explicitly stored values/formulas, formula-group range endpoints, merge endpoints, and native table ranges. Empty-string cells count as stored cells. Styles alone, empty row declarations, column formatting, and the producer's cached worksheet `dimension` do not expand the evidence rectangle. Validate native coordinate bounds and metadata independently; never allocate from `dimension` alone. Preserve the rectangle's actual first row/column, not an artificial A1 origin. Missing cells/rows inside it are blank positions. Budget the entire rectangle before materializing anything, including hidden/blank positions; sparse far-apart cells must fail when their bounding geometry exceeds limits.

Source-unit ordinals are consecutive across all sheets in workbook order, then increasing source row order within each nonempty table plane. Physical row numbers need not equal ordinals and need not begin at 1. Blank/hidden row units retain their coordinates and IDs even though they do not independently create searchable passages.

### Cell values

Each cell has a row and column, presence state, value kind, raw stored representation, canonical value, visibility, and optional formula/merge metadata. Blank is not zero, the string `""`, or an absent formula cache.

| Input | Canonical behavior |
| --- | --- |
| CSV field | Literal Unicode string; preserve spaces, leading zeros, punctuation, and embedded newlines. Do not infer numbers, booleans, dates, or formulas. |
| CSV zero-field blank record | Blank positions across the established width; record the blank-record origin. Quoted/unquoted empty fields are empty strings, not absent cells. |
| XLSX absent cell or explicit empty cell | Blank, with presence recorded separately. |
| XLSX shared/inline string | Resolve the validated stored string; rich-text runs concatenate in order without interpreting formatting. Preserve an empty string. |
| XLSX number | Preserve the lexical number and an exact decimal representation, not a binary-float approximation. Reject invalid/nonfinite numeric lexemes. |
| XLSX boolean | Validate `0`/`1`; retain raw representation and boolean value. |
| XLSX ISO date cell | Validate the stored ISO date/date-time form, retain it exactly, and invent no timezone. |
| XLSX numeric cell with a date/currency/percentage style | Remains a number; retain bounded number-format/style identifiers as metadata. Do not convert serial dates, add a currency sign, or rescale values. |
| XLSX error | Retain the explicit error token/type. It is not a numeric fact or a substitute for missing data. |

Retain the workbook's 1900/1904 date-system setting. Because serial numbers are not converted, no fictitious 1900-02-29 or guessed locale is introduced. Unknown cell types, invalid string indices, duplicate cell coordinates, malformed row/cell associations, and unsupported value representations fail clearly.

### Headers, repeated headers, and merged cells

A header is an explicit source region, never a replacement for row coordinates. Duplicate or blank labels remain unchanged; disambiguate by column letter/number, not invented source text. The exact header cells accompany header context. For example, `B (Amount)` and `C (Amount)` remain different columns. A repeated header-looking row stays an ordinary row unless explicitly declared as a header; do not remove it or use value similarity to reset a table.

CSV uses record 1 as the header by default. `header=absent` preserves that record as data. XLSX native table headers apply only to data cells in their declared table region. Outside those regions, an explicit per-sheet header row may provide labels; otherwise use column letters. An explicit override applies to that sheet's plane and takes precedence over native header labels, but native metadata is still preserved. The row must exist within the occupied extent and be visible; do not infer a multirow header. Empty/hidden header cells use a column-letter fallback. A header-only file has no searchable data rows and fails clearly.

Retain each XLSX merged rectangle once with its top-left anchor. Only the anchor may contain a stored value/formula; covered cells are `merged-covered` references, not repeated copies of the anchor value. Reject overlaps, conflicting covered values, and out-of-bounds merges. A quote of an anchor value cites the anchor and explicitly discloses its merged rectangle; requesting only a covered cell cannot manufacture a value. Never expand an anchor value across columns to create additional matches or facts. A merged header labels only its anchor column unless explicitly supplied as ordinary separate header cells; do not infer a hierarchy or fill labels rightward.

### Formulas and stored values

Preserve formula text, formula kind, group/base identifiers and declared ranges separately from each cell's cached value and cache-presence status. Support normal, shared, and array formula representations as stored. Shared/array followers point to the original group definition; do not translate an anchor formula to invent per-cell expressions. Validate group membership, unique anchors, declared ranges, and references. Unsupported data-table formulas fail explicitly.

A present valid cache may contribute a value to search, accompanied by `formula-cache; freshness not verified` in result context, discovery evidence, and packets. Preserve the formula's original anchor/expression in provenance without placing it in normal value indexes. A missing cache remains unavailable even if related cells or a group anchor have caches. Missing caches and error caches do not become numeric facts; a file consisting only of unavailable/error results is not searchable evidence. An explicitly cached empty string remains distinguishable from an absent cache but creates no independent value match.

Never calculate formulas, launch Excel, invoke a spreadsheet engine, update external links, execute a macro, or fetch a URL/function target. Expression strings such as `HYPERLINK`, `WEBSERVICE`, or a CSV field starting with `=`, `+`, `-`, or `@` are inert source data, not instructions. XLSX external-link/package relationships are governed by the safety policy below, regardless of cached values. A formula cache is evidence of what was stored, not proof that the expression is correct or current.

### Hidden content

Retain sheet states (`visible`, `hidden`, `veryHidden`) and hidden row/column intervals. A cell is eligible only if its sheet, row, and column are all visible. Hidden values/formulas remain in canonical typed storage and the original artifact, but must not enter normalized evidence text, FTS/vector inputs, headers, neighboring-row context, deterministic facts, enrichment prompts, briefs, or packets. Direct source-range resolution for a new evidence request rejects hidden cells rather than bypassing this rule.

The exclusion is a retrieval policy, not a claim of encryption or access control: the corpus owner still has the original artifact. Report excluded sheet/row/column counts and whether exclusion affected a selected region, without echoing excluded values. Do not close coordinate gaps or renumber remaining rows/columns. Discontiguous visible intervals produce separate focus/context regions; never imply that omitted hidden cells were quoted. Metadata names/visibility and native extents remain visible as provenance. No include-hidden command, automatic unhiding, or inference from hidden values is added.

## Machine locations and human citations

### Locations

All coordinates are one-based inclusive integers, except null row/column bounds on an empty table descriptor. Reject booleans masquerading as integers, reversed/out-of-bounds ranges, unknown table/sheet references, hidden evidence cells, mixed generations, and mismatched document ownership.

A row source unit uses `location_type=table_row` and identifies `table_id`, `sheet_index`, `row_start=row_end`, `column_start`, and `column_end`. Cell values and source-native annotations are structured data, not an anonymous flattened prose blob. Source-unit identity is document/generation/ordinal based, consistent with existing non-page units; table IDs alone never identify evidence across documents or generations.

An evidence region uses `location_type=table_region`, one `table_id`/`sheet_index`, a `region_kind` (`table`, `rows`, `columns`, or `cells`), and resolved inclusive row/column bounds. A single cell is a one-cell rectangle. `table` means the validated full extent, `rows` means full-width rows, and `columns` means full-height columns; bounded `cells` is an arbitrary rectangle. An empty table has inventory metadata but cannot supply evidence. Whole-region requests must still obey eligibility and output budgets; never silently narrow a `columns` citation to a few matching rows.

Every evidence reference also carries existing source/artifact/document/revision/processing-generation identifiers and exact source-unit row endpoints. A region cannot cross tables, sheets, artifacts, or generations. Multi-sheet/discontiguous evidence uses several explicit regions, not one synthetic range. Validate both endpoints and the intervening canonical rows/cells against persisted descriptors; an integer rectangle or two source-unit IDs alone are not sufficient evidence.

For each passage/evidence item, store one focus region plus a bounded ordered context-region list. Each context reference records its role (`header`, `preceding`, `following`, or `merge-anchor`), row-unit endpoints, and exact cell selector. Context must share the focus table/document/generation. A header on row 1 must not widen a row-500 focus to rows 1–500.

### Example machine reference

For a CSV header on row 1 and focus cells B3:C3, a generation-qualified reference has this shape (symbolic IDs shown only for illustration):

```json
{
  "document_id": "document-example",
  "processing_generation_id": "processing-example",
  "location_type": "table_region",
  "table_id": "sheet-1",
  "sheet_index": 1,
  "region_kind": "cells",
  "row_start": 3,
  "row_end": 3,
  "column_start": 2,
  "column_end": 3,
  "source_unit_start_id": "unit-row-3",
  "source_unit_end_id": "unit-row-3",
  "context": [
    {"role": "header", "row_start": 1, "row_end": 1, "column_start": 2, "column_end": 3,
     "source_unit_start_id": "unit-row-1", "source_unit_end_id": "unit-row-1"}
  ]
}
```

Context references inherit only the outer table/document/generation identity; their own bounds and row endpoints remain explicit. Source, artifact hash, and revision provenance are retained in the containing result, as for existing formats.

### Human labels

| Region | Example |
| --- | --- |
| CSV table | `expenses.csv — table 1 — rows 1–24, columns A–D` |
| CSV rows | `expenses.csv — rows 3–5` |
| CSV columns | `expenses.csv — columns B–D (rows 1–24)` |
| CSV cells | `expenses.csv — cells B3:C5` |
| XLSX worksheet plane | `budget.xlsx — sheet 2 “FY 2026” — table extent B4:F120` |
| XLSX rows | `budget.xlsx — sheet 2 “FY 2026” — rows 8–10` |
| XLSX columns | `budget.xlsx — sheet 2 “FY 2026” — columns C–E (rows 4–120)` |
| XLSX cells | `budget.xlsx — sheet 2 “FY 2026” — cells C8:E10` |

Column letters are coordinate labels, never inferred header text. CSV uses the same A/B/C notation without implying it was an Excel workbook. A native table name may accompany an XLSX citation only when the cited region belongs to that native table; it does not replace sheet index or coordinates. Escape all names and source values in output. Include separate context labels, formula-cache qualifications, and merged-region annotations where applicable. Non-tabular page/line/block citations remain unchanged.

## Search passages, context, and evidence validation

Use the existing SQLite FTS5 plus compatible LanceDB hybrid retrieval. Add a tabular chunking path, not the current arbitrary character slicing of flattened row strings. Each searchable passage focuses on one visible data row and a deterministic contiguous visible column stripe of at most 64 cells. Greedily pack complete cell representations in increasing column order within the focus budget; never split a cell, silently drop a column, or pack across hidden columns. Long rows may produce multiple nonoverlapping focus stripes. Header rows and wholly blank/error/unavailable-cache rows do not independently create hits.

Render cells deterministically using coordinate keys and JSON-quoted strings or explicitly tagged numeric/boolean/date/error/blank representations. Preserve embedded newlines via escapes, not extra apparent rows. Missing/covered/hidden cells are not invented values. Canonical cells retain the original decoded/stored value; generated coordinate/type labels are explicitly orientation, not literal source prose.

For each focus stripe, supply the applicable header cells and at most the immediately preceding and following physical data row over the same columns. Do not skip across blank rows, hidden rows, headers, native-table boundaries, or sheet boundaries to find a more convenient neighbor. A native-table header is not itself a neighbor. If a header or neighbor would exceed the context budget, omit that entire optional context region and record why; column coordinates always remain available. Do not truncate context values. Attach context in header, preceding, following, then merge-anchor order, subject to the shared per-item cell/character budgets. Omitted context is not indexed or sent to enrichment; an omitted header uses coordinate labels instead. Header selection must be consistent within a stripe; split at changes between native header regions.

Use combined focus/header/neighbor text for embeddings, but keep focus and context roles persisted. Keyword retrieval searches focus values and applicable header labels in separate fields; neighbor-only matches cannot masquerade as focus matches. Header-only keyword matches must be identified as such. Vector similarity may use context, but returned evidence clearly distinguishes its focus from contextual rows. Apply source/revision/generation/visibility eligibility before candidate limits and context expansion. Deduplicate by focus identity, not merely the shared row unit.

Quote validation must resolve the exact cited cells. It must not accept any substring from the combined passage as proof about the focus. A normalized tabular excerpt may contain deterministic coordinate/type labels, but must be identified as an extractive table representation, not a verbatim prose quotation. Validate serialized values and their coordinate mapping against the selected region; if a claim quotes a header or neighbor, attach that region explicitly as supporting evidence. Do not validate a multi-cell claim by finding its words somewhere in the full worksheet row.

Deterministic facts, enrichment, topics, timelines, and leads use the same typed selectors and eligibility. They may report source-supported values with explicit header context and cache/error qualifications. They must not invent arithmetic, currency, units, dates, or relationships between distinct rows. Never synthesize a total or infer a meeting date from a column label. Keep existing quote/source validation for non-tabular evidence unchanged.

Packets keep the existing evidence/timeline/open-questions/source-list structure. Tabular items show the focus rectangle, exact selected cells, separately labeled context, and full source/revision/generation/artifact provenance. Render an escaped literal representation, never executable CSV, formulas, active links/images, or raw HTML. Packet generation must use the retrieval snapshot even if refresh/reprocessing publishes concurrently. Inventory shows source type, sheet/table counts, bounded extents, and excluded-hidden counts rather than pages. `--source-type csv|xlsx` composes with existing filters; mixed-source retrieval remains the default.

## CSV decoding, dialect, and options

Register `csv`, canonical `text/csv`, alias `application/csv`, and case-insensitive `.csv`. There is no CSV content signature or arbitrary printable-text fallback. Generic transport needs explicit/filename evidence. `text/plain` stays plain text without an explicit CSV hint; with that hint, preserve its charset for CSV decoding. An explicit hint cannot override contradictory non-text media or invalid bytes. `.tsv` is not automatically registered by this task, though an explicitly selected CSV source can use tab delimiters.

Reuse strict text BOM/charset decoding: UTF-8 by default; UTF-8 BOM removed; UTF-16 LE/BE requires a matching BOM; declared ASCII, Windows-1252, and Latin-1 plus recognized aliases are supported. Unsupported/invalid/conflicting declarations fail. An explicit encoding, HTTP charset, and BOM must agree; an override is not permission to reinterpret contradictory bytes. Retain the actual per-artifact media/charset and chosen encoding for reprocessing.

A supplied HTTP CSV `header` parameter must be `present` or `absent` and agree with the chosen header recipe; a conflict fails with guidance to supply the correct recipe rather than silently overriding it.

Delimiter defaults to comma; allow explicit comma, semicolon, tab, or pipe. There is no delimiter/encoding/header sniffing or retry-with-another-dialect behavior. The dialect uses double-quoted fields, doubled interior quotes, LF/CRLF/CR record endings, no backslash-escape extension, no comment syntax, no whitespace trimming, and no text after a closing quote except a delimiter/record terminator. Quoted fields may contain delimiters/newlines; CRLF/CR normalize to LF consistently with plain text, including within quoted values. Physical line counts and logical record counts are separately bounded. Reject unclosed quotes, quotes inside unquoted fields, inconsistent nonblank row widths, unsupported controls/signatures, and oversized content.

Add explicit adapter recipe controls to single ingestion and manifests:

```bash
newsrag ingest ./expenses.csv --csv-delimiter semicolon --csv-header absent --csv-encoding utf-8
```

```yaml
documents:
  - source: ./expenses.csv
    type: csv
    csv:
      delimiter: semicolon
      header: absent
      encoding: utf-8
  - source: ./budget.xlsx
    type: xlsx
    xlsx:
      header_rows:
        FY 2026: 4
```

Delimiter enum values are `comma|semicolon|tab|pipe`, header values are `present|absent`, and encoding defaults to `auto` (strict BOM/declaration/default rules, not detection). XLSX single-file ingestion accepts `--xlsx-header-rows` as a JSON object mapping exact sheet names to positive row numbers. No override means native-table declarations or coordinate labels. Reject unknown option keys, invalid value types, cross-format options, and ambiguous option combinations before enqueueing; existence/geometry/header-content validation occurs in the worker. Options select the corresponding adapter when no explicit type is given; conflicting explicit type fails. Reject CSV/XLSX recipe flags on a directory scan rather than applying one interpretation indiscriminately; use a manifest for per-file recipes.

Parsing every supported encoding/dialect/header combination follows the same immutable-byte identity policy. Re-ingesting identical bytes with different options is still `duplicate_ignored`; it does not silently reinterpret a published document. Intentional interpretation changes use explicit reprocessing, described below.

## XLSX workbook and archive safety

Register `xlsx`, canonical `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet`, and case-insensitive `.xlsx`. Explicit/filename-selected generic ZIP/octet-stream transport is allowed only after workbook validation. ZIP magic is not a workbook signature. Reject `.xlsm`, `.xlsb`, legacy `.xls`, templates, unrelated Office formats, encrypted packages, and disguised macro/active content even when renamed `.xlsx`.

Support the transitional SpreadsheetML worksheet profile. Validate required content types, root office-document relationship, one workbook, workbook-declared sheet order/IDs/names/targets, worksheet structures, shared strings, styles, native tables, merge definitions, formula groups/caches, and all referenced IDs/parts. Check coordinate uniqueness/order and native Excel limits (1,048,576 rows, 16,384 columns) independently of the much smaller accepted geometry budgets. Invalid counts/indices/dimensions and unknown required features fail; do not recover malformed workbooks or trust producer dimension/count hints as allocation sizes.

Generalize/reuse the hardened OPC ZIP/XML mechanisms established for DOCX with format-specific allowlists; do not weaken DOCX validation or treat every Office ZIP as the same document type. Enforce safe member names/aliases, CRCs, bounded reads/expansion, no filesystem extraction, no symlinks/encryption/unsupported compression, validated content associations/relationships, and hardened XML without DTD/entities/network/recovery. Validate all package parts, including hidden/unindexed parts. Allow inert recognized properties, themes, styles, and bounded custom metadata; unknown or active package features fail explicitly.

External hyperlink targets are ignored, retaining stored visible cell text. Reject every other external relationship, external-link parts, connections/queries, macros, ActiveX, OLE/embedded objects, and active data sources. Never follow hyperlinks, calculate formulas, retrieve external workbooks, or update caches. Charts, drawings, images, pivot tables/caches, and other unsupported features are rejected in the initial workbook profile rather than partially indexing an incompletely validated package. No worksheet/image OCR or binary-workbook decoding is added.

## Resource budgets

All limits are fail-closed, not truncation targets. Counts include hidden content. Shared strings, metadata, formula text, and repeated context must be budgeted even when they do not become searchable values.

| Resource | CSV | XLSX |
| --- | --- | --- |
| Raw acquired bytes | 10 MiB | 25 MiB |
| Decoded input / expanded ZIP | 10,485,760 decoded characters; 100,000 physical lines | 100 MiB total expanded bytes |
| ZIP parts / one part / ratio | Not applicable | 2,000 parts; 20 MiB per part; 200:1 maximum per part; stored/deflated only |
| XML elements / depth | Not applicable | 500,000 cumulative elements; 128 levels |
| Sheets / table planes | 1 / 1 | 32 / 32, including hidden/empty sheets |
| Native table regions / merge regions | Not applicable | 128 native regions; 10,000 nonoverlapping merges per workbook |
| Rows | 100,000 logical records including header/blanks | 100,000-row span per sheet; 200,000 row units across workbook |
| Columns | 256 | 256-column span per sheet, at original worksheet coordinates |
| Rectangular cell positions | 1,000,000 | 1,000,000 across workbook, including blank/hidden/covered positions |
| Individual decoded/stored cell or formula text | 8,192 characters | 8,192 characters per value/expression |
| Canonical value/formula characters | 10,485,760 total | 10,485,760 total, including unreferenced shared-string entries |
| Descriptors/cell records/labels serialized output | 64 MiB UTF-8 | 64 MiB UTF-8 |
| Focus stripe | At most 64 cells in one row | Same |
| Focus / context / full passage | 16,384 / 16,384 / 32,768 serialized characters | Same |
| Total generated passages / index text | 100,000 passages; 64 MiB UTF-8 combined focus/context text | Same |
| Evidence regions per passage | One focus plus at most eight context regions | Same |
| Per-item packet/evidence materialization | 256 cells and 32,768 rendered characters including context | Same |

Individual cell limits are checked before serialization; if escaped serialization still cannot fit the focus budget, fail instead of splitting the value. Build stripes and contexts deterministically and check aggregate output budgets before publication. If optional context cannot fit, omit it with an explicit reason; focus data must never be silently shortened. The packet/evidence cap applies to explicit whole-table/column regions too: return a clear too-large-region error or require a narrower selection, never emit a misleading citation for a truncated excerpt. Existing search-result count limits continue to apply.

## Shared storage/API integration and lifecycle

The existing `CanonicalSourceUnit`/`AdapterResult`, generic per-unit character chunker, row endpoint storage, and passage-substring quote validator are insufficient by themselves for rectangular cell evidence with noncontiguous context. CSV implementation must add the common descriptor/cell/selector plumbing rather than hiding it in XLSX or pretending `page_start` is a column selector.

Persist table descriptors/cells, focus selectors, and context-role references with document and processing-generation ownership and uniqueness constraints. Source units remain the row anchors; precise cells and context references are additional typed provenance. Add optional typed region/context fields to chunk/passage/search/discovery/enrichment/packet contracts and persisted evidence, with explicit validation and a versioned tabular renderer. Existing scalar extent APIs may retain a primary row/cell count plus descriptor details; they must not coerce worksheets into pages. The CSV task owns a lossless schema migration with legacy tabular fields absent/null, stable old IDs/text/citations, idempotency, and rejection of inconsistent ownership. Do not assign fabricated table regions to legacy evidence.

Publication of descriptors, cells, row units, selectors, passages, indexes, and the generation/revision pointer follows the existing atomic visibility/compensation policy. No partial sheet or table is published independently. Failure on any sheet fails the workbook candidate while preserving the previous current revision/generation. Retain exact-byte first-successful-import semantics, staged ordinary changes, refresh no-ops/new revisions/reactivation, and source-type changes under validated fresh evidence. Known CSV/XLSX sources retain acquisition limits and type evidence for generic refreshes; CSV over `text/plain` retains its accepted format without discarding a new artifact's declared charset.

Adapter recipes and effective header/dialect/encoding settings participate in processing fingerprints with parser/model/index versions. Store both the requested policy and the observed per-artifact decoding/header resolution. Refresh inherits the captured current processing generation's requested recipe, not stale initial-ingestion options; new bytes obtain their own validated observed declarations. A saved refresh candidate retains its recipe, base revision, bytes, and observed type/charset across retries.

Extend explicit reprocessing with the same CSV recipe options and XLSX header-map option. An omitted option inherits the active generation's recipe; an explicit option replaces that setting (an empty XLSX map clears overrides). Batch validation rejects options incompatible with any selected document before enqueuing any jobs; generic reprocessing without overrides still supports mixed documents. Reprocessing never fetches the original file/URL, changes artifact/revision identity, or rewrites historical selectors. Same fingerprint remains an integrity-verified no-op; changed recipes rebuild all derived tabular data as a new generation. Retain saved-configuration conflict handling, active-generation CAS, reader snapshots, committed receipts, and attempted-ID-scoped vector compensation.

## Worked examples and required tests

### CSV records versus lines

For the following bytes, quoted content creates an embedded physical newline but only one logical record:

```csv
Department,Amount,Note
Roads,001200,"Paving
phase two"
Parks,,"=HYPERLINK(""https://example.invalid"",""link"")"
```

The header is row 1; Roads is row 2; Parks is row 3. B2 is the literal string `001200`, not inferred numeric 1200. B3 is an empty string, not zero. C3 is literal text and is never evaluated. A hit on `phase two` focuses on a stripe in row 2, identifies the quoted C2 value with its escaped newline, and may include row-1 headers and row-3 context with separate references. It never cites the physical line containing `phase two` as CSV row 3.

### XLSX coordinates, caches, and visibility

Suppose worksheet 2 is named `FY 2026`, with a declared header on row 4, B5=`Roads`, C5 a numeric cache `1200` for a formula, D5 an unavailable formula cache, and hidden row 6 containing another stored amount. A C5 result cites `sheet 2 “FY 2026” — cell C5`, identifies its stored cache as unverified, and separately cites C4 if using its header. It does not calculate D5, pull a result from a formula group anchor, expose row 6, or jump over hidden row 6 to add row 7 as a neighbor. A merge B8:C8 with value only at B8 is cited through B8 plus its merge annotation; C8 is not another occurrence of that value.

Implementation acceptance evidence must cover:

- Encoding/BOM/declaration agreement, explicit dialects, quoted multiline fields, malformed quotes, duplicate/blank headers, no-header input, repeated headers, literal formula-looking CSV strings, blank records, and ragged widths.
- Sparse geometry/dimension attacks, shared/inline strings, exact numeric/boolean/date/error types, multiple/empty/hidden sheets, hidden rows/columns, native table regions, merge coverage/conflicts, normal/shared/array formulas, missing/stale/error/empty caches, and date-system metadata without calculation.
- Focus/header/neighbor separation, long-row stripes, cell boundaries, multiple header regions, hidden gaps, merged-anchor citations, exact selector/quote validation, context-only keyword hits, cross-sheet/generation rejection, and no invented arithmetic or provenance.
- Raw/expanded/count/depth/character/metadata/index/output limits, malformed ZIP/XML, aliases/CRC failures, macros/external data, all-or-nothing workbook failure, and safe output escaping.
- Mixed-source filtering/retrieval/discovery/packets, compatible vectors and keyword fallback, unchanged existing citations, fresh/legacy schema migration, and all duplicate/refresh/reprocessing/retry/conflict/failure invariants with preserved old artifacts and packets.
- Formatting, Ruff, mypy, pytest, `make check`, pre-PR checks, CI, independent review, and disposable CLI/daemon smoke tests during implementation; installed corpus/services untouched.

## Existing implementation tasks and approval record

| Existing task | Approved scope | Dependencies |
| --- | --- | --- |
| `task-58d3cf54` — Add CSV source adapter | Shared tabular descriptors/cells/selectors, lossless migration, tabular chunking/evidence validation, CSV parser/options, lifecycle recipe controls, mixed-source presentation, tests/docs. No XLSX parsing. | `task-c089c26c` |
| `task-2f7058f3` — Add XLSX source adapter | Bounded workbook validation/extraction, worksheet/native-region metadata, values/formulas/caches/visibility/merges, XLSX header options, and XLSX lifecycle/safety/mixed-source tests using the shared CSV foundation. | `task-c089c26c`, `task-58d3cf54` |

The consolidated design was explicitly approved by the human after reviewing the draft and decision summary. Existing tasks `task-58d3cf54` and `task-2f7058f3` were updated in place with the approved requirements and acceptance criteria; their IDs and dependencies were retained. No backend tasks were created and no ingestion implementation was performed. Design approval does not waive documentation PR/CI/independent review or the separate human merge/closure gates.
