# newsrag

Local-first CLI evidence retrieval tool for city hall PDFs with OCR, hybrid search, and cited Markdown source packets.

## Development prerequisites

NewsRAG uses a local-first stack:
- SQLite with FTS5 for metadata and keyword search
- LanceDB for vector search
- OCRmyPDF, Tesseract, Ghostscript, and qpdf for OCR normalization
- Ollama with `nomic-embed-text` for local embeddings
- Overmind for `make dev`

The default development path does not require Docker or `compose.yaml` because SQLite and LanceDB are embedded/local.

For the full macOS setup, installation commands, and validation steps, see [docs/development.md](docs/development.md).

## Installation

```bash
uv sync --dev
```

## CLI quick start

```bash
uv run newsrag --help
uv run newsrag doctor
uv run newsrag status --initialize
```

## How to work on this project

### Start the dev environment

```bash
make dev
```

This starts all processes defined in `Procfile.dev`, including the foreground `newsrag daemon run` process managed by Overmind.

### View logs

```bash
# Stream all logs (Ctrl+C to stop)
make dev-logs

# Quick peek at recent logs
make dev-tail

# Attach to one service terminal (default: SERVICE=newsrag)
make dev-connect
```

### Check status

```bash
make dev-status
```

### Stop the dev environment

```bash
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

## Environment configuration

Copy `.env.example` to `.env` and adjust values as needed for your local corpus path, config path, and embedding provider settings.

## License

MIT
