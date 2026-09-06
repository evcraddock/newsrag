# XLSX sources

NewsRAG ingests a bounded, transitional SpreadsheetML worksheet profile without opening Excel or evaluating formulas. A workbook remains one artifact/document; its worksheets become generation-owned table planes in workbook order, reusing the [CSV tabular foundation](csv.md) and [approved citation contract](research/tabular-source-ingestion-and-citations.md).

## Ingest and search

```bash
newsrag ingest ./budget.xlsx
newsrag ingest https://example.gov/budget.xlsx
newsrag ingest https://example.gov/export --type xlsx
newsrag ingest ./budget.xlsx --xlsx-header-rows '{"FY 2026":4}'
newsrag documents list --source-type xlsx
newsrag search "Roads" --source-type xlsx
newsrag packet "Roads" --source-type xlsx --out packets/roads.md
```

Local `.xlsx` extensions are case-insensitive. Public HTTP(S) acquisition accepts the canonical `application/vnd.openxmlformats-officedocument.spreadsheetml.sheet` media type. Generic ZIP/octet-stream transport needs explicit XLSX selection or filename evidence and must pass complete workbook validation; ZIP magic alone never selects XLSX. Contradictory Office filenames or media types fail even with an explicit hint. The compatibility command `ingest-url` accepts the same arguments as `ingest`.

Mixed recursive directory scans include XLSX alongside existing formats. Recipe flags cannot apply to a directory scan: use a manifest for per-file choices. XLSX options select the adapter when no explicit type is supplied; conflicting PDF/CSV/type options, invalid JSON, duplicate keys, unknown recipe keys, and invalid row-number types fail before jobs are inserted.

```yaml
documents:
  - source: ./budget.xlsx
    type: xlsx
    xlsx:
      header_rows:
        FY 2026: 4
  - source: ./expenses.csv
    csv:
      header: present
```

Run `newsrag ingest-manifest ./sources.yaml`. All entries/options validate before enqueueing. Worksheet existence and header geometry/visibility validate against the saved workbook in the worker.

## Headers, coordinates, and types

Without an override, native Excel table header declarations provide header context only inside their declared regions; other cells use coordinate labels. `--xlsx-header-rows` is a JSON object mapping **exact** sheet names to positive row numbers. An override applies across its worksheet and takes precedence over native headers, without discarding native table metadata. Header rows must be visible and inside the occupied extent. Empty or hidden header cells do not supply invented labels. No heuristic header detection, repeated-header removal, multirow-header inference, or merged-label filling occurs.

Worksheet names/IDs, order, visibility, native table regions/columns/totals, date system, styles, and original cell positions remain provenance. Native tables annotate the worksheet plane rather than duplicating cells or documents. Occupied geometry derives from stored values/formulas, formula ranges, native table ranges, and merges—not producer `dimension` hints, formatting-only cells, or empty row declarations. Offsets such as B4 remain B4; blank positions and hidden gaps are not renumbered. Empty sheets retain descriptors without row units.

- Strings remain literal, including whitespace, leading zeros, empty strings, rich-text runs, and validated OOXML string escapes.
- Absent cells, explicitly empty cells, empty-string values, merged-covered positions, and unavailable formula caches remain distinct.
- Numbers preserve their original numeric lexemes and exact decimal representations, not binary-float approximations. Booleans and stored ISO date/date-time values retain their types. Errors remain errors, not numeric facts.
- Styles and the 1900/1904 date system are retained, but serial dates are not converted and display formatting is not emulated. No currency, units, civic dates, totals, joins, or arithmetic are inferred.

## Formulas, merges, and hidden content

Normal, shared, and array formula expressions/groups are preserved separately from each cell's cache. Shared followers are not translated into invented expressions; array/shared followers never inherit another cell's cached value. Missing caches remain unavailable. Cache presence comes from each cell's stored value element, not from its normalized blank/value kind; present empty numeric caches fail for normal, shared, and array formulas, while explicitly cached empty strings remain distinguishable. Valid stored caches can supply evidence, always qualified **`formula-cache; freshness not verified`**. Error caches and empty/unavailable values do not independently create matches. Expressions remain canonical provenance, not ordinary value-index or prompt text.

Merged rectangles retain one top-left anchor. Covered cells refer to that anchor without repeating its value. Anchor evidence discloses its exact merged rectangle; requesting only a covered cell cannot manufacture an anchor quote. Overlapping merges and conflicting covered values/formulas fail.

Hidden/very-hidden sheets and hidden rows/columns remain in canonical storage and the preserved original artifact, but their cell values/formulas are excluded from normalized row evidence, FTS/vector inputs, header/neighbor context, facts, enrichment, briefs, and packets. Direct new evidence requests containing hidden cells fail. Inventory and evidence disclose exclusion counts without values. This is a retrieval policy, not encryption or access control; there is no include-hidden override.

A workbook containing no visible searchable data values fails explicitly. Headers alone, empty cells/strings, errors, and unavailable caches are insufficient.

## Exact evidence and output

Search passages focus on one visible row and contiguous column stripes of at most 64 complete cells. Stripes split at hidden gaps and native/header boundaries. Applicable headers and immediate eligible neighboring rows have separate selectors; context never jumps over hidden/blank rows or crosses native-table boundaries. Optional context is omitted as a whole with a reason if budgets are exceeded, never truncated into misleading evidence.

