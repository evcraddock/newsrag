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

Ollama is optional and is not installed with NewsRAG. To use the default local embedding model:

```bash
brew install ollama
brew services start ollama
ollama pull nomic-embed-text
mkdir -p ~/.config/newsrag
cat > ~/.config/newsrag/config.yaml <<'YAML'
embedding:
  provider: ollama
  base_url: http://127.0.0.1:11434
  model: nomic-embed-text
YAML
```

A hosted OpenAI-compatible embedding provider can be configured instead through `~/.config/newsrag/config.yaml`.

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

## Development prerequisites

NewsRAG uses a local-first stack:

- SQLite with FTS5 for metadata and keyword search
- LanceDB for vector search
- OCRmyPDF, Tesseract, Ghostscript, and qpdf for OCR normalization
- Ollama with `nomic-embed-text` for optional local embeddings
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

This starts all processes defined in `Procfile.dev`, including the foreground `newsrag daemon run` process managed by Overmind.

### View and manage the development environment

```bash
make dev-logs
make dev-tail
make dev-connect
make dev-status
make dev-stop
```

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
