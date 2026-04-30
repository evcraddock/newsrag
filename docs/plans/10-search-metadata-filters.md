# Search metadata filters

## Goal

Make search useful for civic research by allowing users to filter results by supplied document metadata.

## Requirements

- Add search filters for body, document type, jurisdiction/source, and meeting date range.
- Apply filters consistently across keyword candidates, vector candidates, and final merged results.
- Preserve filter metadata in result output where helpful.
- Keep filter behavior deterministic and documented in CLI help.

## Acceptance criteria

- [ ] `newsrag search "query" --body "Planning Commission" --since 2025-01-01` filters results as expected under test.
- [ ] Filters apply to both FTS5 and LanceDB-backed candidate paths or are enforced during final merge without leaking out-of-filter results.
- [ ] Invalid date filters produce clear CLI errors.
- [ ] Filtered no-result cases distinguish no evidence from invalid input.

## Dependencies

- task-e5c2f467 — Hybrid search with citations
