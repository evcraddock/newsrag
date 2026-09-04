# YAML manifest ingest end-to-end

## Goal

Allow users to ingest a hand-curated YAML manifest of source URLs or local paths and metadata so civic document batches can be managed reproducibly.

## Requirements

- Add `newsrag ingest-manifest <path>` for YAML manifests.
- Support a `documents` list with a `source` URL or local path, an optional `type` hint, and metadata fields including title, meeting date, body, document type, and jurisdiction.
- Validate the entire manifest and report useful errors for missing sources, invalid dates, unsupported types, and unsupported fields.
- Enqueue all processing jobs atomically after validation.
- Reuse unified source ingestion behavior.

## Acceptance criteria

- [ ] A valid manifest enqueues one job per document.
- [ ] Invalid manifests fail with clear validation errors and enqueue no partial work.
- [ ] Manifest metadata is preserved on created documents.
- [ ] Unit tests cover URL and local sources, missing required fields, invalid date values, type hints, and duplicate sources.

## Dependencies

- task-44db91c8 — Direct PDF URL ingest end-to-end
