# AI Agent Guidelines for newsrag

## Before Starting ANY Task

**ALWAYS use the `task-start-preflight` skill** when you hear:
- "start task", "work on task", "get started", "pick up task"
- "let's do task", "begin task", "tackle task"
- Or any variation of starting work

The preflight ensures you understand the task, check dependencies, and follow project guidelines.

## Required Reading

Before working, read and follow:
- [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md) - workflow and PR process
- [docs/CODE_STANDARDS.md](docs/CODE_STANDARDS.md) - code style and patterns

You MUST follow these guidelines throughout your work.

## Project Overview

Local-first CLI evidence retrieval tool for city hall PDFs with OCR, hybrid search, and cited Markdown source packets.

## Tech Stack

- Language: Python
- Framework: None

## Development

**ALWAYS start the dev server using `make dev`** - this runs all services (app, database, etc.) via the Makefile.

Key Makefile targets:
- `make dev` - Start development server (REQUIRED)
- `make test` - Run tests
- `make lint` - Run linter
- `make fmt` - Format code

Read the Makefile to understand available commands before starting work.

## Dependencies

When installing packages:
- Use latest **STABLE** versions only
- Reject canary/beta/alpha/rc versions unless user explicitly approves
- Check PyPI for stable releases: `pip index versions <package>`

Non-stable versions (canary, beta, alpha, rc) can have bugs or incomplete features. Always ask before using them.

## Task Lifecycle

- **Starting**: ALWAYS run `task-start-preflight` skill first
- **Closing**: Run `task-close-gate` skill

## PR Workflow (Mandatory Sequence)

After implementation is complete, execute this exact order:

1. Run `./scripts/pre-pr.sh`
2. Push branch and open/update PR
3. Resolve CI gate:
   - If checks exist: wait for green
   - If checks fail: fetch failures, fix, rerun `./scripts/pre-pr.sh`, push, re-check
   - If checks cannot be verified automatically (e.g., Forgejo without CI integration): stop and ask the human whether to continue without a CI signal
4. Run the `pr-review` skill for independent review
5. Report review result with explicit pipeline state
6. Stop and wait for explicit human merge approval

Do not ask "want me to...?" for required next steps.

## Conventions

- Use type hints for all functions
- Format with Ruff (ruff format)
- Lint with Ruff (ruff check)
- Use pytest for testing
- Follow PEP 8 naming conventions
