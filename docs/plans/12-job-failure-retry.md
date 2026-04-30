# Job failure visibility and retry UX

## Goal

Make background processing failures visible, debuggable, and recoverable from the CLI.

## Requirements

- Store failure error messages, timestamps, and enough context to understand which document/job failed.
- Add or refine `newsrag jobs list` to highlight failed jobs.
- Add `newsrag jobs retry <job-id>` to move retryable failed jobs back to pending.
- Ensure retry does not duplicate already-created records for idempotent processing steps.
- Make `newsrag status` summarize failed job count and queue health.

## Acceptance criteria

- [ ] A failed mocked job appears in status/jobs output with an error message.
- [ ] Retrying a failed job changes it back to pending and allows the daemon to process it again.
- [ ] Retrying a non-failed or unknown job reports a clear error.
- [ ] Unit tests cover failure display, retry state transition, and idempotent retry behavior.

## Dependencies

- task-6142564f — Daemon run loop and durable job queue
- task-7b45b114 — Local PDF ingest end-to-end
