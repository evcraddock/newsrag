#!/usr/bin/env bash
set -euo pipefail

require_command() {
    if ! command -v "$1" >/dev/null 2>&1; then
        echo "Missing required command: $1"
        echo "See docs/development.md for installation steps."
        exit 1
    fi
}

require_command uv

echo "Running pre-PR checks..."

echo "→ Verifying lockfile and virtualenv..."
uv sync --locked --dev

echo "→ Formatting..."
uv run ruff format --check .

echo "→ Linting..."
uv run ruff check .

echo "→ Type checking..."
uv run mypy .

echo "→ Running tests..."
uv run pytest

echo "✓ All checks passed!"
