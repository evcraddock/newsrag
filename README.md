# newsrag

Local-first CLI evidence retrieval for city hall PDFs, static HTML, plain text, Markdown, DOCX, CSV, and XLSX, with OCR, hybrid search, and cited Markdown source packets.

## Installation

NewsRAG's supported macOS installation uses Homebrew on Apple Silicon. The formula installs NewsRAG together with SQLite, OCRmyPDF, Tesseract, Ghostscript, and qpdf:

```bash
brew install evcraddock/tap/newsrag
```

The curl installer is a convenience wrapper around the same Homebrew formula:

```bash
curl -fsSL https://raw.githubusercontent.com/evcraddock/newsrag/main/scripts/install.sh | bash
```

NewsRAG installs as a standalone `newsrag` command. Installed usage does not require `uv` or a repository checkout.

### Configure embeddings

NewsRAG uses one OpenAI-compatible embedding API for local and hosted services. An embedding provider must be configured explicitly before ingestion or vector search.

For a local llama.cpp server exposing Nomic Embed Text v1.5:

```bash
mkdir -p ~/.config/newsrag
cat > ~/.config/newsrag/config.yaml <<'YAML'
embedding:
  provider: openai_compatible
  base_url: http://127.0.0.1:8080/v1
  model: nomic-embed-text-v1.5
YAML
```

NewsRAG also supports LM Studio, Ollama's `/v1` API, OpenAI, and other compatible services through the same provider. See [Embedding configuration](docs/embeddings.md) for server examples, hosted authentication, and migration instructions.

### Verify and initialize

```bash
newsrag --version
newsrag doctor
newsrag status --initialize
```

### Update or uninstall

```bash
brew update
brew upgrade newsrag

# Remove NewsRAG while retaining corpus data under ~/.local/share/newsrag
brew uninstall newsrag
```

## CLI quick start

```bash
newsrag --help
newsrag doctor
newsrag status --initialize
```

Ingest plain-text sources with stable line citations:

```bash
newsrag ingest ./meeting-notes.txt --title "Meeting notes"
newsrag ingest https://example.gov/meeting-notes.txt
newsrag documents list --source-type text
newsrag search "road budget" --source-type text
```

Text also works in mixed directories, manifests, discovery, packets, refresh, and reprocessing. See [Plain-text sources](docs/plain-text.md) for supported encodings, line numbering, and safety limits.

Ingest Markdown with preserved structure and heading/line-range citations:

```bash
newsrag ingest ./meeting-notes.md
newsrag ingest https://example.gov/export --type markdown
newsrag search "road budget" --source-type markdown
newsrag packet "road budget" --source-type markdown --out packet.md
```

The [Markdown adapter](docs/markdown.md) shares text decoding and limits, retains headings, lists, quotes, tables, and code blocks, and never renders HTML or fetches linked resources.

Ingest DOCX with heading-aware paragraph, table, and footnote citations:

```bash
newsrag ingest ./meeting-notes.docx
newsrag ingest https://example.gov/export --type docx
newsrag search "road budget" --source-type docx
```

The [DOCX adapter](docs/docx.md) validates bounded ZIP/XML packages without an Office runtime. It never executes macros, embedded objects, or fields, never retrieves external resources, and excludes embedded-image OCR.

Ingest CSV with exact cell coordinates and separately attributed header/neighbor context:

```bash
newsrag ingest ./expenses.csv
newsrag ingest ./expenses.csv --csv-delimiter semicolon --csv-header absent
newsrag search "Roads" --source-type csv
newsrag reprocess <csv-document-id> --csv-header absent
```

The [CSV adapter](docs/csv.md) preserves literal strings, leading zeros, whitespace, and logical records. It supports strict encoding/dialect/header recipes, mixed directories and manifests, and generation-retaining refresh/reprocessing. It never calculates formulas or infers arithmetic, units, currency, or civic dates.

Ingest XLSX with original worksheet/cell coordinates and qualified stored formula caches:

```bash
newsrag ingest ./budget.xlsx --xlsx-header-rows '{"FY 2026":4}'
newsrag search "Roads" --source-type xlsx
newsrag packet "Roads" --source-type xlsx --out packets/roads.md
newsrag reprocess <xlsx-document-id> --xlsx-header-rows '{}'
```

The [XLSX adapter](docs/xlsx.md) reuses the shared tabular model with bounded ZIP/XML validation, native headers, exact stored types, merges, and workbook order. Hidden cells remain canonical but never enter new evidence. Formulas are never evaluated; caches are marked as unverified. Unsupported or active workbook features fail atomically.

Search indexed evidence using plain text, including hyphenated road names:

```bash
newsrag search "Clear Springs Road SH-152"
```

