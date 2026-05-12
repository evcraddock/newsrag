# CLI, config, and doctor foundation

## Goal

Create the initial Typer CLI foundation, global config loading, selected data-directory resolution, and a `newsrag doctor` command that reports whether the local environment is ready for NewsRAG work.

## Requirements

- Add a Typer-based `newsrag` CLI entrypoint runnable through `uv run newsrag`.
- Load user-global configuration from a conventional config path such as `~/.config/newsrag/config.yaml`.
- Resolve the active corpus data directory from CLI flag, config, or the default user data directory.
- Implement `newsrag doctor` with checks for config validity, data-dir writability, OCR tooling presence, embedding provider availability, and daemon connectivity when applicable.
- Keep output readable for humans and stable enough for agents to inspect.

## Acceptance criteria

- [ ] `uv run newsrag --help` shows the CLI and top-level commands.
- [ ] `uv run newsrag doctor` runs without crashing in a fresh checkout and reports missing optional prerequisites clearly.
- [ ] Config loading has unit test coverage for default config, missing config, invalid config, and CLI override behavior.
- [ ] Data-directory resolution has unit test coverage.

## Dependencies

No blockers.
