# Development setup

NewsRAG uses a local-first development stack. SQLite and LanceDB are embedded dependencies, so the default development path does not require Docker or `compose.yaml`.

## macOS prerequisites

Install the local tools NewsRAG expects on a fresh machine:

```bash
brew install uv python@3.11 overmind tmux sqlite ocrmypdf tesseract ghostscript qpdf
brew install --cask ollama
```

Notes:
- Overmind uses tmux under the hood, so install both.
- SQLite must include FTS5 support. The validation command below checks the Python runtime that NewsRAG will use.
- Ollama must be running before local embedding checks will pass.

Pull the default local embedding model:

```bash
ollama pull nomic-embed-text
```

If Ollama is not already running, start it with one of:

```bash
brew services start ollama
# or
ollama serve
```

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
ollama --version
ollama list | grep nomic-embed-text
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

## Development workflow

Start the development environment:

```bash
make dev
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
- The long-running process is a placeholder until `newsrag daemon run` is implemented.
- When the daemon entrypoint lands, replace `./scripts/dev-placeholder.sh` in `Procfile.dev` with the real command.

## Verification commands

Use these commands before opening a PR:

```bash
make check
./scripts/pre-pr.sh
```

`make check` runs formatting checks, linting, type checking, and tests through `uv`.

## Environment variables

See `.env.example` for the local defaults and optional overrides used by the planned local stack.
