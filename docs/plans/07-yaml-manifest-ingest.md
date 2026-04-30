# YAML manifest ingest end-to-end

## Goal

Allow users to ingest a hand-curated YAML manifest of direct PDF URLs and metadata so civic document batches can be managed reproducibly.

## Requirements

- Add `newsrag ingest-manifest <path>` for YAML manifests.
- Support a `documents` list with URL and optional metadata fields including title, meeting date, body, document type, and jurisdiction.
- Validate manifest shape and report useful errors for missing URLs, invalid dates, and unsupported fields.
- Enqueue one processing job per valid manifest document.
- Reuse direct URL ingestion and local processing behavior.

## Acceptance criteria

- [ ] A valid manifest enqueues one job per document.
- [ ] Invalid manifests fail with clear validation errors and do not enqueue partial ambiguous work unless behavior is explicit.
- [ ] Manifest metadata is preserved on created documents.
- [ ] Unit tests cover valid manifests, missing required fields, invalid date values, and duplicate URLs.

## Dependencies

- task-44db91c8 — Direct PDF URL ingest end-to-end
