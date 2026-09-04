# Development setup

NewsRAG uses a local-first development stack. SQLite and LanceDB are embedded dependencies, so the default development path does not require Docker or `compose.yaml`.

## macOS prerequisites

Install the local tools NewsRAG expects on a fresh machine:

```bash
brew install uv python@3.11 overmind tmux sqlite ocrmypdf tesseract ghostscript qpdf
```

Notes:
- Overmind uses tmux under the hood, so install both.
- SQLite must include FTS5 support. The validation command below checks the Python runtime that NewsRAG will use.
- The test suite mocks embedding requests, so development checks do not require a running embedding service.

For manual ingestion and vector-search testing, configure any OpenAI-compatible embedding service. See [Embedding configuration](embeddings.md) for llama.cpp, LM Studio, Ollama, and hosted OpenAI examples.

## Validate local prerequisites

Run these commands after installation:

```bash
uv --version
python3 --version
overmind -v
sqlite3 --version
ocrmypdf --version
tesseract --version
gs --version
qpdf --version
```

Validate SQLite FTS5 support through Python:

```bash
python3 - <<'PY'
import sqlite3

print(f"sqlite_version={sqlite3.sqlite_version}")
with sqlite3.connect(":memory:") as connection:
    connection.execute("create virtual table docs using fts5(content)")
print("fts5=enabled")
PY
```

## Bootstrap the repository

```bash
uv sync --dev
cp .env.example .env
make check
```

Development commands run through `uv run`. An end-user Homebrew installation instead exposes `newsrag` directly and does not require a repository checkout; see the installation section in the [README](../README.md).

## Development workflow

Start the development environment:

```bash
make dev
```

The development daemon uses the repository's gitignored `.newsrag` directory by default instead of the installed corpus under `~/.local/share/newsrag`. Override the development location through the environment when needed:

```bash
NEWSRAG_DATA_DIR=/path/to/dev-corpus make dev
```

Use the same data directory explicitly for development CLI commands run outside Overmind:

```bash
uv run newsrag --data-dir "${NEWSRAG_DATA_DIR:-.newsrag}" status
uv run newsrag --data-dir "${NEWSRAG_DATA_DIR:-.newsrag}" jobs list
```

Attach to one service terminal:

```bash
make dev-connect
# or choose a different process name
make dev-connect SERVICE=newsrag
```

Check status:

```bash
make dev-status
```

Stop the development environment:

```bash
make dev-stop
```

Current behavior:
- `Procfile.dev` is present and wired into `make dev`.
- The long-running process is `uv run newsrag --data-dir "${NEWSRAG_DATA_DIR:-.newsrag}" daemon run` managed by Overmind.
- Development runtime data stays under `.newsrag` unless `NEWSRAG_DATA_DIR` overrides it.
- Installed commands continue to use the configured data directory or `${XDG_DATA_HOME:-~/.local/share}/newsrag`.

## Verification commands

Use these commands before opening a PR:

```bash
make check
./scripts/pre-pr.sh
```

`make check` runs formatting checks, linting, type checking, and tests through `uv`.

## Environment variables

See `.env.example` for development process variables. Installed CLI configuration is YAML at `~/.config/newsrag/config.yaml`; `.env` is not the installed application's configuration file.
