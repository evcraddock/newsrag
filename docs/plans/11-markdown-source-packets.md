# Markdown source packet generation

## Goal

Implement `newsrag packet` so users can generate reusable Markdown source packets from retrieved evidence.

## Requirements

- Add `newsrag packet <query> --out <path>`.
- Use the same retrieval pipeline and metadata filters as `newsrag search`.
- Generate a fixed research-oriented Markdown template with sections for Key Evidence, Timeline, Open Questions, and Source List.
- Include cited passages and source-list entries with document title, body, meeting date, page number, and source file where available.
- Keep packet generation extractive and templated rather than LLM-authored.

## Acceptance criteria

- [ ] `newsrag packet "query" --out packet.md` writes a Markdown file from mocked retrieval results.
- [ ] The packet contains `# Source Packet: <query>`, `## Key Evidence`, `## Timeline`, `## Open Questions`, and `## Source List`.
- [ ] Evidence entries include citations in the agreed human-readable format.
- [ ] Existing output files are handled safely with explicit overwrite behavior.

## Dependencies

- task-8b24551a — Search metadata filters
