# newsrag

Local-first CLI evidence retrieval tool for city hall PDFs with OCR, hybrid search, and cited Markdown source packets.

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
