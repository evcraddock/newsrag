# Watcher debouncing and daemon health checks

## Goal

Make folder watching reliable enough for real PDF drops by debouncing filesystem events, avoiding partially copied files, and exposing daemon health through CLI status/doctor commands.

## Requirements

- Add debounce/stabilization logic around watchfiles events before enqueueing PDFs.
- Avoid processing files that are still changing or partially copied.
- Surface daemon connectivity and watcher health in `newsrag status` and `newsrag doctor`.
- Ensure repeated filesystem events for the same stable file do not create duplicate processing jobs.
- Keep watcher behavior unit-testable with mocked events and clocks.

## Acceptance criteria

- [ ] A burst of events for one PDF produces one enqueue action after stabilization.
- [ ] A changing file is not enqueued until it is stable under test.
- [ ] `doctor` or `status` reports whether daemon connectivity can be established where applicable.
- [ ] Watcher health failures produce actionable errors.

## Dependencies

- task-40c08a4f — Watched folder ingestion
- task-393a0a9c — Job failure visibility and retry UX
