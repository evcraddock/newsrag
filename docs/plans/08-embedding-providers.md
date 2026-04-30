# Embedding provider integration

## Goal

Provide a pluggable embedding layer with a local-first default so chunks can be embedded for LanceDB vector search without hard-coding one provider forever.

## Requirements

- Define an embedding provider interface for embedding chunks and queries.
- Implement the default Ollama provider for `nomic-embed-text`.
- Make provider/model configurable through global config and CLI override where useful.
- Store provider, model, and version information with embedding records.
- Add `doctor` checks for Ollama reachability and configured model availability.
- Leave room for `bge-base-en` and OpenAI providers without forcing them as the first implementation if sequencing requires a smaller slice.

## Acceptance criteria

- [ ] Query and chunk embedding calls work through a provider interface under test.
- [ ] The Ollama provider can be tested with mocked HTTP/API behavior.
- [ ] Missing Ollama or missing model produces actionable `doctor` output.
- [ ] Embedding metadata records include provider/model identity.

## Dependencies

- task-11831224 — CLI, config, and doctor foundation
- task-9c242503 — Corpus storage lifecycle
