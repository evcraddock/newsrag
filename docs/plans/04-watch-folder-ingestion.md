# Watched folder ingestion

## Goal

Allow users to register folders for automatic PDF ingestion so the daemon can enqueue new documents when files appear.

## Requirements

- Add `newsrag watch add <path>` with metadata options such as body, document type, meeting date default, and jurisdiction where appropriate.
- Add `newsrag watch list` to show configured watches.
- Store watch registrations in durable state associated with the selected data directory.
- Use `watchfiles` in the daemon to observe registered folders.
- Enqueue newly discovered PDFs as ingestion jobs without immediately doing all processing in the CLI.

## Acceptance criteria

- [ ] A watch can be added and listed.
- [ ] The daemon observes a configured folder and enqueues a job when a PDF appears, using mocked filesystem events in tests.
- [ ] Non-PDF files are ignored.
- [ ] Duplicate file events do not create duplicate jobs for the same unchanged file.

## Dependencies

- task-6142564f — Daemon run loop and durable job queue
