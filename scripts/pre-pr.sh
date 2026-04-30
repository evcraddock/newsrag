#!/bin/bash
set -e

echo "Running pre-PR checks..."

echo "→ Formatting..."
uv run ruff format .

echo "→ Linting..."
uv run ruff check .

echo "→ Type checking..."
uv run mypy .

echo "→ Running tests..."
uv run pytest

echo "✓ All checks passed!"