Citations include the sheet index/name and original coordinates, for example `sheet 2 "FY 2026" — cells C8:E10`. Focus and context must belong to the same document/table/processing generation, and row-unit endpoints must match the exact persisted cells. Header-only keyword matches are distinguished from focus matches; neighbor-only matches cannot impersonate focus values. Formula/merge qualifications accompany typed evidence, including literal cell quotations.

Packets use escaped, inert extractive representations with explicit selectors and source/revision/artifact/generation provenance. Hidden values, formula expressions, active HTML/links/images, and executable spreadsheet exports are excluded. Saved retrieval snapshots remain authoritative even when refresh or reprocessing publishes concurrently. Non-tabular citations remain unchanged.

## Package safety and limits

Every package part, including hidden or unindexed content, is validated using shared hardened OPC ZIP/XML machinery with an XLSX-specific allowlist. Accepted workbook, worksheet, shared-string, style, and native-table structures also use closed child/attribute rules, including nested inert display metadata; unknown descendants, malformed leaves, invalid metadata value types/enums, missing required fields, duplicate singleton children, invalid child ordering, and exceeded repetition limits are rejected rather than ignored. The accepted-subset rules in `newsrag/xlsx_structure_rules.py` record ECMA-376 transitional structural facts with a pinned reference; no schemas are downloaded or evaluated at runtime. DOCX retains its independent policy and limits. Validation checks aliases/traversal, symlinks, encryption/compression, actual expansion and CRCs, content associations, roots, relationship ownership/references/reachability, and strict XML without DTD/entities/network access or recovery. No ZIP member is extracted to a filesystem path.

External worksheet hyperlink targets are ignored; stored visible text remains. All other external relationships, external-link parts, macros, ActiveX, embedded objects, connections/queries, and active data sources fail. The initial profile also rejects charts/drawings/images/pivots, comments, calculation-chain parts, extension/alternate-content features, default-hidden rows declared with `sheetFormatPr zeroHeight` (explicit hidden-row flags are supported), non-worksheet sheets, strict OOXML, and unknown package features rather than partially indexing them. Macro-enabled/binary/legacy/template formats (`.xlsm`, `.xlsb`, `.xls`, `.xltx`, etc.) are not supported. There is no Office runtime, formula execution, external retrieval, spreadsheet image OCR, or malformed-workbook recovery.

| Resource | Limit |
| --- | --- |
| Raw workbook | 25 MiB |
| Total expanded ZIP / individual part | 100 MiB / 20 MiB |
| ZIP members / compression ratio | 2,000 / 200:1 per part; stored or deflated only |
| Cumulative XML elements / depth | 500,000 / 128 |
| Sheets, including empty/hidden | 32 |
| Native table regions / merges | 128 / 10,000 per workbook |
| Worksheet extent | 100,000-row span × 256-column span, at original coordinates |
| Total row units / rectangular positions | 200,000 / 1,000,000, including hidden/blank/covered positions |
| Individual stored value/formula | 8,192 characters |
| Aggregate value/formula text | 10,485,760 characters, including unreferenced shared strings |
| Serialized descriptors/cells/row metadata | 64 MiB UTF-8 |
| Focus / context / complete passage | 16,384 / 16,384 / 32,768 characters |
| Generated passages / aggregate index text | 100,000 / 64 MiB UTF-8 |
| Per-item evidence, including context | 256 cells / 32,768 characters |

Native Excel coordinate limits (1,048,576 rows and 16,384 columns) validate independently. Sparse far-apart cells cannot bypass the smaller rectangular budgets. Oversized focus or whole-table/column evidence requests fail with guidance to select a smaller region; no silent truncation occurs.

## Refresh and reprocessing

```bash
newsrag reprocess <xlsx-document-id> --xlsx-header-rows '{"FY 2026":4}'
newsrag reprocess <xlsx-document-id> --xlsx-header-rows '{}'
newsrag documents generations <xlsx-document-id>
newsrag refresh <source-id>
```

Re-ingesting identical bytes with different options is still a duplicate, not an interpretation change. Use explicit reprocessing to replace a worksheet-header map; `{}` clears overrides, while an omitted option inherits the current generation's recipe. Mixed reprocessing batches reject incompatible overrides atomically. Reprocessing uses verified saved bytes, even if the original file is missing, and never creates a new artifact/source revision or rewrites old anchors/packets.

Refresh captures the active generation's requested recipe, validates each new workbook's observed interpretation, and preserves saved candidate bytes/options across retries. Unchanged bytes are a no-op; historical bytes can reactivate their existing revision. Any worksheet failure fails the entire workbook candidate. Failed publication leaves old searchable generations/revisions intact with attempt-scoped vector compensation. See [Reprocessing](reprocessing.md) for fingerprints, configuration conflicts, snapshots, and retry semantics.

XLSX reuses schema 8; no additional schema migration is introduced. The shared tabular renderer is version 2. Existing generations and packets are retained; updated rendering takes effect for newly processed generations, not through automatic corpus rewriting. Restart workers on the same version when upgrading; do not mix old/new binaries on one corpus.

## Disposable smoke validation

```bash
uv run python scripts/smoke_xlsx.py
```

With the existing Overmind/tmux development prerequisites, this runs the real CLI and daemon via `make dev` in a temporary workspace with an isolated socket/corpus/configuration and localhost mock embedding API. It checks hidden-value/formula-expression exclusion, typed citations and cache qualifications, directory/manifest input, duplicates, saved-byte reprocessing, refresh recipe inheritance, and atomic invalid inputs. It stops only its own services; installed corpora/services are not used.