Keyword retrieval treats whitespace-separated terms as literal text and requires all terms to match, while vector retrieval uses the original query. Hyphens and quotes are handled safely; Boolean operators, column filters, wildcards, and quoted-phrase syntax are not interpreted as FTS commands.

Refresh a known source while keeping its historical evidence:

```bash
newsrag documents show <document-id>       # Includes the source ID
newsrag refresh <source-id>                # Enqueues one background refresh
newsrag jobs list
newsrag documents versions <document-id>
newsrag search "budget" --include-history
newsrag packet "budget" --include-history --out packets/budget-history.md
```

Refresh rereads the registered file or public URL and compares complete content hashes. New bytes become the current local searchable revision only after successful processing. Search, new packets, and corpus discovery use current revisions by default; `--include-history` includes previous published revisions. Inventory and direct document lookup retain historical records. Nothing is uploaded or shared by publication.

Unchanged bytes do not reindex or overwrite metadata. If the source returns its own historical bytes, that revision is reactivated without reindexing. Bytes already published for another source are reported as a duplicate and leave the requested source unchanged. `newsrag jobs retry <job-id>` retries a failed refresh's saved candidate once acquired; use a new refresh to check the live source again. Historical citations, metadata, and existing packet files are preserved. Refresh is manual and single-source only; metadata editing, bulk refresh, schedules, and reprocessing are separate concerns.

Rebuild derived evidence from saved artifacts without refetching or creating source revisions:

```bash
newsrag reprocess <document-id>                       # Up to 20 explicit IDs
newsrag reprocess <pdf-document-id> --pdf-extractor pdfplumber
newsrag documents generations <document-id>
newsrag jobs list
newsrag jobs retry <failed-job-id>
```

Reprocessing retains old citation anchors and stages a complete replacement processing generation before making it active. Unchanged processing fingerprints produce a verified no-op. Failed runs leave existing searchable evidence intact; retries retain their saved configuration and base generation. See [Reprocessing](docs/reprocessing.md) for batch limits, compatible embedding models, and recovery behavior.

Stop all old daemons before upgrading a corpus to schema 8, then restart them using the new version. Migration preserves existing document and citation IDs, source history, and derived evidence. Legacy descriptive metadata is inherited conservatively; unknown legacy processing configurations are marked explicitly rather than invented.

List documents ingested during an inclusive UTC calendar-date range:

```bash
newsrag documents list --ingested-since 2026-09-01 --ingested-until 2026-09-02
```

The existing `--since` and `--until` document filters continue to apply to meeting dates.

## Development prerequisites

NewsRAG uses a local-first stack:

- SQLite with FTS5 for metadata and keyword search
- LanceDB for vector search
- OCRmyPDF, Tesseract, Ghostscript, and qpdf for OCR normalization
- Any OpenAI-compatible `/v1/embeddings` service for local or hosted embeddings
- Overmind for `make dev`

The default development path does not require Docker because SQLite and LanceDB are embedded. For the full macOS development setup and validation steps, see [docs/development.md](docs/development.md).

## Development from a checkout

Development commands use the repository's uv environment and are intentionally different from installed CLI commands:

```bash
uv sync --dev
uv run newsrag --help
```

### Start the development environment

```bash
make dev
```

This starts all processes defined in `Procfile.dev`, including the foreground `newsrag daemon run` process managed by Overmind. The development daemon stores its corpus under the repository's gitignored `.newsrag` directory by default; set `NEWSRAG_DATA_DIR` to override it.

Target the same development corpus from another terminal by passing the development data directory explicitly:

```bash
uv run newsrag --data-dir "${NEWSRAG_DATA_DIR:-.newsrag}" status
uv run newsrag --data-dir "${NEWSRAG_DATA_DIR:-.newsrag}" jobs list
```

### View and manage the development environment

```bash
make dev-logs
make dev-tail
make dev-connect
make dev-status
make dev-stop
```

`make dev-logs` streams daemon startup, job lifecycle, ingestion stage, completion, and failure messages. Set `NEWSRAG_LOG_LEVEL` in `.env` to `DEBUG`, `INFO`, `WARNING`, `ERROR`, or `CRITICAL`; the default is `INFO`.

### Run verification

```bash
make check
```

### Before opening a PR

```bash
make pre-pr
```

### Available Make commands

```bash
make help
```

## Configuration

Installed NewsRAG reads user configuration from `~/.config/newsrag/config.yaml` by default and stores corpus data under `${XDG_DATA_HOME:-~/.local/share}/newsrag`. Use `--config-path` or `--data-dir` for command-specific overrides.

The repository's `.env.example` is only for development process configuration; it is not the installed CLI configuration file.

## License

MIT
