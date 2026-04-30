# Daemon run loop and durable job queue

## Goal

Implement the portable daemon entrypoint and durable job queue so NewsRAG can process work asynchronously while the CLI remains responsive.

## Requirements

- Add `newsrag daemon run` as the foreground long-running daemon entrypoint for external process managers.
- Store jobs durably in SQLite with pending, running, done, and failed states.
- Implement an async worker loop that claims pending jobs, executes registered handlers, and records completion or failure.
- Add `newsrag jobs list` or equivalent visibility into queued and running jobs.
- Keep job processing deterministic and testable with mocked job handlers.

## Acceptance criteria

- [ ] `newsrag daemon run` can start against an initialized data directory and wait for jobs.
- [ ] A mocked job can move from pending to running to done under test.
- [ ] A failing mocked job records failed state and an error message under test.
- [ ] `newsrag jobs list` shows pending, running, done, and failed jobs in a human-readable form.

## Dependencies

- task-9c242503 — Corpus storage lifecycle
